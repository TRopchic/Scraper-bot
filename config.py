"""
Central configuration for the Binance Copy Trading scraper.

Add or remove trader UIDs here. These are the "encryptedUid" values
from Binance's leaderboard / copy-trading profile pages.

To find a trader's encryptedUid:
  1. Go to https://www.binance.com/en/copy-trading
  2. Click on a lead trader's profile
  3. Open DevTools (F12) → Network tab → filter "bapi"
  4. Look for requests to getOtherPosition or getTraderBaseInfo
  5. The encryptedUid is in the request payload or URL parameters
"""

# ─── Trader UIDs to track ───────────────────────────────────────────
# Replace these with real encryptedUid values from Binance.
# You can add as many as you want; the scraper iterates over all of them.
TRADER_UIDS = [
    # Example (replace with real values):
    # "0987654321ABCDEF0987654321ABCDEF",
    # "ABCDEF0987654321ABCDEF0987654321",
]

# ─── Binance internal API endpoints ─────────────────────────────────
# These are the backend endpoints the Binance web app calls.
# They may change without notice — update here if they break.
BINANCE_BASE_URL = "https://www.binance.com"

LEADERBOARD_POSITIONS_URL = (
    f"{BINANCE_BASE_URL}/bapi/futures/v1/public/future/leaderboard/getOtherPosition"
)

LEADERBOARD_BASE_INFO_URL = (
    f"{BINANCE_BASE_URL}/bapi/futures/v1/public/future/leaderboard/getOtherLeaderboardBaseInfo"
)

# Copy-trading profile page (used to load the page and intercept XHR)
COPY_TRADING_PROFILE_TEMPLATE = (
    f"{BINANCE_BASE_URL}/en/copy-trading/lead-details/{{uid}}?timeRange=7D"
)

# ─── Scraper settings ───────────────────────────────────────────────
# Maximum time (ms) to wait for XHR responses after page load
PAGE_LOAD_TIMEOUT_MS = 30_000

# Additional wait (ms) after page load to catch late XHR responses
POST_LOAD_WAIT_MS = 8_000

# Delay between scraping different traders (ms) — be respectful
INTER_TRADER_DELAY_MS = 3_000
