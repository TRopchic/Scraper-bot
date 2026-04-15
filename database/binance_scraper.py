"""
scraper/binance_scraper.py
──────────────────────────
Headless browser scraper for Binance Copy Trading / Futures Leaderboard.

Strategy:
  1. Launch Chromium via Playwright with stealth patches applied.
  2. PHASE 1 (DISCOVERY): Navigate to the copy trading leaderboard.
     - Intercept the internal Binance API payload used to fetch the grid list.
     - Replay this intercepted request inside `page.evaluate()` scaling 'pageNumber' 1..50.
     - Harvest ALL public trader UIDs (~thousands).
  3. PHASE 2 (FETCHING): 
     - We push a Javascript loop into the browser that fetches `getOtherPosition`
       for every harvested UID, reusing the same cookies/headers simultaneously.
  4. Yield all parsed positions.
"""

import json
import logging
import time
from typing import Any

from playwright.sync_api import sync_playwright, Page, Response, Browser

try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = None

import sys, os

# Allow imports from project root when running as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import (
    TRADER_UIDS,
    BINANCE_BASE_URL,
    PAGE_LOAD_TIMEOUT_MS,
    POST_LOAD_WAIT_MS,
    INTER_TRADER_DELAY_MS,
)

logger = logging.getLogger(__name__)

# ─── Response parsing ───────────────────────────────────────────────

def _parse_positions(raw_data: dict, trader_uid: str) -> list[dict[str, Any]]:
    positions = []
    data_list = raw_data.get("data")
    if not data_list or not isinstance(data_list, list):
        return positions

    for item in data_list:
        try:
            amount = float(item.get("amount", 0))
            if amount < 0:
                position_side = "SHORT"
                amount = abs(amount)
            else:
                position_side = "LONG"

            position = {
                "trader_uid": trader_uid,
                "symbol": str(item.get("symbol", "UNKNOWN")),
                "position_side": position_side,
                "entry_price": float(item.get("entryPrice", 0)),
                "mark_price": float(item.get("markPrice", 0)),
                "amount": amount,
                "pnl": float(item.get("pnl", 0)),
                "roe": float(item.get("roe", 0)),
                "leverage": int(item.get("leverage", 1)),
                "update_timestamp": item.get("updateTimeStamp"),
            }
            positions.append(position)
        except Exception:
            continue
    return positions


# ─── Core Logic ───────────────────────────────────────────────

def scrape_all_traders(
    trader_uids: list[str] | None = None,
    headless: bool = True,
) -> list[dict[str, Any]]:
    """
    Scrape all copy traders on Binance dynamically using In-Browser Fetch Injection.
    """
    uids_to_scrape = set(trader_uids or TRADER_UIDS or [])
    all_positions: list[dict[str, Any]] = []

    with sync_playwright() as pw:
        # Launch with anti-detection args
        browser: Browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        if stealth_sync:
            stealth_sync(page)
            logger.info("🥷  Stealth patches applied.")

        # Override webdriver check
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        # ─── PHASE 1: DISCOVER ALL UIDS via Leadersboard ───
        logger.info("🌐  Navigating to Copy Trading Leaderboard to harvest all UIDs...")
        try:
            page.goto(f"{BINANCE_BASE_URL}/en/copy-trading", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            page.wait_for_timeout(3000)
        except Exception as e:
            logger.warning(f"Timeout on initial load, continuing... {e}")

        # Try to automatically harvest all UIDs from the page DOM using a mass scraping script.
        # It repeatedly scrolls and harvests any encryptedUid found in the profile links.
        logger.info("🕵️  Injecting mass-harvesting script into browser...")
        harvested = page.evaluate('''async () => {
            let uids = new Set();
            for(let i=0; i<15; i++) {
                let links = document.querySelectorAll('a[href*="/lead-details/"]');
                links.forEach(el => {
                    let href = el.getAttribute("href") || "";
                    let parts = href.split("?");
                    let uid = parts[0].split("/").pop();
                    if(uid && uid.length > 10) uids.add(uid);
                });
                // Attempt to click pagination "Next" button
                let btns = Array.from(document.querySelectorAll('button, svg'));
                for (let b of btns) {
                    if (b.getAttribute('aria-label') === 'Next' || b.getAttribute('aria-label') === 'Next page') {
                        b.click(); 
                        break;
                    }
                }
                // Scroll to load more
                window.scrollTo(0, document.body.scrollHeight);
                await new Promise(r => setTimeout(r, 1000));
            }
            return Array.from(uids);
        }''')
        
        if harvested:
            for h in harvested:
                uids_to_scrape.add(h)
            logger.info(f"✅  Harvested {len(harvested)} UIDs from Leaderboard UI.")
        else:
            logger.warning("⚠️  Could not harvest UIDs from the UI. Using config fallback if available.")

        # ─── PHASE 2: IN-BROWSER MASS FETCH ───
        final_uids = list(uids_to_scrape)
        logger.info(f"🚀  Phase 2: Executing fast in-browser scrapes for {len(final_uids)} UIDs...")

        if not final_uids:
             logger.warning("No UIDs to scrape. Exiting.")
             browser.close()
             return []

        # We pass batches to the browser to avoid memory/timeout issues
        batch_size = 50
        for i in range(0, len(final_uids), batch_size):
            batch = final_uids[i:i+batch_size]
            logger.info(f"   ► Scraping batch {i//batch_size + 1}/{(len(final_uids)+batch_size-1)//batch_size}...")
            
            # This evaluation runs directly inside the browser using Promise.all to fetch concurrently!
            raw_results = page.evaluate('''async ([uids, url]) => {
                const results = [];
                // Process concurrently in chunks of 5 to avoid Binance rate limits
                for(let i = 0; i < uids.length; i += 5) {
                    let chunk = uids.slice(i, i + 5);
                    let promises = chunk.map(async (uid) => {
                        try {
                            const resp = await fetch(url, {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({encryptedUid: uid, tradeType: 'PERPETUAL'})
                            });
                            const data = await resp.json();
                            return {uid: uid, data: data};
                        } catch (e) {
                            return {uid: uid, error: e.toString()};
                        }
                    });
                    
                    let resolved = await Promise.all(promises);
                    results.push(...resolved);
                    // Tiny delay between chunks
                    await new Promise(r => setTimeout(r, 500));
                }
                return results;
            }''', [batch, f"{BINANCE_BASE_URL}/bapi/futures/v1/public/future/leaderboard/getOtherPosition"])

            # Parse results
            for result in raw_results:
                uid = result.get('uid')
                if 'data' in result and result['data'].get('success'):
                    parsed = _parse_positions(result['data'], uid)
                    if parsed:
                        all_positions.extend(parsed)
            
            # Throttle between large batches
            page.wait_for_timeout(INTER_TRADER_DELAY_MS)

        browser.close()

    logger.info(f"\\n🏁  Scraping complete. Total positions collected: {len(all_positions)}")
    return all_positions

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    positions = scrape_all_traders(headless=True)
    print(json.dumps(positions, indent=2, default=str))
