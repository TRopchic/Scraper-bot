"""
database/db_client.py
─────────────────────
MongoDB Atlas client for the Binance Copy Trading scraper.

Responsibilities:
  • Connect to MongoDB Atlas using the MONGO_URI env var.
  • Upsert scraped positions into the `trader_positions` collection.
  • Maintain a `position_history` collection that logs snapshots
    every time a position's PnL / mark price changes.
  • Avoid duplicates: a position is uniquely identified by
    (trader_uid, symbol, position_side, entry_price, amount).
"""

import os
import logging
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError

logger = logging.getLogger(__name__)

# ─── Connection ──────────────────────────────────────────────────────

_client: MongoClient | None = None


def get_client() -> MongoClient:
    """Lazy-initialise and return a MongoClient singleton."""
    global _client
    if _client is None:
        uri = os.environ.get("MONGO_URI")
        if not uri:
            raise RuntimeError(
                "MONGO_URI environment variable is not set. "
                "Please set it to your MongoDB Atlas connection string."
            )
        _client = MongoClient(uri, serverSelectionTimeoutMS=10_000)
        # Quick connectivity check
        _client.admin.command("ping")
        logger.info("✅  Connected to MongoDB Atlas successfully.")
    return _client


def get_database(db_name: str = "binance_scraper"):
    """Return the database handle."""
    return get_client()[db_name]


# ─── Collections ─────────────────────────────────────────────────────

def _positions_collection(db_name: str = "binance_scraper"):
    return get_database(db_name)["trader_positions"]


def _history_collection(db_name: str = "binance_scraper"):
    return get_database(db_name)["position_history"]


# ─── Index creation (idempotent) ─────────────────────────────────────

def ensure_indexes(db_name: str = "binance_scraper"):
    """Create compound indexes for fast lookups and deduplication."""
    pos = _positions_collection(db_name)
    pos.create_index(
        [
            ("trader_uid", 1),
            ("symbol", 1),
            ("position_side", 1),
            ("entry_price", 1),
            ("amount", 1),
        ],
        unique=True,
        name="unique_position",
    )
    pos.create_index([("trader_uid", 1)], name="by_trader")
    pos.create_index([("updated_at", -1)], name="by_updated")

    hist = _history_collection(db_name)
    hist.create_index([("trader_uid", 1), ("symbol", 1)], name="hist_by_trader_symbol")
    hist.create_index([("snapshot_at", -1)], name="hist_by_time")

    # Set up TTL index for position_history (auto-delete after 30 days)
    try:
        hist.create_index(
            "snapshot_at", 
            expireAfterSeconds=2592000, # 30 days
            name="history_ttl_30d"
        )
        logger.info("✅  Database indexes ensured.")
    except Exception as e:
        logger.error(f"Failed to create TTL index: {e}")


# ─── Upsert logic ───────────────────────────────────────────────────

def insert_trades(positions: list[dict[str, Any]], db_name: str = "binance_scraper") -> dict:
    """
    Upsert a list of scraped positions into MongoDB.

    Each position dict should contain at minimum:
      - trader_uid      (str)  encrypted UID from Binance
      - symbol          (str)  e.g. "BTCUSDT"
      - position_side   (str)  "LONG" or "SHORT"
      - entry_price     (float)
      - mark_price      (float)
      - amount          (float) position size
      - pnl             (float) unrealised PnL
      - roe             (float) return on equity / ROI %
      - leverage        (int)

    Returns a summary dict: {inserted, modified, errors}.
    """
    if not positions:
        logger.info("No positions to insert.")
        return {"inserted": 0, "modified": 0, "errors": 0}

    ensure_indexes(db_name)
    col = _positions_collection(db_name)
    hist = _history_collection(db_name)
    now = datetime.now(timezone.utc)

    bulk_ops = []
    history_docs = []

    for pos in positions:
        # ── Build the filter (unique position identity) ──
        filt = {
            "trader_uid": pos["trader_uid"],
            "symbol": pos["symbol"],
            "position_side": pos["position_side"],
            "entry_price": pos["entry_price"],
            "amount": pos["amount"],
        }

        # ── Fields that may change over time ──
        update_fields = {
            "mark_price": pos.get("mark_price"),
            "pnl": pos.get("pnl"),
            "roe": pos.get("roe"),
            "leverage": pos.get("leverage"),
            "updated_at": now,
        }

        # ── Fields set only on first insert ──
        on_insert_fields = {
            **filt,
            "first_seen_at": now,
            "updated_at": now,
            "mark_price": pos.get("mark_price"),
            "pnl": pos.get("pnl"),
            "roe": pos.get("roe"),
            "leverage": pos.get("leverage"),
        }

        bulk_ops.append(
            UpdateOne(
                filt,
                {
                    "$set": update_fields,
                    "$setOnInsert": {
                        "first_seen_at": now,
                    },
                },
                upsert=True,
            )
        )

        # ── Always log a snapshot in history ──
        history_docs.append(
            {
                "trader_uid": pos["trader_uid"],
                "symbol": pos["symbol"],
                "position_side": pos["position_side"],
                "entry_price": pos["entry_price"],
                "mark_price": pos.get("mark_price"),
                "amount": pos["amount"],
                "pnl": pos.get("pnl"),
                "roe": pos.get("roe"),
                "leverage": pos.get("leverage"),
                "snapshot_at": now,
            }
        )

    # ── Execute bulk upsert ──
    result = {"inserted": 0, "modified": 0, "errors": 0}
    try:
        bulk_result = col.bulk_write(bulk_ops, ordered=False)
        result["inserted"] = bulk_result.upserted_count
        result["modified"] = bulk_result.modified_count
        logger.info(
            f"📊  Positions upserted: {bulk_result.upserted_count} new, "
            f"{bulk_result.modified_count} updated."
        )
    except BulkWriteError as bwe:
        result["errors"] = len(bwe.details.get("writeErrors", []))
        logger.warning(f"⚠️  Bulk write had {result['errors']} errors: {bwe.details}")

    # ── Insert history snapshots ──
    if history_docs:
        hist.insert_many(history_docs, ordered=False)
        logger.info(f"📸  Logged {len(history_docs)} history snapshots.")

    return result


# ─── Mark closed positions ──────────────────────────────────────────

def mark_closed_positions(
    trader_uid: str,
    active_symbols: set[str],
    db_name: str = "binance_scraper",
):
    """
    For a given trader, mark any positions in the DB that are no longer
    in the active list as 'closed'. This lets us track deals end-to-end.
    """
    col = _positions_collection(db_name)
    now = datetime.now(timezone.utc)

    result = col.update_many(
        {
            "trader_uid": trader_uid,
            "symbol": {"$nin": list(active_symbols)},
            "status": {"$ne": "CLOSED"},
        },
        {
            "$set": {
                "status": "CLOSED",
                "closed_at": now,
                "updated_at": now,
            }
        },
    )
    if result.modified_count > 0:
        logger.info(
            f"🔒  Marked {result.modified_count} positions as CLOSED for trader {trader_uid[:8]}…"
        )


# ─── Cleanup ─────────────────────────────────────────────────────────

def close_connection():
    """Gracefully close the MongoDB connection."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
        logger.info("🔌  MongoDB connection closed.")
