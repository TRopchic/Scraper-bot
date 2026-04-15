"""
scraper/binance_scraper.py
──────────────────────────
Headless browser scraper for Binance Copy Trading / Futures Leaderboard.

Strategy:
  1. Launch Chromium via Playwright with stealth patches applied.
  2. For each trader UID in config.TRADER_UIDS:
     a. Register a network response listener to intercept XHR calls
        to Binance's internal `getOtherPosition` and similar endpoints.
     b. Navigate to the trader's copy-trading profile page.
     c. Wait for position data to arrive via the intercepted XHR.
     d. Parse the JSON payload into normalised position dicts.
  3. Return all collected positions to the caller.

The scraper also supports a FALLBACK path: if XHR interception fails
(e.g. Binance changed the endpoint), it tries to make a direct POST
to the known `bapi` endpoint using the browser's cookies/session.
"""

import json
import logging
import time
from typing import Any

from playwright.sync_api import sync_playwright, Page, Response, Browser

try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = None  # graceful degradation

import sys, os

# Allow imports from project root when running as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import (
    TRADER_UIDS,
    BINANCE_BASE_URL,
    LEADERBOARD_POSITIONS_URL,
    LEADERBOARD_BASE_INFO_URL,
    COPY_TRADING_PROFILE_TEMPLATE,
    PAGE_LOAD_TIMEOUT_MS,
    POST_LOAD_WAIT_MS,
    INTER_TRADER_DELAY_MS,
)

logger = logging.getLogger(__name__)


# ─── Response parsing ───────────────────────────────────────────────

def _parse_positions(raw_data: dict, trader_uid: str) -> list[dict[str, Any]]:
    """
    Normalise the JSON blob returned by Binance's getOtherPosition
    endpoint into a flat list of position dicts.

    Expected raw_data structure (may vary):
    {
      "data": [
        {
          "symbol": "BTCUSDT",
          "entryPrice": 64321.5,
          "markPrice": 65100.0,
          "pnl": 123.45,
          "roe": 0.0192,
          "amount": 0.5,
          "leverage": 20,
          "tradeBefore": true,
          "updateTimeStamp": 1700000000000
        },
        ...
      ],
      "success": true
    }
    """
    positions = []

    data_list = raw_data.get("data")
    if not data_list or not isinstance(data_list, list):
        logger.warning(f"No position data found for trader {trader_uid[:8]}…")
        return positions

    for item in data_list:
        try:
            amount = float(item.get("amount", 0))
            # Binance encodes SHORT as negative amount
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
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to parse position item: {e} — raw: {item}")
            continue

    return positions


# ─── Direct API fallback ────────────────────────────────────────────

def _try_direct_api(page: Page, trader_uid: str) -> list[dict[str, Any]]:
    """
    Fallback: use the browser context (with its cookies) to POST
    directly to the getOtherPosition endpoint.
    """
    logger.info(f"🔄  Trying direct API call for {trader_uid[:8]}…")
    try:
        api_response = page.evaluate(
            """async ([url, uid]) => {
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        encryptedUid: uid,
                        tradeType: 'PERPETUAL'
                    })
                });
                return await resp.json();
            }""",
            [LEADERBOARD_POSITIONS_URL, trader_uid],
        )
        if api_response and api_response.get("success"):
            return _parse_positions(api_response, trader_uid)
        else:
            logger.warning(
                f"Direct API returned non-success for {trader_uid[:8]}…: "
                f"{json.dumps(api_response, default=str)[:200]}"
            )
    except Exception as e:
        logger.error(f"Direct API fallback failed for {trader_uid[:8]}…: {e}")

    return []


# ─── XHR interception approach ──────────────────────────────────────

