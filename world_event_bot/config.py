"""
World Event Bot configuration.

Defaults are tuned for the EdgeFinder-consumer doctrine (v2, 2026-05-27).
See _config/signal-doctrine.md and _config/sizing.md for the rationale
behind every number.

Override any value via .env — these constants read from os.environ.
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ── External APIs ─────────────────────────────────────────────────────────────

# EdgeFinder (primary signal source — see _config/edgefinder-api.md)
EDGEFINDER_POLL_INTERVAL_S = float(os.getenv("EDGEFINDER_POLL_INTERVAL_S", "300"))  # 5 min

# Open-position health check (stop-loss + EF resolution) runs far more often than
# signal polling so fast market moves trip the stop-loss before they gap past it.
POSITION_CHECK_INTERVAL_S  = float(os.getenv("POSITION_CHECK_INTERVAL_S", "30"))  # 30 s

# Polymarket Gamma (enrichment only in v2)
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
EVENTS_LIMIT   = int(os.getenv("EVENTS_LIMIT", "100"))
REQUEST_TIMEOUT_GAMMA = 30

# Polymarket CLOB (NO-ask verification + price monitoring)
CLOB_API_BASE  = "https://clob.polymarket.com"
REQUEST_TIMEOUT_CLOB = 15


# ── Paper bankroll ────────────────────────────────────────────────────────────

STARTING_BALANCE   = float(os.getenv("STARTING_BALANCE",   "1000"))


# ── Kelly sizing (see _config/sizing.md) ─────────────────────────────────────

# Fraction of full Kelly to apply. 0.5 = half-Kelly (lesson recommendation).
KELLY_FRACTION              = float(os.getenv("KELLY_FRACTION",              "0.5"))

# Hard cap on any single trade as fraction of bankroll.
MAX_BANKROLL_PCT_PER_TRADE  = float(os.getenv("MAX_BANKROLL_PCT_PER_TRADE",  "0.05"))

# Cash always kept aside; never deployed.
RESERVE_PCT                 = float(os.getenv("RESERVE_PCT",                 "0.30"))

# Cap exposure across positions resolving in the same 7-day window.
CORRELATION_CLUSTER_CAP_PCT = float(os.getenv("CORRELATION_CLUSTER_CAP_PCT", "0.25"))

# Polymarket fee on winning positions (observed up to 4.8% per M5).
POLYMARKET_FEE_RATE         = float(os.getenv("POLYMARKET_FEE_RATE",         "0.048"))

# Soft ceiling on dollar size (defense against config errors making Kelly explode).
MAX_TRADE_SIZE              = float(os.getenv("MAX_TRADE_SIZE",              "200"))


# ── Entry gates (see _config/signal-doctrine.md "Entry gates") ───────────────

# Volume floor on the underlying Polymarket market.
MIN_VOLUME_24HR             = float(os.getenv("MIN_VOLUME_24HR", "50000"))

# Max acceptable slippage between EdgeFinder's published yes_price and current NO ask.
MAX_PRICE_DRIFT             = float(os.getenv("MAX_PRICE_DRIFT", "0.02"))   # 2¢

# Skip a signal if it was emitted by EdgeFinder more than this many hours ago.
MAX_SIGNAL_AGE_HOURS        = int(os.getenv("MAX_SIGNAL_AGE_HOURS", "72"))

# Max simultaneous open positions (defense against config errors; 0 = unlimited).
MAX_OPEN_POSITIONS          = int(os.getenv("MAX_OPEN_POSITIONS", "30"))


# ── Risk management ──────────────────────────────────────────────────────────

# Close the NO position if YES climbs back to this level (M17, M22, M28, M30).
STOP_LOSS_YES_THRESHOLD     = float(os.getenv("STOP_LOSS_YES_THRESHOLD", "0.50"))


# ── Storage ───────────────────────────────────────────────────────────────────

DB_PATH       = os.getenv("DB_PATH", "polymarket_event_bot.db")


# ── Legacy (v1) — not load-bearing in v2 ─────────────────────────────────────
# DEAD IN v2: signal_engine.py reads from EdgeFinder; market_scanner.py is only
# used to generate market_snapshots for our_engine (which doesn't filter by keywords).
# Kept here so market_scanner.py still imports cleanly without KeyError.

MAX_SPREAD             = float(os.getenv("MAX_SPREAD", "0.04"))
MAX_HOURS_TO_RESOLVE   = int(os.getenv("MAX_HOURS_TO_RESOLVE", "0"))   # 0 = disabled
SCAN_INTERVAL          = float(os.getenv("SCAN_INTERVAL", "300"))

# Not consulted by any active v2 path. market_scanner reads it but our_engine
# snapshots all markets regardless of category.
TARGET_KEYWORDS = [
    "government", "geopolit", "election", "economy", "economic",
    "finance", "financial", "tech", "technology", "artificial intelligence",
    " ai ", "policy", "international", "trade war", "diplomacy",
    "military", "sanction", "war", "conflict", "president", "senate",
    "congress", "parliament", "inflation", "gdp", "federal reserve",
    "interest rate", "recession", "tariff", "nato", "united nations",
    "g7", "g20", "imf", "world bank", "silicon valley", "regulation",
    "climate policy", "nuclear", "missile", "treaty", "referendum",
]

# v2 doctrine: don't hard-exclude anything — EdgeFinder pre-filters.
EXCLUDE_KEYWORDS: list = []
