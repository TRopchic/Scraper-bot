"""
main.py
───────
Entry point for the Binance Copy Trading scraper pipeline.

This is what GitHub Actions (and local runs) execute.
Flow:
  1. Scrape all configured traders' open positions.
  2. Upsert the data into MongoDB Atlas.
  3. Mark positions that are no longer active as CLOSED.
  4. Log a summary.
"""

import logging
import sys
from datetime import datetime, timezone

from scraper.binance_scraper import scrape_all_traders
from database.db_client import insert_trades, mark_closed_positions, close_connection

# ─── Logging setup ───────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main():
    start = datetime.now(timezone.utc)
    logger.info(f"🚀  Pipeline started at {start.isoformat()}")

    # ── Step 1: Scrape ──
    logger.info("─" * 50)
    logger.info("STEP 1: Scraping Binance trader positions…")
    logger.info("─" * 50)

    positions = scrape_all_traders(headless=True)

    if not positions:
        logger.warning("⚠️  No positions scraped. Exiting.")
        return

    # ── Step 2: Store in MongoDB ──
    logger.info("─" * 50)
    logger.info("STEP 2: Upserting positions into MongoDB…")
    logger.info("─" * 50)

    result = insert_trades(positions)
    logger.info(f"📊  DB Result: {result}")

    # ── Step 3: Mark closed positions ──
    logger.info("─" * 50)
    logger.info("STEP 3: Detecting closed positions…")
    logger.info("─" * 50)

    # Group active symbols by trader
    trader_active: dict[str, set[str]] = {}
    for pos in positions:
        uid = pos["trader_uid"]
        trader_active.setdefault(uid, set()).add(pos["symbol"])

    for uid, symbols in trader_active.items():
        mark_closed_positions(uid, symbols)

    # ── Done ──
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    logger.info(f"\n✅  Pipeline complete in {elapsed:.1f}s")
    logger.info(f"   Positions scraped : {len(positions)}")
    logger.info(f"   New in DB         : {result['inserted']}")
    logger.info(f"   Updated in DB     : {result['modified']}")

    close_connection()


if __name__ == "__main__":
    main()