def _scrape_trader(page: Page, trader_uid: str) -> list[dict[str, Any]]:
    """
    Scrape one trader's open positions by navigating to their profile
    and intercepting the XHR response containing position data.
    """
    captured_positions: list[dict[str, Any]] = []

    def on_response(response: Response):
        """Callback fired for every network response."""
        nonlocal captured_positions
        url = response.url

        # Match any of the known position endpoints
        position_keywords = [
            "getOtherPosition",
            "getTraderPositions",
            "position",
        ]

        is_position_endpoint = any(kw in url for kw in position_keywords) and (
            "bapi" in url or "futures" in url
        )

        if is_position_endpoint and response.status == 200:
            try:
                body = response.json()
                if body and isinstance(body, dict) and body.get("data"):
                    parsed = _parse_positions(body, trader_uid)
                    if parsed:
                        captured_positions.extend(parsed)
                        logger.info(
                            f"🎯  Intercepted {len(parsed)} positions for "
                            f"{trader_uid[:8]}… from {url.split('/')[-1]}"
                        )
            except Exception as e:
                logger.debug(f"Could not parse response from {url}: {e}")

    # Register the XHR listener
    page.on("response", on_response)

    # Navigate to the trader's profile page
    profile_url = COPY_TRADING_PROFILE_TEMPLATE.format(uid=trader_uid)
    logger.info(f"🌐  Navigating to {profile_url}")

    try:
        page.goto(profile_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
    except Exception as e:
        logger.warning(f"Page navigation warning for {trader_uid[:8]}…: {e}")

    # Wait for dynamic XHR calls to complete
    page.wait_for_timeout(POST_LOAD_WAIT_MS)

    # Try clicking on "Positions" tab if it exists (triggers XHR)
    try:
        positions_tab = page.locator("text=Positions").first
        if positions_tab.is_visible(timeout=3000):
            positions_tab.click()
            page.wait_for_timeout(3000)
            logger.info("📋  Clicked 'Positions' tab.")
    except Exception:
        pass  # Tab may not exist; that's fine

    # Also try the leaderboard-style profile page as a second chance
    if not captured_positions:
        try:
            leaderboard_url = f"{BINANCE_BASE_URL}/en/futures-activity/leaderboard/user?encryptedUid={trader_uid}"
            logger.info(f"🌐  Trying leaderboard page: {leaderboard_url}")
            page.goto(leaderboard_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            page.wait_for_timeout(POST_LOAD_WAIT_MS)

            # Click "Positions" tab on leaderboard page
            try:
                positions_tab = page.locator("text=Positions").first
                if positions_tab.is_visible(timeout=3000):
                    positions_tab.click()
                    page.wait_for_timeout(3000)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Leaderboard page navigation failed: {e}")

    # Remove the listener
    page.remove_listener("response", on_response)

    # Fallback: direct API call if interception got nothing
    if not captured_positions:
        logger.info(f"XHR interception returned 0 positions, trying direct API…")
        captured_positions = _try_direct_api(page, trader_uid)

    logger.info(
        f"{'✅' if captured_positions else '❌'}  "
        f"Total positions for {trader_uid[:8]}…: {len(captured_positions)}"
    )
    return captured_positions


# ─── Main entry point ───────────────────────────────────────────────

def scrape_all_traders(
    trader_uids: list[str] | None = None,
    headless: bool = True,
) -> list[dict[str, Any]]:
    """
    Launch a browser, iterate through all configured traders,
    and return the full list of scraped positions.
    """
    uids = trader_uids or TRADER_UIDS
    if not uids:
        logger.warning("⚠️  No trader UIDs configured in config.py — nothing to scrape.")
        return []

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
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )

        page = context.new_page()

        # Apply stealth patches if available
        if stealth_sync:
            stealth_sync(page)
            logger.info("🥷  Stealth patches applied.")
        else:
            logger.warning(
                "⚠️  playwright-stealth not installed — running without stealth. "
                "Install it with: pip install playwright-stealth"
            )

        # Remove the webdriver flag manually as an extra precaution
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            // Override permissions query
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)

        # First visit the main page to get cookies / pass initial checks
        logger.info("🌐  Loading Binance main page to establish session…")
        try:
            page.goto(f"{BINANCE_BASE_URL}/en/futures-activity/leaderboard", 
                      wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            page.wait_for_timeout(5000)
        except Exception as e:
            logger.warning(f"Initial page load warning: {e}")

        # Scrape each trader
        for i, uid in enumerate(uids):
            logger.info(f"\n{'='*60}")
            logger.info(f"📌  Scraping trader {i+1}/{len(uids)}: {uid[:8]}…")
            logger.info(f"{'='*60}")

            try:
                positions = _scrape_trader(page, uid)
                all_positions.extend(positions)
            except Exception as e:
                logger.error(f"❌  Error scraping trader {uid[:8]}…: {e}")

            # Respectful delay between traders
            if i < len(uids) - 1:
                page.wait_for_timeout(INTER_TRADER_DELAY_MS)

        browser.close()

    logger.info(f"\n🏁  Scraping complete. Total positions collected: {len(all_positions)}")
    return all_positions


# ─── CLI entry ───────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    positions = scrape_all_traders(headless=True)
    print(json.dumps(positions, indent=2, default=str))
