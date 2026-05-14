"""
Crypto Ghost Scanner 4 (S4) -- Market Open Predator
====================================================
Fork of Scanner 3. Completely different entry logic.

Enters UP/DOWN markets at $0.38-$0.62 at market open (4.0-5.5 min left),
holds until resolution. Uses 3-pillar order flow confirmation:

  Pillar 1 — Rolling CVD (5x1min Binance klines):
             buy_vol - sell_vol sum must be positive AND improving.
  Pillar 2 — ATR-relative squeeze: tick range < 0.6 * ATR(10) on 1m bars.
             OBI on our token > 0.12 confirms accumulation.
  Pillar 3 — HMA direction (period 9) must match entry side.

  Anomaly switch: OBI and CVD strongly conflicting -> skip (trap).
  Spread filter: ask - bid must be <= $0.04.

All parameters hardcoded. Telegram alerts on every fire.
Scans ALL crypto coins (BTC/ETH/BNB/SOL/XRP).

PM2: pm2 start crypto_ghost_scanner4.py --name scanner4 --interpreter python3
DB tag: strategy='s4_reversal', tier=8
"""

import asyncio
import aiohttp
import json
import time
import sqlite3
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple, Any

# ─── WINDOWS FIX / UVLOOP ────────────────────────────────────────────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
else:
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass

# ─── LOAD .ENV ────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    # Try multiple locations (your existing UnifiedBot .env or local)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_candidates = [
        os.path.join(script_dir, ".env"),
        os.path.join(script_dir, "..", ".env"),
        r"C:\Users\ramos\OneDrive\Desktop\CryptoGhostScanner\.env",
        r"C:\Users\ramos\Desktop\CryptoGhostScanner\.env",
        os.path.join(os.path.expanduser("~"), "Desktop", ".env"),
        r"C:\Users\ramos\OneDrive\Desktop\UnifiedBot\.env",
    ]
    for ep in env_candidates:
        if os.path.exists(ep):
            load_dotenv(ep, override=True)
            print(f"[ENV] Loaded: {ep}")
            break
except ImportError:
    print("[WARN] python-dotenv not installed — reading env vars directly")

# ─── CLOB CLIENT ──────────────────────────────────────────────────────────────
try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import OrderArgsV2 as OrderArgs, ApiCreds
    BUY = "BUY"
except ImportError:
    print("ERROR: py-clob-client-v2 not installed.")
    print("Run: pip install py-clob-client-v2")
    sys.exit(1)

# ─── CONFIG (all values come from .env — edit that file, not this one) ────────
PRIVATE_KEY     = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS  = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
POLYMARKET_HOST   = "https://clob.polymarket.com"
GAMMA_API         = "https://gamma-api.polymarket.com"
DATA_API          = "https://data-api.polymarket.com"
CLOB_API          = "https://clob.polymarket.com"
# Skip first 1000 old markets in CLOB API (they are old resolved sports markets)
# Recent April 2026 crypto markets start appearing after offset 1000
CLOB_START_CURSOR = "MTAwMA=="
BINANCE_REST    = "https://api.binance.com/api/v3"
BINANCE_WS_URL  = (
    "wss://stream.binance.com:9443/stream?streams="
    "btcusdt@ticker/ethusdt@ticker/bnbusdt@ticker/solusdt@ticker/xrpusdt@ticker"
)

# ── Trade sizing per tier ──
T1_SIZE  = float(os.getenv("T1_SIZE_USDC",  "5.0"))   # Strike crossings
T2_SIZE  = float(os.getenv("T2_SIZE_USDC",  "3.0"))   # Hourly Up/Down (default)
T3_SIZE  = float(os.getenv("T3_SIZE_USDC",  "3.0"))   # 4hr Up/Down
T4_SIZE  = float(os.getenv("T4_SIZE_USDC",  "2.0"))   # Daily Up/Down

# ── Per-coin T2 sizing overrides (data-backed) ──
# Trading BTC / ETH / BNB only. SOL and XRP removed.
T2_SIZE_PER_COIN = {
    "BTC": float(os.getenv("T2_SIZE_BTC", "5.0")),  # 23.7% WR — workhorse
    "ETH": float(os.getenv("T2_SIZE_ETH", "3.0")),  # 9.3%  WR — fair
    "BNB": float(os.getenv("T2_SIZE_BNB", "3.0")),  # no live track record
}

# ── Entry price ceilings per tier ──
T1_MAX_ENTRY = float(os.getenv("T1_MAX_ENTRY", "0.03"))
T2_MAX_ENTRY = float(os.getenv("T2_MAX_ENTRY", "0.030"))  # unused by S4 entry logic
S4_TIER      = 8      # S4 writes tier=8 to DB
T3_MAX_ENTRY = float(os.getenv("T3_MAX_ENTRY", "0.15"))
T4_MAX_ENTRY = float(os.getenv("T4_MAX_ENTRY", "0.20"))

# ── S4 HARDCODED ENTRY CONFIG (ignores .env) ──────────────────────────────────
S4_ENTRY_MIN   = 0.38    # token price floor
S4_ENTRY_MAX   = 0.62    # token price ceiling (wider at open, price ~$0.50)
S4_MAX_SPREAD  = 0.04    # max CLOB bid-ask spread (audit: spread trap filter)
S4_MIN_MINS    = 4.0     # enter only at market open (first ~90s of 5-min market)
S4_MAX_MINS    = 5.5     # max minutes left
S4_MIN_OBI     = 0.12    # OBI threshold (net buy/sell imbalance)
S4_MIN_DEV_1H  = 0.002   # min 1h Binance trend required
S4_CVD_CANDLES = 5       # 1-min candles for rolling CVD
S4_ATR_PERIOD  = 10      # ATR period (1-min candles)
S4_ATR_MULT    = 0.6     # squeeze: tick_range < S4_ATR_MULT * ATR
_CVD_TTL       = 15      # seconds between CVD fetches per coin
_ATR_TTL       = 60      # seconds between ATR fetches per coin

# ── Signal filters ──
MIN_NO_PRICE  = 0.55   # S4: not used for entry; opposite token ~0.55-0.62 in 50/50 zone
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY",  "3.0"))   # min $ at ask

# ── Entry time windows (minutes before market close) ──
T2_WINDOW_MIN = int(os.getenv("T2_WINDOW_MIN", "30"))    # Hourly: final 20 min

# v10.2 STALE-ASK SNIPE WINDOW
# Data from 71-trade backup: WR 53% at 0-30s left, 22% at 30-60s,
# 0-20% beyond. Firing only in the last 60 seconds doubles your edge.
T2_MAX_SECS_LEFT = int(os.getenv("T2_MAX_SECS_LEFT", "60"))   # don't fire if more than 60s left
T2_MIN_SECS_LEFT = int(os.getenv("T2_MIN_SECS_LEFT", "5"))    # don't fire if less than 5s (won't fill)
T3_WINDOW_MIN = int(os.getenv("T3_WINDOW_MIN", "90"))    # 4hr:   final 60 min
T4_WINDOW_MIN = int(os.getenv("T4_WINDOW_MIN", "240"))   # Daily: final 4 hrs

# ── Price deviation required for Up/Down markets ──
T2_MIN_DEV = float(os.getenv("T2_MIN_DEV", "0.003"))   # 0.3% from open
T3_MIN_DEV = float(os.getenv("T3_MIN_DEV", "0.005"))   # 0.5% from open
T4_MIN_DEV = float(os.getenv("T4_MIN_DEV", "0.004"))   # 0.4% from open

# ── Risk controls ──
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "20"))
MAX_DAILY_LOSS     = float(os.getenv("DAILY_LOSS_LIMIT", "50.0"))
SCAN_INTERVAL      = float(os.getenv("SCAN_INTERVAL",    "1.0"))

# v9.5: Skip T1 strike-crossing scan entirely if no strikes are firing.
# Saves ~6s at startup + ~5min idle gamma fetches.
STRIKE_SCAN_ENABLED = os.getenv("STRIKE_SCAN_ENABLED", "false").strip().lower() in ("true","1","yes")

# ── DST-safe ET timezone (for hour-of-day filters) ─────────────────
try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:
    _ET_TZ = None  # Fallback to UTC-4 below if zoneinfo unavailable


def _et_hour() -> int:
    """Current ET hour (0-23). Falls back to UTC-4 if tzdata missing."""
    from datetime import datetime, timezone, timedelta
    if _ET_TZ is not None:
        return datetime.now(_ET_TZ).hour
    return (datetime.now(timezone.utc) - timedelta(hours=4)).hour


# ── Bias-aware filters (data-backed from 5,852 pooled markets) ──────
# Coins listed here have such a strong UP-bias in pooled data that
# cheap-DOWN longshots have negative EV. Skip cheap-DOWN entries on
# these coins. Comma-separated list in .env. Set blank to disable.
SKIP_DOWN_BIASED_COINS = set(
    c.strip().upper() for c in
    os.getenv("SKIP_DOWN_BIASED_COINS", "BNB").split(",")
    if c.strip()
)

# Hour-of-day gate. Hours listed here are skipped for cheap-UP entries
# (in pooled data they have <50% UP-win rate). 11 AM ET only wins 40%
# UP — flip the strategy in those hours, or skip outright.
DEAD_UP_HOURS_ET = set(
    int(h.strip()) for h in
    os.getenv("DEAD_UP_HOURS_ET", "").split(",")
    if h.strip().isdigit()
)
DEAD_DOWN_HOURS_ET = set(
    int(h.strip()) for h in
    os.getenv("DEAD_DOWN_HOURS_ET", "").split(",")
    if h.strip().isdigit()
)
BIAS_FILTERS_ENABLED = os.getenv("BIAS_FILTERS", "true").strip().lower() in ("true","1","yes")

# ── Coin disable list (ghost_analyst auto-tunes via .env) ─────────────────────
_disabled_raw = os.getenv("DISABLED_COINS", "")
DISABLED_COINS: set = {c.strip().upper() for c in _disabled_raw.split(",") if c.strip()}
if DISABLED_COINS:
    print(f"[ENV] DISABLED_COINS = {sorted(DISABLED_COINS)}")

# ── Cold-hour UTC filter (ghost_analyst auto-tunes via .env) ──────────────────
_blocked_raw = os.getenv("BLOCKED_HOURS", "").strip()
BLOCKED_HOURS: set = set()
if _blocked_raw:
    for _h in _blocked_raw.split(","):
        try:
            _v = int(_h.strip())
            if 0 <= _v <= 23:
                BLOCKED_HOURS.add(_v)
        except Exception:
            pass
if BLOCKED_HOURS:
    print(f"[ENV] BLOCKED_HOURS = {sorted(BLOCKED_HOURS)} (UTC)")

# ── Brain state skip list ──────────────────────────────────────────────────────
# DORMANT = 0/43 wins post-fix — well below break-even. If brain file is
# stale/missing the filter is skipped (fail-open). Default: no states blocked.
_skip_brain_raw = os.getenv("SKIP_BRAIN_STATES", "").strip()
SKIP_BRAIN_STATES: set = set()
for _sb in _skip_brain_raw.split(","):
    _sb = _sb.strip().upper()
    if _sb in ("HAUNTING", "STALKING", "DRIFTING", "DORMANT"):
        SKIP_BRAIN_STATES.add(_sb)
if SKIP_BRAIN_STATES:
    print(f"[ENV] SKIP_BRAIN_STATES = {sorted(SKIP_BRAIN_STATES)}")
else:
    print(f"[ENV] SKIP_BRAIN_STATES = (none — brain state has no effect on filtering)")

# ── Direction filter + MarketGhost hour bias ───────────────────────────────────
# DIRECTION_FILTER: auto → use MarketGhost DB bias | up_only / down_only | off
HOUR_BIAS_STRONG      = int(os.getenv("HOUR_BIAS_STRONG",      "60"))  # % threshold
HOUR_BIAS_MIN_SAMPLES = int(os.getenv("HOUR_BIAS_MIN_SAMPLES", "15"))  # min resolved markets
DIRECTION_FILTER = os.getenv("DIRECTION_FILTER", "auto").strip().lower()
if DIRECTION_FILTER not in ("auto", "up_only", "down_only", "off"):
    DIRECTION_FILTER = "auto"
print(f"[ENV] DIRECTION_FILTER = {DIRECTION_FILTER}")

# ── V2 momentum gate (ported 2026-05-13) ─────────────────────────────────────
# When TREND_ENHANCED=true, refuses to fire if Binance is reversing at entry.
# L23 forensics: 92/115 losses had price reversing after entry (0% WR).
TREND_ENHANCED      = os.getenv("TREND_ENHANCED", "false").strip().lower() in ("true","1","yes")
TREND_N_TICKS       = int(os.getenv("TREND_N_TICKS", "10"))
TREND_MOMENTUM_MIN  = float(os.getenv("TREND_MOMENTUM_MIN", "0.30"))
TREND_VELOCITY_MIN  = float(os.getenv("TREND_VELOCITY_MIN", "0.0000005"))

# ── Direction-based entry multipliers ─────────────────────────────────────────
# Set <1.0 to tighten the UP cap (UP historically weaker than DOWN).
UP_MAX_ENTRY_MULT   = float(os.getenv("UP_MAX_ENTRY_MULT",   "1.0"))
DOWN_MAX_ENTRY_MULT = float(os.getenv("DOWN_MAX_ENTRY_MULT", "1.0"))

# ── Entry price floor (skip near-zero junk tokens) ────────────────────────────
MIN_ENTRY_PRICE = float(os.getenv("MIN_ENTRY_PRICE", "0.008"))

# ── Regime gate config (requires ghost_regime_sentinel.py running) ────────────
TREND_REGIME_GATE      = os.getenv("TREND_REGIME_GATE", "false").strip().lower() in ("true","1","yes")
TREND_REGIME_VOL_MIN   = float(os.getenv("TREND_REGIME_VOL_MIN",  "0.0005"))
TREND_REGIME_MAX_AGE_S = int(os.getenv("TREND_REGIME_MAX_AGE_S", "600"))

# ── Hour-bias + brain state caches (module-level, filled lazily) ──────────────
_hour_bias_cache: Dict[str, Any] = {}
_brain_cache: Dict[str, Any] = {"ts": 0, "data": None}

# v9 Engine Change 1: Lazy orderbook fetch (filters → predict side → 1 fetch).
# Strategy-neutral in 95%+ of cases per analysis. Default ON.
# Set LAZY_ORDERBOOK=false to fall back to v7-style fetch-both-then-pick.
LAZY_ORDERBOOK = os.getenv("LAZY_ORDERBOOK", "true").strip().lower() in ("true","1","yes")

# ── Paper vs Live ──
PAPER_TRADE    = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true", "1", "yes")
PORTFOLIO_SIZE = float(os.getenv("PORTFOLIO_SIZE", "200.0"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(SCRIPT_DIR,
                "crypto_ghost_PAPER.db" if PAPER_TRADE else "crypto_ghost.db")

# ── Ghost Fill Guard (duplicate + ghost-fill prevention) ──────────────────────
try:
    from ghost_fill_guard import GhostFillGuard
    _ghost_guard = GhostFillGuard(
        guard_db=os.path.join(SCRIPT_DIR, "agent_reports.db"),
        trades_db=DB_PATH,
    )
    _GHOST_GUARD_OK = True
except Exception as _gge:
    print(f"[WARN] GhostFillGuard unavailable ({_gge}) — duplicate protection off")
    _GHOST_GUARD_OK = False
    _ghost_guard    = None

try:
    from ghost_oracle_agreement import allow_fire as _oracle_allow_fire
    _ORACLE_OK = True
except Exception as _ooe:
    print(f"[WARN] ghost_oracle_agreement unavailable ({_ooe}) — oracle gate off (fail-open)")
    _ORACLE_OK = False
    _oracle_allow_fire = None


# ─── DATABASE ─────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            tier        INTEGER,
            coin        TEXT,
            market_id   TEXT,
            token_id    TEXT,
            question    TEXT,
            entry_price REAL,
            size_usdc   REAL,
            order_id    TEXT,
            outcome     TEXT DEFAULT 'UP',
            status      TEXT DEFAULT 'open',
            pnl         REAL DEFAULT 0,
            closed_at   TEXT,
            strategy    TEXT DEFAULT 'lottery',
            brain_state TEXT,
            brain_score REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strike_log (
            ts          TEXT,
            coin        TEXT,
            strike      REAL,
            spot        REAL,
            fired       INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT,
            icon    TEXT,
            msg     TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_stats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT,
            markets_found   INTEGER DEFAULT 0,
            signals_passed  INTEGER DEFAULT 0,
            skip_ask        INTEGER DEFAULT 0,
            skip_liq        INTEGER DEFAULT 0,
            skip_certainty  INTEGER DEFAULT 0,
            skip_window     INTEGER DEFAULT 0,
            scan_ms         INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def log_scan_stats(markets, signals, skip_ask, skip_liq, skip_cert, skip_win, ms):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO scan_stats
            (ts, markets_found, signals_passed, skip_ask, skip_liq, skip_certainty, skip_window, scan_ms)
            VALUES (?,?,?,?,?,?,?,?)
        """, (datetime.now(timezone.utc).isoformat(),
              markets, signals, skip_ask, skip_liq, skip_cert, skip_win, ms))
        conn.execute("DELETE FROM scan_stats WHERE id NOT IN (SELECT id FROM scan_stats ORDER BY id DESC LIMIT 500)")
        conn.commit()
        conn.close()
    except Exception:
        pass

def write_stats_json():
    """Write stats to JSON file for web dashboard to read."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        conn  = sqlite3.connect(DB_PATH)
        wins    = conn.execute("SELECT COUNT(*) FROM trades WHERE status='won'").fetchone()[0]
        losses  = conn.execute("SELECT COUNT(*) FROM trades WHERE status='lost'").fetchone()[0]
        open_   = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
        pnl     = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status IN ('won','lost')").fetchone()[0]
        recent  = conn.execute("""
            SELECT ts, tier, coin, entry_price, size_usdc, pnl, status
            FROM trades ORDER BY id DESC LIMIT 10
        """).fetchall()

        total = wins + losses
        wr    = (wins / total * 100) if total > 0 else 0

        trades_list = []
        for row in recent:
            ts_, tier_, coin_, entry_, size_, p_, status_ = row
            trades_list.append({
                "time":   ts_[11:19] if ts_ else "",
                "tier":   tier_, "coin":   coin_,
                "entry":  f"@{entry_:.3f}" if entry_ else "",
                "size":   size_, "pnl": round(p_ or 0, 2), "status": status_,
            })

        # Full trades list for Live Trades tab (BEFORE closing connection)
        all_trades = conn.execute("""
            SELECT id, ts, tier, coin, question, entry_price, size_usdc, pnl, status
            FROM trades ORDER BY id DESC LIMIT 200
        """).fetchall()
        conn.close()

        all_trades_list = []
        for row in all_trades:
            all_trades_list.append({
                "id": row[0], "ts": row[1], "tier": row[2], "coin": row[3],
                "question": row[4], "entry_price": row[5], "size_usdc": row[6],
                "pnl": round(row[7] or 0, 2), "status": row[8]
            })

        data = {
            "wins": wins, "losses": losses, "open": open_,
            "total_pnl": round(pnl, 2), "win_rate": round(wr, 1),
            "recent_trades": trades_list, "all_trades": all_trades_list,
            "updated": datetime.now().strftime("%H:%M:%S")
        }
        script_dir = os.path.dirname(os.path.abspath(__file__))
        out_path = os.path.join(script_dir, "ghost_stats.json")
        with open(out_path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        # Print so we can see what's going wrong
        print(f"[write_stats_json ERROR] {type(e).__name__}: {e}")



def log_event(icon: str, msg: str):
    """Write an event to the DB so the dashboard can display it."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO events (ts, icon, msg) VALUES (?,?,?)",
                     (datetime.now().strftime("%H:%M:%S"), icon, msg))
        # Keep only last 100 events
        conn.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 100)")
        conn.commit()
        conn.close()
    except Exception:
        pass

def log_trade(tier, coin, market_id, token_id, question, entry, size, order_id,
              outcome="UP", trend_dev=None, secs_left=None, brain_state=None, brain_score=None):
    conn = sqlite3.connect(DB_PATH)
    for col in ["outcome TEXT DEFAULT 'UP'", "trend_dev_1h REAL", "secs_left REAL",
                "strategy TEXT DEFAULT 'lottery'", "brain_state TEXT", "brain_score REAL"]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass
    conn.execute("""
        INSERT INTO trades (ts,tier,coin,market_id,token_id,question,entry_price,size_usdc,order_id,outcome,trend_dev_1h,secs_left,strategy,brain_state,brain_score)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (datetime.now(timezone.utc).isoformat(), tier, coin,
          market_id, token_id, question[:120], entry, size, order_id, outcome, trend_dev, secs_left, "s4_reversal",
          brain_state, brain_score))
    conn.commit()
    conn.close()

def get_open_count():
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
    conn.close()
    return count

def get_daily_loss():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts LIKE ? AND status IN ('won','lost')",
        (f"{today}%",)
    ).fetchone()[0]
    conn.close()
    return -min(pnl, 0)  # Return loss as positive number

# ─── CRYPTO KEYWORD FILTERS ───────────────────────────────────────────────────
COINS = {
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ethereum"],
    "BNB": ["bnb", "binance coin"],
    "SOL": ["sol", "solana"],
    "XRP": ["xrp", "ripple"],
}

# ─── HOUR BIAS & BRAIN STATE HELPERS (V2 port) ────────────────────────────────

def get_hour_bias(coin: str) -> tuple:
    """
    Read MarketGhost DB for current UTC hour's UP win % for this coin.
    Returns (up_pct: float, samples: int). Cached 5 min per coin+hour.
    Returns (50.0, 0) if DB unavailable or too few samples.
    """
    try:
        now  = time.time()
        hour = datetime.utcnow().hour
        key  = f"{coin}-{hour:02d}"
        entry = _hour_bias_cache.get(key)
        if entry and now - entry["ts"] < 300:
            return entry["up_pct"], entry["samples"]
        mg_db = os.path.join(SCRIPT_DIR, "marketghost.db")
        if not os.path.exists(mg_db):
            return 50.0, 0
        con = sqlite3.connect(mg_db, timeout=3)
        row = con.execute("""
            SELECT COUNT(*) total,
                   SUM(CASE WHEN winner='UP' THEN 1 ELSE 0 END) up_wins
            FROM resolutions
            WHERE coin=?
              AND CAST(strftime('%H', end_utc) AS INTEGER) = ?
        """, (coin, hour)).fetchone()
        con.close()
        total   = row[0] or 0
        up_wins = row[1] or 0
        up_pct  = (up_wins / total * 100) if total > 0 else 50.0
        _hour_bias_cache[key] = {"up_pct": up_pct, "samples": total, "ts": now}
        return up_pct, total
    except Exception:
        return 50.0, 0


def read_brain_state(coin: str) -> Optional[dict]:
    """
    Read brain state for a coin from ghost_brain.json. Cached 5s.
    Returns dict {state, score, direction, dir_consistent} or None if stale/missing.
    """
    try:
        now = time.time()
        bf  = os.path.join(SCRIPT_DIR, "ghost_brain.json")
        if not os.path.exists(bf):
            return None
        if now - _brain_cache["ts"] < 5 and _brain_cache["data"] is not None:
            data = _brain_cache["data"]
        else:
            with open(bf, "r") as f:
                data = json.load(f)
            _brain_cache["ts"]   = now
            _brain_cache["data"] = data
        coins = data.get("coins", {}) if isinstance(data, dict) else {}
        info  = coins.get(coin.upper()) or coins.get(coin)
        if not info:
            return None
        last_real = info.get("last_real_ts") or 0
        if now - last_real > 90:
            return None
        return {
            "state":          info.get("state"),
            "score":          info.get("score"),
            "direction":      info.get("direction"),
            "dir_consistent": info.get("dir_consistent"),
        }
    except Exception:
        return None


def _check_regime_chop() -> Tuple[bool, str]:
    """
    Read latest regime_log from agent_reports.db. Returns (halt, reason).
    Halts when BOTH BTC and ETH 5m vol are below TREND_REGIME_VOL_MIN.
    Fails open (no halt) when sentinel data is missing/stale.
    """
    if not TREND_REGIME_GATE:
        return False, ""
    try:
        rdb = os.path.join(SCRIPT_DIR, "agent_reports.db")
        con = sqlite3.connect(rdb, timeout=2)
        row = con.execute(
            "SELECT ts, btc_vol_5m, eth_vol_5m FROM regime_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        con.close()
    except Exception:
        return False, ""
    if not row or row[1] is None or row[2] is None:
        return False, ""
    try:
        d = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - d).total_seconds() > TREND_REGIME_MAX_AGE_S:
            return False, ""
    except Exception:
        return False, ""
    btc_v, eth_v = row[1], row[2]
    if btc_v < TREND_REGIME_VOL_MIN and eth_v < TREND_REGIME_VOL_MIN:
        return True, (f"BTC vol={btc_v*100:.3f}% AND ETH vol={eth_v*100:.3f}% "
                      f"both < {TREND_REGIME_VOL_MIN*100:.2f}%")
    return False, ""


def get_coin(question: str) -> Optional[str]:
    q = question.lower()
    for coin, keywords in COINS.items():
        if any(k in q for k in keywords):
            return coin
    return None

def is_crypto_market(question: str) -> bool:
    return get_coin(question) is not None

def classify_tier(question: str) -> Optional[int]:
    """
    T1: 'Will BTC hit $X?' / 'Will BTC reach $X?' / 'Will BTC be above $X?'
    T2: 'BTC Up or Down [Hour]' / 'BTC Up or Down - [time]-[time]'
    T3: 'BTC Up or Down [4hr]' / 'BTC Up or Down [4-hour]'
    T4: 'BTC Up or Down [Day]' / 'BTC Up or Down on [date]'
    """
    q = question.lower()

    # T1: Strike crossing markets
    if any(kw in q for kw in ["hit $", "reach $", "hit or exceed", "above $", "cross $", "at $"]):
        if get_coin(question):
            return 1

    # T2: Hourly up/down (most specific first)
    if "up or down" in q or "up-or-down" in q:
        if any(x in q for x in ["[hour]", "[1hr]", "[1h]", "11:55pm", "12:00am",
                                  ":00pm", ":00am", "- hour", "hourly"]):
            return 2
        # Time-range pattern like "11:55PM-12:00AM"
        if re.search(r'\d{1,2}:\d{2}[ap]m.*\d{1,2}:\d{2}[ap]m', q):
            return 2

        # T3: 4-hour
        if any(x in q for x in ["[4hr]", "[4h]", "[4-hour]", "4 hour", "4hr"]):
            return 3

        # T4: Daily
        if any(x in q for x in ["[day]", "[daily]", "today", "on ", "april", "may",
                                  "january", "february", "march", "june", "july"]):
            return 4

        # Default up/down without clear tier → treat as T4
        return 4

    return None  # Not a crypto tier market

def parse_strike(question: str) -> Optional[float]:
    """
    Parse strike price from: 'Will BTC hit $67,000?', 'Will BTC reach $70k?'
    """
    match = re.search(r'\$(\d[\d,]*)([kK])?', question)
    if not match:
        return None
    num = float(match.group(1).replace(',', ''))
    if match.group(2):
        num *= 1000
    return num

def parse_mins_from_question(question: str, now: float) -> Optional[float]:
    """Parse estimated minutes to close from question text like
    'ETH above $2,050 on April 18?' or 'BTC Up or Down - April 16, 11:55PM-12:00AM ET'"""
    q = question.lower()
    months = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
              "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
    cur = datetime.fromtimestamp(now, tz=timezone.utc)
    for mname, mnum in months.items():
        if mname not in q:
            continue
        dm = re.search(rf'{mname}[^\d]*(\d{{1,2}})', q)
        if not dm:
            continue
        day = int(dm.group(1))
        for yr in [cur.year, cur.year + 1]:
            try:
                end  = datetime(yr, mnum, day, 23, 59, 59, tzinfo=timezone.utc)
                mins = (end.timestamp() - now) / 60
                if 1 < mins < 43200:  # 1 min to 30 days
                    return round(mins, 1)
            except Exception:
                pass
    return None


# ─── BINANCE PRICE TRACKER ────────────────────────────────────────────────────
class PriceTracker:
    """
    Maintains real-time prices and candle opens via Binance WebSocket.
    Also tracks which strikes have been crossed to avoid duplicate fires.
    """
    def __init__(self):
        self.prices: Dict[str, float] = {}          # e.g. {"BTC": 67500.0}
        self.opens_1h: Dict[str, float] = {}         # Hourly open prices
        self.opens_4h: Dict[str, float] = {}         # 4hr open prices
        self.opens_1d: Dict[str, float] = {}         # Daily open prices
        self.crossed_strikes: set = set()            # (coin, strike) already fired
        self.running = True
        self._ws_connected = False
        # Ring buffer of (ts, price) per coin — feeds momentum gate
        from collections import deque
        _N = TREND_N_TICKS
        self._tick_history: Dict[str, "deque"] = {
            c: deque(maxlen=_N) for c in ("BTC", "ETH", "BNB", "SOL", "XRP")
        }

    async def fetch_opens(self, session: aiohttp.ClientSession):
        """Fetch current candle opens from Binance REST"""
        symbols = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT",
                   "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
        for coin, sym in symbols.items():
            try:
                # 1h candle
                url = f"{BINANCE_REST}/klines"
                async with session.get(url, params={"symbol": sym, "interval": "1h", "limit": 1},
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data:
                            self.opens_1h[coin] = float(data[0][1])  # open price

                # 4h candle
                async with session.get(url, params={"symbol": sym, "interval": "4h", "limit": 1},
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data:
                            self.opens_4h[coin] = float(data[0][1])

                # 1d candle
                async with session.get(url, params={"symbol": sym, "interval": "1d", "limit": 1},
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data:
                            self.opens_1d[coin] = float(data[0][1])

            except Exception as e:
                print(f"[PriceTracker] Open fetch error for {coin}: {e}")

    def get_deviation(self, coin: str, timeframe: str) -> Optional[float]:
        """Return % deviation from candle open. Positive = above open."""
        price = self.prices.get(coin)
        opens = {"1h": self.opens_1h, "4h": self.opens_4h, "1d": self.opens_1d}
        open_price = opens.get(timeframe, {}).get(coin)
        if not price or not open_price:
            return None
        return (price - open_price) / open_price

    def get_momentum(self, coin: str, buying_up: bool) -> Tuple[float, float]:
        """
        Returns (momentum_pct, velocity_pct_per_sec).
        momentum_pct = fraction of intra-window deltas in the bet direction.
        velocity     = price range / price_first / dt (always positive if active).
        Neutral (0.5, 0.0) when fewer than 3 ticks — won't block early.
        """
        hist = list(self._tick_history.get(coin, []))
        if len(hist) < 3:
            return 0.5, 0.0
        matches = 0; moves = 0
        for i in range(1, len(hist)):
            p_prev = hist[i-1][1]; p_now = hist[i][1]
            if p_now == p_prev:
                continue
            moves += 1
            if (p_now > p_prev) == buying_up:
                matches += 1
        momentum_pct = matches / moves if moves else 0.5
        prices = [p for _, p in hist]
        ts_first = hist[0][0]; ts_last = hist[-1][0]
        dt = ts_last - ts_first
        p_first = prices[0]
        price_range = max(prices) - min(prices)
        velocity = (price_range / p_first / dt) if (dt > 0 and p_first > 0) else 0.0
        return momentum_pct, velocity

    def get_short_momentum(self, coin: str) -> Optional[float]:
        """Price change % across last 3 ticks. None if fewer than 3 ticks available.
        Positive = price rising, Negative = price falling."""
        hist = list(self._tick_history.get(coin, []))
        if len(hist) < 3:
            return None
        _, p_old = hist[-3]
        _, p_new = hist[-1]
        return (p_new - p_old) / p_old if p_old else None

    def get_hma_direction(self, coin: str, period: int = 9) -> Optional[str]:
        """Hull MA direction from tick history. Returns 'UP', 'DOWN', or None."""
        hist   = list(self._tick_history.get(coin, []))
        prices = [p for _, p in hist]
        if len(prices) < period * 2 + 2:
            return None
        half   = max(2, period // 2)
        sqrt_p = max(2, int(period ** 0.5))

        def wma(vals, k):
            if len(vals) < k: return None
            w = list(range(1, k + 1))
            return sum(v * w_ for v, w_ in zip(vals[-k:], w)) / sum(w)

        raw = []
        for i in range(period - 1, len(prices)):
            wh = wma(prices[:i+1], half)
            wf = wma(prices[:i+1], period)
            if wh is not None and wf is not None:
                raw.append(2 * wh - wf)
        if len(raw) < sqrt_p + 1:
            return None
        h_now  = wma(raw,      sqrt_p)
        h_prev = wma(raw[:-1], sqrt_p)
        if h_now is None or h_prev is None or h_now == h_prev:
            return None
        return 'UP' if h_now > h_prev else 'DOWN'

    def get_price_squeeze(self, coin: str, lookback: int = 6) -> Optional[float]:
        """Price range / mean over last N ticks. Lower = tighter squeeze."""
        hist   = list(self._tick_history.get(coin, []))
        prices = [p for _, p in hist[-lookback:]]
        if len(prices) < lookback:
            return None
        mean_p = sum(prices) / len(prices)
        return (max(prices) - min(prices)) / mean_p if mean_p > 0 else None

    def check_strike_crossing(self, coin: str, strike: float) -> bool:
        """Returns True if spot has crossed this strike (and not yet fired)."""
        price = self.prices.get(coin)
        if not price:
            return False
        key = (coin, strike)
        if key in self.crossed_strikes:
            return False  # Already fired for this strike
        # Crossed = spot is now >= strike (with tiny buffer)
        return price >= strike * 0.9995

    def mark_strike_fired(self, coin: str, strike: float):
        self.crossed_strikes.add((coin, strike))

    async def run_websocket(self):
        """Maintain Binance WebSocket connection with auto-reconnect"""
        coin_map = {"btcusdt": "BTC", "ethusdt": "ETH",
                    "bnbusdt": "BNB", "solusdt": "SOL", "xrpusdt": "XRP"}
        _backoff = 1
        while self.running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        BINANCE_WS_URL,
                        heartbeat=30,
                        timeout=aiohttp.ClientWSTimeout(ws_close=10)
                    ) as ws:
                        self._ws_connected = True
                        _backoff = 1  # reset on clean connect
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"[{ts}] Binance WebSocket connected ✓")
                        async for msg in ws:
                            if not self.running:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                    stream = data.get("stream", "")
                                    ticker = data.get("data", {})
                                    sym = stream.split("@")[0]
                                    coin = coin_map.get(sym)
                                    if coin and ticker:
                                        _new = float(ticker.get("c", 0))
                                        if _new > 0:
                                            self.prices[coin] = _new
                                            hist = self._tick_history.get(coin)
                                            if hist is not None:
                                                hist.append((time.time(), _new))
                                except Exception:
                                    pass
                            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                             aiohttp.WSMsgType.ERROR):
                                break
            except Exception as e:
                self._ws_connected = False
                print(f"[PriceTracker] WS error: {e} — reconnecting in {_backoff}s")
                await asyncio.sleep(_backoff)
                _backoff = min(_backoff * 2, 30)

# ─── S4 ORDER FLOW HELPERS ────────────────────────────────────────────────────
_cvd_cache: Dict[str, Any] = {}
_atr_cache: Dict[str, Any] = {}


async def get_rolling_cvd(session: aiohttp.ClientSession, coin: str
                          ) -> Tuple[Optional[float], Optional[float]]:
    """
    Rolling CVD from Binance 1-min klines.
    kline[5]=total_vol, kline[9]=taker_buy_vol (aggressive buys).
    Returns (cvd_sum, cvd_momentum).
      cvd_sum > 0  = net buy pressure over S4_CVD_CANDLES candles
      cvd_mom > 0  = pressure improving (recent half > older half)
    Cached _CVD_TTL seconds.
    """
    now = time.time()
    cached = _cvd_cache.get(coin)
    if cached and now - cached[0] < _CVD_TTL:
        return cached[1], cached[2]
    symbol = f"{coin}USDT"
    try:
        async with session.get(
            f"{BINANCE_REST}/klines",
            params={"symbol": symbol, "interval": "1m", "limit": S4_CVD_CANDLES + 1},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200: return None, None
            data = await r.json()
            if len(data) < S4_CVD_CANDLES: return None, None
        deltas = []
        for k in data[-S4_CVD_CANDLES:]:
            buy_vol  = float(k[9])
            sell_vol = float(k[5]) - buy_vol
            deltas.append(buy_vol - sell_vol)
        cvd_sum = sum(deltas)
        half    = S4_CVD_CANDLES // 2
        cvd_mom = sum(deltas[-half:]) - sum(deltas[:half])
        _cvd_cache[coin] = (now, cvd_sum, cvd_mom)
        return cvd_sum, cvd_mom
    except Exception:
        return None, None


async def get_atr_pct(session: aiohttp.ClientSession, coin: str) -> Optional[float]:
    """ATR(10) on 1-min klines as fraction of close price. Cached _ATR_TTL seconds."""
    now = time.time()
    cached = _atr_cache.get(coin)
    if cached and now - cached[0] < _ATR_TTL:
        return cached[1]
    symbol = f"{coin}USDT"
    try:
        async with session.get(
            f"{BINANCE_REST}/klines",
            params={"symbol": symbol, "interval": "1m", "limit": S4_ATR_PERIOD + 2},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200: return None
            data = await r.json()
            if len(data) < S4_ATR_PERIOD + 1: return None
        trs = []
        for i in range(1, len(data)):
            h = float(data[i][2]); l = float(data[i][3])
            pc = float(data[i-1][4])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr    = sum(trs[-S4_ATR_PERIOD:]) / S4_ATR_PERIOD
        close_ = float(data[-1][4])
        result = (atr / close_) if close_ > 0 else None
        _atr_cache[coin] = (now, result)
        return result
    except Exception:
        return None


async def get_orderbook_with_obi(session: aiohttp.ClientSession, token_id: str
                                 ) -> Tuple[Optional[float], Optional[float], float, Optional[float]]:
    """Fetch CLOB orderbook. Returns (best_bid, best_ask, liq, obi).
    OBI = (bid_vol - ask_vol) / total. Range -1.0 to +1.0."""
    try:
        async with session.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=aiohttp.ClientTimeout(total=6)
        ) as r:
            if r.status != 200: return None, None, 0.0, None
            data = await r.json()
        bids = sorted(data.get("bids", []), key=lambda x: float(x.get("price", 0)), reverse=True)
        asks = sorted(data.get("asks", []), key=lambda x: float(x.get("price", 1)))
        bid  = float(bids[0]["price"]) if bids else 0.0
        ask  = float(asks[0]["price"]) if asks else 0.0
        liq  = float(asks[0]["size"])  if asks else 0.0
        bid_vol = sum(float(b.get("size", 0)) for b in bids)
        ask_vol = sum(float(a.get("size", 0)) for a in asks)
        total   = bid_vol + ask_vol
        obi     = (bid_vol - ask_vol) / total if total > 0 else None
        return bid, ask, liq, obi
    except Exception:
        return None, None, 0.0, None


# ─── STRIKE MARKET PRELOADER ──────────────────────────────────────────────────
class StrikePreloader:
    """
    At startup, fetches all 'Will BTC hit $X?' markets.
    Keeps them ranked by distance from current price.
    When price crosses a strike, scanner fires immediately.
    """
    def __init__(self, tracker: PriceTracker):
        self.tracker = tracker
        self.strike_markets: List[dict] = []  # [{coin, strike, market_data, token_id}]
        self.last_refresh = 0
        self.REFRESH_EVERY = 300  # Reload every 5 minutes

    async def refresh(self, session: aiohttp.ClientSession):
        """Fetch ALL active markets and filter locally for strike markets.
        (Gamma API keyword param is unreliable — local filter is more accurate)
        """
        now = time.time()
        # v9.4: cache by TIMESTAMP only. Previously also required
        # self.strike_markets to be non-empty, which meant if zero strike
        # markets were active (the normal state) the bot re-fetched 300
        # gamma markets EVERY scan — 9 seconds of wasted HTTP per scan.
        if self.last_refresh > 0 and now - self.last_refresh < self.REFRESH_EVERY:
            return

        # v9.4: stamp cache time UP FRONT. Even if the gamma fetch fails or
        # finds zero markets, we won't retry for REFRESH_EVERY seconds.
        self.last_refresh = now

        STRIKE_HINTS = ["hit $", "reach $", "hit or exceed", "above $",
                        "cross $", "at $", "exceed $", "surpass $"]
        markets = []
        offset  = 0
        limit   = 200

        while True:
            try:
                params = {
                    "active": "true", "archived": "false", "closed": "false",
                    "limit": limit, "offset": offset,
                    "order": "endDateIso", "ascending": "true"
                }
                async with session.get(f"{GAMMA_API}/markets", params=params,
                                       timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status != 200:
                        break
                    data = await r.json()
                    if not data:
                        break

                    for m in data:
                        q    = m.get("question", "")
                        qlow = q.lower()
                        coin   = get_coin(q)
                        strike = parse_strike(q)
                        if not coin or not strike:
                            continue
                        if not any(h in qlow for h in STRIKE_HINTS):
                            continue
                        end_ts = m.get("endDateIso", "")
                        if end_ts:
                            try:
                                end  = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                                if (end.timestamp() - now) / 60 < 1:
                                    continue
                            except Exception:
                                pass
                        tokens   = m.get("tokens", []) or []
                        token_id = None
                        for t in tokens:
                            if isinstance(t, dict) and t.get("outcome", "").upper() in ("YES","UP","HIGHER","ABOVE"):
                                token_id = t.get("token_id") or t.get("id")
                                break
                        if not token_id and tokens:
                            tok      = tokens[0]
                            token_id = tok.get("token_id") if isinstance(tok, dict) else tok
                        if token_id:
                            markets.append({
                                "coin": coin, "strike": strike,
                                "market": m, "token_id": token_id,
                                "question": q[:80]
                            })

                    if len(data) < limit:
                        break
                    offset += limit
                    # Cap at 3 pages (300 markets) to avoid blocking for 52 seconds
                    if offset >= 300:
                        break

            except Exception as e:
                print(f"[StrikePreloader] Error: {e}")
                break

        # Deduplicate by token_id
        seen, unique = set(), []
        for m in markets:
            if m["token_id"] not in seen:
                seen.add(m["token_id"])
                unique.append(m)

        self.strike_markets = unique
        self.last_refresh   = now
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Strike markets loaded: {len(self.strike_markets)} "
              f"({sum(1 for m in unique if m['coin']=='BTC')} BTC, "
              f"{sum(1 for m in unique if m['coin']=='ETH')} ETH, "
              f"{sum(1 for m in unique if m['coin']=='BNB')} BNB)")

    def get_ready_strikes(self) -> List[dict]:
        """Return strike markets where spot has crossed the strike."""
        ready = []
        for sm in self.strike_markets:
            if self.tracker.check_strike_crossing(sm["coin"], sm["strike"]):
                ready.append(sm)
        return ready

# ─── ORDERBOOK UTILS ──────────────────────────────────────────────────────────
# v9 Fix #6: TTL cache for orderbooks. Pre-warm during the 60s before a
# market enters its fire window, so when the bot decides to fire, the
# orderbook is already in hand. Drops T2 fire latency ~300ms → ~50ms.
_orderbook_cache: Dict[str, Tuple[float, dict]] = {}  # token_id → (ts, book)
_ORDERBOOK_CACHE_TTL = 0.5  # 500ms — fresh enough for 3-min entry window
PREWARM_ORDERBOOKS = os.getenv("PREWARM_ORDERBOOKS", "true").strip().lower() in ("true","1","yes")


async def _fetch_orderbook_raw(session: aiohttp.ClientSession,
                               token_id: str) -> Optional[dict]:
    """Live HTTP fetch — no cache. Used for T1 (latency-critical) path."""
    try:
        url = f"{POLYMARKET_HOST}/book"
        async with session.get(url, params={"token_id": token_id},
                               timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        pass
    return None


async def get_orderbook(session: aiohttp.ClientSession,
                        token_id: str,
                        max_age_ms: int = 500) -> Optional[dict]:
    """
    Cache-aware orderbook fetch. Returns a cached book if it's fresher
    than `max_age_ms`; otherwise hits the API and caches the result.
    Set max_age_ms=0 to bypass the cache (T1 strike scan does this).
    """
    if max_age_ms > 0 and PREWARM_ORDERBOOKS:
        cached = _orderbook_cache.get(token_id)
        if cached:
            ts, book = cached
            if (time.time() - ts) * 1000 < max_age_ms and book:
                return book
    book = await _fetch_orderbook_raw(session, token_id)
    if book is not None:
        _orderbook_cache[token_id] = (time.time(), book)
    return book


async def prewarm_orderbook(session: aiohttp.ClientSession, token_id: str):
    """Background task: fetch + cache an orderbook for a market about
    to enter its fire window. Silent on failure."""
    try:
        await get_orderbook(session, token_id, max_age_ms=0)
    except Exception:
        pass


# ─── v10: WebSocket orderbook subscription ────────────────────────────────────
# Subscribe to Polymarket CLOB live order-book stream. When a market the
# bot is tracking has its orderbook update, PM pushes the new state to us
# in ~50ms. We write it straight into _orderbook_cache; the next scan tick
# reads cache and fires immediately. Net effect: ~10x faster reaction time
# vs the 1-second polling loop. Falls back to HTTP polling if WS fails.

WS_ENABLED = os.getenv("WS_ENABLED", "true").strip().lower() in ("true", "1", "yes")
WS_URL     = os.getenv("WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")


class _OrderbookWS:
    """Persistent WebSocket client. Auto-reconnects. Subscribes to
    token_ids on demand. Pushes book snapshots into _orderbook_cache."""

    def __init__(self):
        self.subscribed: set = set()  # token_ids we've asked PM to push
        self.connected = False
        self.ws       = None
        self._sess    = None
        self.task     = None
        self.msgs_received = 0
        self.last_msg_ts   = 0.0

    async def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        backoff = 1
        while True:
            try:
                self._sess = aiohttp.ClientSession()
                async with self._sess.ws_connect(
                    WS_URL,
                    heartbeat=20,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    print(f"[WS] Connected to {WS_URL}")
                    backoff = 1
                    # Re-subscribe to everything we had
                    if self.subscribed:
                        await self._send_subscribe(list(self.subscribed))
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_msg(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.CLOSING,
                                          aiohttp.WSMsgType.ERROR):
                            break
            except Exception as e:
                print(f"[WS] connection error: {type(e).__name__}: {e}")
            finally:
                self.connected = False
                self.ws = None
                if self._sess and not self._sess.closed:
                    await self._sess.close()
                self._sess = None
            # Reconnect with exponential backoff (capped)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def subscribe(self, token_ids: List[str]):
        """Request push updates for these token_ids. Idempotent."""
        new = [t for t in token_ids if t and t not in self.subscribed]
        if not new:
            return
        self.subscribed.update(new)
        if self.connected and self.ws and not self.ws.closed:
            await self._send_subscribe(new)

    async def _send_subscribe(self, token_ids: List[str]):
        # Polymarket CLOB market channel subscribe payload.
        # Try the documented format; if PM rejects, future messages will
        # surface the error and we'll iterate.
        payload = {"type": "MARKET", "assets_ids": token_ids}
        try:
            await self.ws.send_json(payload)
            print(f"[WS] subscribed to {len(token_ids)} token_ids "
                  f"(total: {len(self.subscribed)})")
        except Exception as e:
            print(f"[WS] subscribe send failed: {e}")

    def _handle_msg(self, raw: str):
        """Convert PM book / price_change events into _orderbook_cache rows."""
        self.msgs_received += 1
        self.last_msg_ts = time.time()
        try:
            obj = json.loads(raw)
        except Exception:
            return

        events = obj if isinstance(obj, list) else [obj]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            ev_type = (ev.get("event_type") or ev.get("type") or "").lower()
            asset_id = ev.get("asset_id") or ev.get("asset") or ev.get("token_id")
            if not asset_id:
                continue

            if ev_type == "book":
                # Full orderbook snapshot
                book = {
                    "bids": ev.get("bids", []) or [],
                    "asks": ev.get("asks", []) or [],
                }
                _orderbook_cache[str(asset_id)] = (time.time(), book)
            elif ev_type in ("price_change", "tick_size_change"):
                # Incremental update — invalidate cache, next scan refetches
                # via HTTP (still faster than polling because HTTP only when
                # the book actually changed)
                cur = _orderbook_cache.get(str(asset_id))
                if cur:
                    _orderbook_cache[str(asset_id)] = (0.0, cur[1])


WS_CLIENT = _OrderbookWS()

def best_ask_info(book: dict) -> Tuple[Optional[float], float]:
    """Returns (best_ask_price, liquidity_usd)"""
    asks = book.get("asks", [])
    if not asks:
        return None, 0.0
    sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 1)))
    best = sorted_asks[0]
    price = float(best.get("price", 1))
    size  = float(best.get("size", 0))
    return price, price * size

# ─── TIME WINDOW CHECKS ───────────────────────────────────────────────────────
def minutes_to_close(end_ts_str: str) -> Optional[float]:
    """Return minutes until market closes. Negative = already closed."""
    try:
        end = datetime.fromisoformat(end_ts_str.replace("Z", "+00:00"))
        delta = (end - datetime.now(timezone.utc)).total_seconds() / 60
        return delta
    except Exception:
        return None

def in_entry_window(tier: int, mins_left: float) -> bool:
    """Is this the right time window to enter this tier?"""
    if mins_left <= 0:
        return False
    if tier == 1:
        return True        # Strike markets: fire anytime
    if tier == 2:
        return 0 < mins_left <= T2_WINDOW_MIN  # Final 3 min only
    if tier == 3:
        return mins_left <= T3_WINDOW_MIN
    if tier == 4:
        return mins_left <= T4_WINDOW_MIN
    return False

# ─── MARKET FETCHER ───────────────────────────────────────────────────────────
def parse_mins_left_from_title(title: str) -> Optional[float]:
    """
    Parse minutes until close from title like:
    'Bitcoin Up or Down - April 16, 1:10AM-1:15AM ET'
    Returns None if can't parse, negative if already closed.
    """
    # Find end time — the second time in a range like "1:10AM-1:15AM ET"
    match = re.search(r'\d{1,2}:\d{2}[AP]M-(\d{1,2}:\d{2}[AP]M)\s*ET', title, re.IGNORECASE)
    if not match:
        return None
    end_str = match.group(1)  # e.g. "1:15AM"

    # Find date like "April 16,"
    date_match = re.search(r'([A-Za-z]+)\s+(\d{1,2}),', title)

    try:
        now_utc    = datetime.now(timezone.utc)
        # April 2026 is EDT = UTC-4
        now_et     = now_utc - timedelta(hours=4)
        now_et_naive = now_et.replace(tzinfo=None)

        end_time = datetime.strptime(end_str.upper(), "%I:%M%p")

        if date_match:
            month = datetime.strptime(date_match.group(1), "%B").month
            day   = int(date_match.group(2))
            end_naive = now_et_naive.replace(
                month=month, day=day,
                hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0
            )
        else:
            end_naive = now_et_naive.replace(
                hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0
            )
            if end_naive < now_et_naive - timedelta(minutes=5):
                end_naive += timedelta(days=1)

        return round((end_naive - now_et_naive).total_seconds() / 60, 1)
    except Exception:
        return None


async def build_expected_slugs() -> list:
    """
    Build Polymarket slugs for upcoming crypto Up/Down markets.

    Real PM slug format (verified):
        {coin}-updown-{interval}-{unix_end_timestamp}

    Examples:
        btc-updown-5m-1777251900   = BTC Up/Down ending 9:05 PM ET (5-min)
        eth-updown-15m-1777251600  = ETH Up/Down ending 9:00 PM ET (15-min)

    5-min end timestamps are multiples of 300 sec.
    15-min end timestamps are multiples of 900 sec.

    Returns list of (coin_label, slug) tuples to try. Covers ~20 minutes
    forward + 1 window back per interval.
    """
    now_ts = int(time.time())
    coins = [
        ("BTC", "btc"),
        ("ETH", "eth"),
    ]

    slugs = []

    # 5-minute markets — previous + next 5 windows (covers 25 min forward)
    curr_5m = ((now_ts // 300) + 1) * 300
    prev_5m = curr_5m - 300
    for i in range(6):  # prev, curr, +1, +2, +3, +4
        end_ts = prev_5m + i * 300
        for label, coin_slug in coins:
            slugs.append((label, f"{coin_slug}-updown-5m-{end_ts}"))

    # 15-minute markets — previous + next 4 windows (covers 60 min forward)
    curr_15m = ((now_ts // 900) + 1) * 900
    prev_15m = curr_15m - 900
    for i in range(5):
        end_ts = prev_15m + i * 900
        for label, coin_slug in coins:
            slugs.append((label, f"{coin_slug}-updown-15m-{end_ts}"))

    return slugs


_slug_diag_logged = {"sample": False}

async def direct_scan_updown(session: aiohttp.ClientSession) -> Dict[str, dict]:
    """
    Fetch crypto Up/Down markets via direct slug lookup against gamma API.
    Tries every constructed slug (5min/hourly/4hr/daily for all 4 coins).
    Concurrent fetches keep the loop fast.
    """
    HEADERS  = {"User-Agent": "Mozilla/5.0"}
    found    = {}
    now      = time.time()

    slugs = await build_expected_slugs()

    async def _try_slug(coin_label, slug):
        try:
            async with session.get(f"{GAMMA_API}/markets",
                params={"slug": slug},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                items = data if isinstance(data, list) else []
                if not items:
                    return None
                return (coin_label, slug, items[0])
        except Exception:
            return None

    # Fire all slug lookups concurrently (cap concurrency reasonably)
    sem = asyncio.Semaphore(20)
    async def _bounded(c, s):
        async with sem:
            return await _try_slug(c, s)

    results = await asyncio.gather(*[_bounded(c, s) for c, s in slugs],
                                   return_exceptions=False)

    hit_count = 0
    for res in results:
        if not res:
            continue
        coin_label, slug, m = res
        hit_count += 1
        q   = m.get("question", "")
        end = m.get("endDate") or m.get("endDateIso", "")
        if not end or not is_crypto_market(q):
            continue
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            mins   = (end_dt.timestamp() - now) / 60
            if mins < 0 or mins > 1500:  # within 25 hours
                continue
            mid = m.get("id", "") or slug
            if mid in found:
                continue
            tokens = m.get("tokens", []) or []
            cti    = m.get("clobTokenIds") or m.get("clob_token_ids") or []
            if isinstance(cti, str):
                try:
                    import json as _j
                    cti = _j.loads(cti)
                except Exception:
                    cti = []
            outs = m.get("outcomes") or m.get("outcome_names") or []
            if isinstance(outs, str):
                try:
                    import json as _j
                    outs = _j.loads(outs)
                except Exception:
                    outs = []
            if not tokens and cti:
                tokens = []
                for i, tid in enumerate(cti):
                    out = outs[i] if i < len(outs) else ("UP" if i == 0 else "DOWN")
                    tokens.append({"token_id": str(tid), "outcome": str(out).upper()})
            primary_token = None
            for t in tokens:
                if isinstance(t, dict):
                    o = (t.get("outcome") or "").upper()
                    if o in ("UP", "YES", "HIGHER", "ABOVE"):
                        primary_token = t.get("token_id") or t.get("id")
                        break
            if not primary_token and tokens:
                t = tokens[0]
                primary_token = t.get("token_id") if isinstance(t, dict) else str(t)
            if primary_token:
                found[mid] = {
                    "question":   q,
                    "id":         mid,
                    "tokens":     tokens or [{"token_id": primary_token, "outcome": "UP"}],
                    "_mins_left": round(mins, 1),
                    "endDateIso": end,
                }
        except Exception:
            continue

    if found:
        ts = datetime.now().strftime("%H:%M:%S")
        sample = next(iter(found.values()))
        print(f"[{ts}] Slug scan: {hit_count}/{len(slugs)} slugs matched "
              f"→ {len(found)} valid markets. e.g. \"{sample['question'][:60]}\" "
              f"({sample['_mins_left']:.0f}m left)")
    elif hit_count == 0 and not _slug_diag_logged["sample"]:
        # First failure — log a sample of slugs we tried so the user can verify
        ts = datetime.now().strftime("%H:%M:%S")
        sample_slugs = [f"{s}" for _, s in slugs[:5]]
        print(f"[{ts}] [WARN] Slug scan: 0 of {len(slugs)} slugs returned data. "
              f"Sample tried: {sample_slugs}")
        _slug_diag_logged["sample"] = True

    if found:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Direct scan found {len(found)} markets via slug lookup")

    return found


# ── Engine v8 Change 2: Market discovery cache ─────────────────────────
# Markets are created every 5 minutes. Caching the discovery list for
# 30s eliminates redundant gamma + slug HTTP calls every 3 seconds.
# mins_to_close is recomputed FRESH on every scan from the cached
# token_markets, so timing stays accurate to the second.
_market_cache = {"token_markets": None, "ts": 0.0}
_MARKET_CACHE_TTL = 30.0  # seconds


async def _discover_crypto_markets(session: aiohttp.ClientSession) -> Dict[str, dict]:
    """
    Run the actual discovery (gamma sweep + slug scan + optional wallet).
    Returns the raw token_markets dict. Heavy HTTP work happens here.
    """
    HEADERS = {"User-Agent": "Mozilla/5.0"}
    token_markets: Dict[str, dict] = {}
    now = time.time()

    # ── Source 1 (optional): Your own wallet's recent activity ───────────────
    # Off by default. Only your FUNDER_ADDRESS — never a third party.
    use_wallet = os.getenv("WALLET_DISCOVERY", "false").strip().lower() in ("true", "1", "yes")
    if use_wallet and FUNDER_ADDRESS:
        try:
            url = (f"{DATA_API}/activity?user={FUNDER_ADDRESS}"
                   f"&type=TRADE&limit=100&sortBy=TIMESTAMP&sortDirection=DESC")
            async with session.get(url, headers=HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    trades = await r.json()
                    for t in (trades if isinstance(trades, list) else []):
                        age    = now - float(t.get("timestamp", 0))
                        if age > 1800:
                            continue
                        title  = t.get("title", "")
                        asset  = str(t.get("asset", ""))
                        cid    = t.get("conditionId", "")
                        outcome= t.get("outcome", "")
                        if not asset or not is_crypto_market(title):
                            continue
                        if asset not in token_markets:
                            token_markets[asset] = {
                                "question":  title,
                                "id":        cid,
                                "tokens":    [{"token_id": asset, "outcome": outcome}],
                                "_age_secs": age,
                            }
        except Exception as e:
            print(f"[WARN] Activity ({FUNDER_ADDRESS[:10]}...): {e}")

    # ── Source 2 (PRIMARY): Polymarket gamma /markets active sweep ───────────
    # Order matters: try queries that prioritize SOONEST-ending markets
    # first so we surface short-lived 5-min / 1-hr / 4-hr crypto markets.
    sweep_attempts = [
        # Sort ascending by end date — soonest-ending first
        f"{GAMMA_API}/markets?closed=false&order=endDate&ascending=true&limit=500",
        f"{GAMMA_API}/markets?active=true&closed=false&order=endDate&ascending=true&limit=500",
        # Sort by end_date_min to filter to upcoming markets only
        f"{GAMMA_API}/markets?end_date_min=now&closed=false&limit=500",
        # Crypto-specific tag (21)
        f"{GAMMA_API}/markets?tag_id=21&closed=false&order=endDate&ascending=true&limit=500",
        # Last resort: unsorted active
        f"{GAMMA_API}/markets?active=true&closed=false&limit=500",
    ]
    gamma_added = 0
    for sweep_url in sweep_attempts:
        try:
            async with session.get(sweep_url, headers=HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    continue
                data = await r.json()
                items = data if isinstance(data, list) else data.get("data", [])
                if not items:
                    continue
                before = len(token_markets)
                for m in items:
                    q = m.get("question", "")
                    if not is_crypto_market(q):
                        continue
                    if m.get("closed") is True:
                        continue
                    end = m.get("endDate") or m.get("endDateIso", "")
                    if not end:
                        continue
                    # Parse tokens / clob_token_ids
                    tokens = m.get("tokens", []) or []
                    cti    = m.get("clobTokenIds") or m.get("clob_token_ids") or []
                    if isinstance(cti, str):
                        try:
                            import json as _j
                            cti = _j.loads(cti)
                        except Exception:
                            cti = []
                    outs = m.get("outcomes") or m.get("outcome_names") or []
                    if isinstance(outs, str):
                        try:
                            import json as _j
                            outs = _j.loads(outs)
                        except Exception:
                            outs = []
                    if not tokens and cti:
                        tokens = []
                        for i, tid in enumerate(cti):
                            out = outs[i] if i < len(outs) else ("UP" if i == 0 else "DOWN")
                            tokens.append({"token_id": str(tid), "outcome": str(out).upper()})
                    if not tokens:
                        continue
                    primary_token = None
                    for t in tokens:
                        if isinstance(t, dict):
                            o = (t.get("outcome") or "").upper()
                            if o in ("UP", "YES", "HIGHER", "ABOVE"):
                                primary_token = t.get("token_id") or t.get("id")
                                break
                    if not primary_token:
                        primary_token = (tokens[0].get("token_id")
                                         if isinstance(tokens[0], dict)
                                         else str(tokens[0]))
                    if primary_token in token_markets:
                        continue
                    token_markets[primary_token] = {
                        "question":   q,
                        "id":         m.get("id", "") or m.get("conditionId", ""),
                        "tokens":     tokens,
                        "endDateIso": end,
                    }
                added = len(token_markets) - before
                gamma_added += added
                if added > 0:
                    # First good attempt — stop trying others
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] Gamma sweep: {len(items)} active markets, "
                          f"{added} crypto matched ({sweep_url.split('?')[1][:50]})")
                    break
        except Exception as e:
            print(f"[WARN] Gamma sweep ({sweep_url[:60]}): {e}")
            continue
    if gamma_added == 0:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [WARN] All gamma sweep variants returned 0 crypto markets")

    # ── Source 3 (BACKUP): Slug-builder fallback (in case gamma sweep misses) ─
    try:
        direct = await direct_scan_updown(session)
        for mid, m in direct.items():
            token_id = m["tokens"][0]["token_id"] if m["tokens"] else None
            if token_id and token_id not in token_markets:
                token_markets[token_id] = m
    except Exception as e:
        print(f"[WARN] Slug scan: {e}")

    return token_markets


async def fetch_crypto_markets(session: aiohttp.ClientSession) -> List[dict]:
    """
    Public entry: returns markets list ready for check_market().
    Discovery is cached for 30 seconds; mins_to_close is always fresh.
    """
    global _market_cache
    now_cache = time.time()

    # Refresh discovery if cache is stale or empty
    if (_market_cache["token_markets"] is None
            or now_cache - _market_cache["ts"] > _MARKET_CACHE_TTL):
        _market_cache["token_markets"] = await _discover_crypto_markets(session)
        _market_cache["ts"] = now_cache
        cache_age = "fresh"
    else:
        cache_age = f"cached ({now_cache - _market_cache['ts']:.0f}s old)"

    token_markets = _market_cache["token_markets"] or {}

    # ── Filter to markets still open and within scan horizon ─────────────────
    # Runs fresh every scan — recomputes mins_to_close from cached endDates.
    now_ts = time.time()
    markets = []
    drop_no_end = 0
    drop_past   = 0
    drop_far    = 0
    bucket = {"<60m": 0, "<4h": 0, "<24h": 0, "<48h": 0, ">48h": 0}
    for token_id, m in token_markets.items():
        # Priority 1: Use exact endDateIso timestamp (most accurate)
        end_iso = m.get("endDateIso") or m.get("endDate","")
        if end_iso:
            try:
                end_dt   = datetime.fromisoformat(end_iso.replace("Z","+00:00"))
                secs_left = end_dt.timestamp() - now_ts
                mins      = secs_left / 60
                m["_secs_left"] = secs_left
                m["_mins_left"] = round(mins, 2)
            except Exception:
                mins = parse_mins_left_from_title(m.get("question",""))
                m["_mins_left"] = round(mins, 2) if mins else None
                m["_secs_left"] = mins * 60 if mins else None
        else:
            # Fallback: parse from title
            mins = parse_mins_left_from_title(m.get("question",""))
            m["_mins_left"] = round(mins, 2) if mins else None
            m["_secs_left"] = mins * 60 if mins else None

        mins = m.get("_mins_left")
        if mins is None:
            drop_no_end += 1
            continue
        if mins <= 0:
            drop_past += 1
            continue
        # Bucket the mins for diagnostics
        if mins < 60:    bucket["<60m"] += 1
        elif mins < 240: bucket["<4h"]  += 1
        elif mins < 1440: bucket["<24h"] += 1
        elif mins < 2880: bucket["<48h"] += 1
        else:             bucket[">48h"] += 1
        # Cap at 48 hours (covers daily markets + some buffer). Per-tier
        # in_entry_window() inside check_market narrows further.
        if mins > 2880:
            drop_far += 1
            continue
        markets.append(m)

    ts_now = datetime.now().strftime("%H:%M:%S")
    if token_markets:
        print(f"[{ts_now}] Discovery: {len(token_markets)} crypto found → "
              f"{len(markets)} in window  "
              f"[buckets {bucket}, dropped: no_end={drop_no_end} past={drop_past} >48h={drop_far}]")

    return markets



# ─── MAIN SCANNER CLASS ───────────────────────────────────────────────────────
class CryptoGhostScanner:
    def __init__(self):
        if not PAPER_TRADE and (not PRIVATE_KEY or not FUNDER_ADDRESS):
            print("\nERROR: Missing credentials in .env\nAdd PAPER_TRADE=true for paper trading\n")
            sys.exit(1)

        if not PAPER_TRADE:
            _creds = ApiCreds(
                api_key=os.getenv("API_KEY", ""),
                api_secret=os.getenv("API_SECRET", ""),
                api_passphrase=os.getenv("API_PASSPHRASE", ""),
            )
            self.client = ClobClient(
                host=POLYMARKET_HOST, key=PRIVATE_KEY,
                chain_id=137,
                funder=os.getenv("POLYMARKET_PROXY_ADDRESS", ""),
                creds=_creds, signature_type=3
            )
        else:
            self.client = None

        self.tracker        = PriceTracker()
        self.preloader      = StrikePreloader(self.tracker)
        self.open_positions: Dict[str, dict] = {}
        self.session_pnl    = 0.0
        self.session_high   = 0.0
        self.trades_fired   = 0
        self.scan_count     = 0
        self.running        = True
        # Dedup: log each (token_id, side) signal once per close window.
        # Cleared every 5 min to prevent unbounded growth.
        self._signaled: set = set()
        self._signaled_lastclear: float = 0.0
        init_db()
        self._print_banner()

    def _print_banner(self):
        wallet = FUNDER_ADDRESS[:12] + "..." if FUNDER_ADDRESS else "NOT SET"
        mode   = "** PAPER MODE — NO REAL MONEY **" if PAPER_TRADE else "!! LIVE MODE — REAL MONEY !!"
        print(f"""
╔══════════════════════════════════════════════════════╗
║     CRYPTO GHOST SCANNER 4 -- EARLY ZONE PREDATOR    ║
║  {mode:<52s}║
╠══════════════════════════════════════════════════════╣
║  S4 config (hardcoded):                              ║
║    Entry zone: ${S4_ENTRY_MIN:.2f}-${S4_ENTRY_MAX:.2f}  Window: {S4_MIN_MINS}-{S4_MAX_MINS} min   ║
║    Pillars: CVD(5x1m) + ATR-squeeze + HMA(9)         ║
║    Spread filter: <${S4_MAX_SPREAD:.2f}  Tier: T{S4_TIER}            ║
╠══════════════════════════════════════════════════════╣
║  Wallet: {wallet:<44s}║
║  Max loss: ${MAX_DAILY_LOSS:.0f}/day                                ║
║  DB: {os.path.basename(DB_PATH):<47s}║
╚══════════════════════════════════════════════════════╝
        """)

    def kill_check(self) -> Optional[str]:
        daily = get_daily_loss()
        if daily >= MAX_DAILY_LOSS:
            return f"Daily loss limit: ${daily:.2f}"
        return None

    def get_size(self, tier: int, coin: str = "") -> float:
        # T2 has per-coin sizing overrides (data-backed). Other tiers
        # use a single size since they fire so rarely.
        if tier == 2 and coin:
            return T2_SIZE_PER_COIN.get(coin.upper(), T2_SIZE)
        return {1: T1_SIZE, 2: T2_SIZE, 3: T3_SIZE, 4: T4_SIZE}.get(tier, 2.0)

    def get_max_entry(self, tier: int, outcome: str = "UP") -> float:
        base = {1: T1_MAX_ENTRY, 2: T2_MAX_ENTRY,
                3: T3_MAX_ENTRY, 4: T4_MAX_ENTRY}.get(tier, 0.15)
        if tier == 2:
            buying_up = outcome.upper() in ("UP", "YES", "HIGHER", "ABOVE")
            mult = UP_MAX_ENTRY_MULT if buying_up else DOWN_MAX_ENTRY_MULT
            return base * mult
        return base

    async def fire_trade(self, tier: int, coin: str, token_id: str,
                         market: dict, ask: float, outcome: str = "UP",
                         trend_dev: float = None, secs_left: float = None) -> bool:
        if token_id in self.open_positions:
            return False
        if len(self.open_positions) >= MAX_OPEN_POSITIONS:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] SKIP: max positions ({MAX_OPEN_POSITIONS})")
            return False
        if self.kill_check():
            return False

        # ── Entry price floor ────────────────────────────────────────────────
        if ask < MIN_ENTRY_PRICE:
            return False

        # ── Ghost fill guard ─────────────────────────────────────────────────
        if _GHOST_GUARD_OK and not _ghost_guard.can_fire(token_id, coin):
            return False

        # ── Oracle agreement gate (Coinbase direction vs Binance prediction) ──
        if _ORACLE_OK and not _oracle_allow_fire(coin, outcome):
            return False

        # ── Momentum gate (TREND_ENHANCED) ────────────────────────────────────
        if TREND_ENHANCED and tier == 2:
            buying_up = outcome.upper() in ("UP", "YES", "HIGHER", "ABOVE")
            mom, vel = self.tracker.get_momentum(coin, buying_up)
            if mom < TREND_MOMENTUM_MIN or vel < TREND_VELOCITY_MIN:
                return False

        size     = self.get_size(tier, coin)
        question = market.get("question", "")[:70]

        try:
            ask    = round(round(ask / 0.001) * 0.001, 4)  # tick-round to $0.001 grid
            shares = max(round(size / ask, 4), 5.0)        # Polymarket CLOB minimum 5 shares

            if PAPER_TRADE:
                order_id = f"PAPER-{int(time.time()*1000)}"
            else:
                order_args = OrderArgs(token_id=token_id, price=ask, size=shares, side=BUY)
                resp       = self.client.create_and_post_order(order_args, order_type="FAK")
                order_id   = (resp.get("orderID") or resp.get("orderId") or
                              resp.get("id") or "") if resp else ""

            if order_id:
                if _GHOST_GUARD_OK:
                    try:
                        _ghost_guard.register_order(
                            order_id=order_id, token_id=token_id, coin=coin,
                            tier=f"T{tier}", ask_price=ask, size_usdc=size,
                        )
                    except Exception as _gge:
                        print(f"[GhostGuard] register_order failed (non-fatal): {_gge}")
                self.open_positions[token_id] = {
                    "tier": tier, "coin": coin, "question": question,
                    "entry": ask, "size": size,
                    "order_id": order_id, "ts": time.time()
                }
                brain   = read_brain_state(coin)
                b_state = brain.get("state") if brain else None
                b_score = brain.get("score") if brain else None
                db_tier  = S4_TIER  # always T8
                log_trade(db_tier, coin, market.get("id",""),
                          token_id, question, ask, size, order_id, outcome, trend_dev, secs_left,
                          brain_state=b_state, brain_score=b_score)
                self.trades_fired += 1
                payout   = round(size / ask - size, 2)
                ts       = datetime.now().strftime("%H:%M:%S")
                mode_tag = "[PAPER]" if PAPER_TRADE else "[LIVE] "
                tier_names = {1:"STRIKE",2:"HOURLY",3:"4HR",4:"DAILY",7:"S3-PRECISION",8:"S4-EARLY"}
                print(f"[{ts}] {mode_tag} T{db_tier} {tier_names.get(db_tier,'')} | "
                      f"{coin} {outcome} @ ${ask:.3f} | Size:${size} -> +${payout:.2f} | '{question[:40]}'")
                log_event("🎯", f"T{db_tier} S4 {coin} {outcome} BUY @ ${ask:.3f} | +${payout:.2f} | {question}")
                write_stats_json()
                # Telegram alert
                try:
                    from telegram_alerts import send_alert
                    asyncio.create_task(send_alert(
                        f"🎯 <b>S4 T8 FIRE</b>\n"
                        f"<b>{coin}</b> {outcome} @ <b>${ask:.3f}</b>\n"
                        f"Size: ${size} → payout +${payout:.2f}\n"
                        f"Dev 1h: {(trend_dev or 0):+.2%} | secs: {int(secs_left or 0)}s\n"
                        f"{'[PAPER]' if PAPER_TRADE else '[LIVE]'} | {question[:60]}"
                    ))
                except Exception:
                    pass
                return True
            else:
                if not PAPER_TRADE:
                    print(f"  Order rejected: {question[:40]}")
        except Exception as e:
            print(f"  Trade error: {e}")
        return False

    async def check_positions(self, session: aiohttp.ClientSession):
        """In paper mode: sync open_positions with DB. In live: poll Polymarket."""
        if not self.open_positions:
            return

        if PAPER_TRADE:
            # Sync with DB — remove positions the resolver already closed
            try:
                conn       = sqlite3.connect(DB_PATH)
                still_open = {row[0] for row in conn.execute(
                    "SELECT token_id FROM trades WHERE status='open'"
                ).fetchall()}
                conn.close()
                closed = [tid for tid in list(self.open_positions.keys())
                          if tid not in still_open]
                for tid in closed:
                    trade = self.open_positions.pop(tid, {})
                    ts    = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] Position cleared from memory: {trade.get('question','?')[:40]}")
            except Exception as e:
                print(f"[WARN] Paper position check: {e}")
            return

        # Live mode: poll Polymarket positions API
        try:
            url = f"{POLYMARKET_HOST}/data/positions"
            async with session.get(url,
                params={"user": FUNDER_ADDRESS, "sizeThreshold": "0"},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return
                positions = await r.json()

            active     = {p.get("asset_id") or p.get("token_id") for p in positions}
            closed_ids = []
            for tid, trade in self.open_positions.items():
                if tid not in active:
                    pnl = round(trade["size"] / trade["entry"] - trade["size"], 2)
                    self.session_pnl  += pnl
                    self.session_high  = max(self.session_high, self.session_pnl)
                    ts = datetime.now().strftime("%H:%M:%S")
                    icon = "✓" if pnl > 0 else "✗"
                    print(f"[{ts}] {icon} CLOSED T{trade['tier']} | "
                          f"{'+' if pnl>0 else ''}${pnl:.2f} | '{trade['question'][:40]}'")
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute(
                        "UPDATE trades SET status=?,pnl=?,closed_at=? WHERE token_id=? AND status='open'",
                        ("won" if pnl>0 else "lost", pnl,
                         datetime.now(timezone.utc).isoformat(), tid)
                    )
                    conn.commit()
                    conn.close()
                    closed_ids.append(tid)
            for tid in closed_ids:
                del self.open_positions[tid]
        except Exception as e:
            print(f"[WARN] Position check: {e}")

    async def scan_strike_crossings(self, session: aiohttp.ClientSession) -> int:
        # v9.5: skip the entire T1 strike scan if disabled. T1 markets
        # rarely fire — the preloader's gamma fetch is dead weight.
        if not STRIKE_SCAN_ENABLED:
            return 0
        fired = 0
        await self.preloader.refresh(session)
        ready = self.preloader.get_ready_strikes()
        for sm in ready:
            if sm["token_id"] in self.open_positions:
                continue
            # T1 is latency-critical: bypass cache, always fetch fresh
            book = await get_orderbook(session, sm["token_id"], max_age_ms=0)
            if not book:
                continue
            ask, liq = best_ask_info(book)
            if ask is None or ask > T1_MAX_ENTRY or liq < MIN_LIQUIDITY:
                continue
            # Strike markets: YES token should be nearly certain (ask < 0.05 = 95%+ YES)
            no_implied = round(1 - ask, 4)
            if no_implied < 0.90:
                continue
            spot = self.tracker.prices.get(sm["coin"], 0)
            ts   = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] ★ STRIKE CROSSING {sm['coin']} spot=${spot:,.0f} "
                  f">= strike=${sm['strike']:,.0f} | ask=${ask:.3f} | "
                  f"certainty={no_implied:.0%}")
            if await self.fire_trade(1, sm["coin"], sm["token_id"], sm["market"], ask, outcome="UP"):
                self.tracker.mark_strike_fired(sm["coin"], sm["strike"])
                fired += 1
                await asyncio.sleep(0.3)
        return fired

    async def scan_updown_markets(self, session: aiohttp.ClientSession,
                                  markets: List[dict]):
        """S4 entry logic: 50/50 zone + 3-pillar order flow."""
        fired   = 0
        skip_np = 0; skip_win = 0; skip_cert = 0; signals = 0

        async def check_market(m):
            nonlocal fired, skip_np, skip_win, skip_cert, signals
            q    = m.get("question", "")
            coin = get_coin(q)
            if not coin or "up or down" not in q.lower():
                return

            secs = m.get("_secs_left", 0.0)
            mins = m.get("_mins_left", 0.0)

            # ── TIME WINDOW: enter early (1.5–5.5 min left) ──────────────────
            if not (S4_MIN_MINS * 60 <= secs <= S4_MAX_MINS * 60):
                skip_win += 1
                return

            # ── PARSE TOKENS ─────────────────────────────────────────────────
            tokens = m.get("tokens") or []
            up_tok = down_tok = None
            for tok in tokens:
                if not isinstance(tok, dict): continue
                out = (tok.get("outcome","") or "").upper()
                tid = tok.get("token_id") or tok.get("id")
                if not tid: continue
                if out in ("UP","YES","HIGHER","ABOVE"):
                    up_tok = tid
                elif out in ("DOWN","NO","LOWER","BELOW"):
                    down_tok = tid
            if not up_tok or not down_tok:
                skip_np += 1
                return

            # ── FETCH BOTH ORDERBOOKS (with OBI) ─────────────────────────────
            up_bid, up_ask, up_liq, up_obi   = await get_orderbook_with_obi(session, up_tok)
            dn_bid, dn_ask, dn_liq, dn_obi   = await get_orderbook_with_obi(session, down_tok)
            if up_ask is None or dn_ask is None:
                skip_np += 1
                return

            # ── FETCH ORDER FLOW SIGNALS (once per market, cached) ───────────
            cvd_sum, cvd_mom = await get_rolling_cvd(session, coin)
            atr_pct          = await get_atr_pct(session, coin)
            dev              = self.tracker.get_deviation(coin, "1h")
            hma_dir          = self.tracker.get_hma_direction(coin)
            squeeze_pct      = self.tracker.get_price_squeeze(coin)

            # ── CHECK EACH CANDIDATE SIDE ─────────────────────────────────────
            candidates = []
            if S4_ENTRY_MIN <= up_ask <= S4_ENTRY_MAX:
                candidates.append(("UP",   up_tok,  up_ask,  up_bid,  up_obi))
            if S4_ENTRY_MIN <= dn_ask <= S4_ENTRY_MAX:
                candidates.append(("DOWN", down_tok, dn_ask, dn_bid,  dn_obi))

            if not candidates:
                skip_win += 1
                return

            for (side, tok_id, ask, bid, obi) in candidates:
                if tok_id in self.open_positions:
                    continue

                spread = ask - (bid or 0.0)
                ts_dbg = datetime.now().strftime("%H:%M:%S")
                dbg = (f"[{ts_dbg}] S4 {coin} {side} ask={ask:.3f} spread={spread:.3f} "
                       f"CVD={cvd_sum}/{cvd_mom} OBI={obi} HMA={hma_dir} "
                       f"sqz={squeeze_pct} atr={atr_pct} dev={dev} mins={mins:.1f}")

                # ── SPREAD FILTER ─────────────────────────────────────────────
                if spread > S4_MAX_SPREAD:
                    print(dbg + f" → SKIP spread>{S4_MAX_SPREAD}")
                    continue

                # ── PILLAR 1: Rolling CVD ────────────────────────────────────
                if cvd_sum is None or cvd_mom is None:
                    print(dbg + " → SKIP cvd=None")
                    continue
                if side == "UP"   and not (cvd_sum > 0 and cvd_mom > 0):
                    print(dbg + f" → SKIP cvd UP need sum>0&mom>0")
                    continue
                if side == "DOWN" and not (cvd_sum < 0 and cvd_mom < 0):
                    print(dbg + f" → SKIP cvd DOWN need sum<0&mom<0")
                    continue

                # ── PILLAR 2: ATR-relative squeeze + OBI ─────────────────────
                if squeeze_pct is None:
                    print(dbg + " → SKIP squeeze=None")
                    continue
                in_squeeze = (
                    squeeze_pct < S4_ATR_MULT * atr_pct
                    if atr_pct else
                    squeeze_pct < 0.0020
                )
                if not in_squeeze:
                    print(dbg + f" → SKIP no_squeeze")
                    continue
                if obi is None:
                    print(dbg + " → SKIP obi=None")
                    continue
                if side == "UP"   and obi < S4_MIN_OBI:
                    print(dbg + f" → SKIP obi<{S4_MIN_OBI}")
                    continue
                if side == "DOWN" and obi > -S4_MIN_OBI:
                    print(dbg + f" → SKIP obi>{-S4_MIN_OBI}")
                    continue

                # ── ANOMALY SWITCH: OBI vs CVD conflict = trap ───────────────
                if side == "UP"   and obi > 0.30 and cvd_sum < -3.0:
                    print(dbg + " → SKIP anomaly")
                    continue
                if side == "DOWN" and obi < -0.30 and cvd_sum > 3.0:
                    print(dbg + " → SKIP anomaly")
                    continue

                # ── PILLAR 3: HMA direction ───────────────────────────────────
                if hma_dir != side:
                    print(dbg + f" → SKIP hma={hma_dir}")
                    continue

                # ── 1H TREND ALIGNMENT ────────────────────────────────────────
                if dev is None or abs(dev) < S4_MIN_DEV_1H:
                    print(dbg + f" → SKIP dev={dev}")
                    continue
                if side == "UP"   and dev < 0:
                    print(dbg + f" → SKIP dev_dir")
                    continue
                if side == "DOWN" and dev > 0:
                    print(dbg + f" → SKIP dev_dir")
                    continue

                # ── ALL PILLARS PASSED ────────────────────────────────────────
                signals += 1
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] S4 SIGNAL | {coin} {side} ask={ask:.3f} "
                      f"spread={spread:.3f} CVD={cvd_sum:.1f}/{cvd_mom:+.1f} "
                      f"OBI={obi:+.2f} HMA={hma_dir} dev={dev:+.3%} mins={mins:.1f}")
                log_event("🎯", f"S4 Signal {coin} {side} ask={ask:.3f} "
                                 f"CVD={cvd_sum:.1f} OBI={obi:+.2f} mins={mins:.1f}")

                sig_key = f"{tok_id}:{side}"
                if sig_key not in self._signaled:
                    self._signaled.add(sig_key)
                    if await self.fire_trade(S4_TIER, coin, tok_id, m, ask,
                                             outcome=side, trend_dev=dev, secs_left=secs):
                        fired += 1
                        await asyncio.sleep(0.3)

        await asyncio.gather(*[check_market(m) for m in markets])
        return fired, 0, 0, skip_cert, skip_win, signals

    async def refresh_opens_periodically(self):
        async with aiohttp.ClientSession() as session:
            while self.running:
                await self.tracker.fetch_opens(session)
                await asyncio.sleep(900)

    async def run(self):
        ws_task    = asyncio.create_task(self.tracker.run_websocket())
        opens_task = asyncio.create_task(self.refresh_opens_periodically())

        print("[Init] Waiting for Binance price feed...")
        for _ in range(30):
            if self.tracker.prices:
                break
            await asyncio.sleep(1)

        prices_str = " | ".join(f"{c}=${p:,.0f}" for c, p in self.tracker.prices.items())
        print(f"[Init] Prices: {prices_str}")
        print("[Scanner] Starting main loop...\n")

        # ── Engine v8 Change 3: Tuned HTTP connector ─────────────────────────
        # keepalive_timeout: hold idle TCP connections open for 60s (avoid handshake)
        # ttl_dns_cache:     cache DNS results for 5 minutes (avoid resolver hits)
        # limit:             allow 30 concurrent sockets to Polymarket
        # limit_per_host:    same, per host
        connector = aiohttp.TCPConnector(
            limit=30,
            limit_per_host=30,
            keepalive_timeout=60,
            ttl_dns_cache=300,
            use_dns_cache=True,
            ssl=False,
        )
        # v10: kick off the WebSocket subscriber. It runs forever, pushing
        # orderbook updates into _orderbook_cache as PM sends them.
        if WS_ENABLED:
            await WS_CLIENT.start()

        async with aiohttp.ClientSession(connector=connector) as session:
            while self.running:
                loop_start = time.time()
                self.scan_count += 1
                ts_now = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts_now}] Starting scan #{self.scan_count}...")

                # Ghost fill guard sweep — every ~60 scan cycles
                if _GHOST_GUARD_OK and self.scan_count % 60 == 0:
                    try:
                        _ghost_guard.sweep_ghosts(clob_client=None)
                    except Exception as _gge:
                        print(f"[GhostGuard] sweep failed (non-fatal): {_gge}")

                # Periodic _signaled clear — prevents unbounded set growth
                if time.time() - self._signaled_lastclear > 300:
                    self._signaled.clear()
                    self._signaled_lastclear = time.time()

                # Circuit breaker halt check — ghost_circuit_breaker.py writes this flag
                _halt_flag = os.path.join(SCRIPT_DIR, "tasks", "HALT.flag")
                if os.path.exists(_halt_flag):
                    try:
                        with open(_halt_flag) as _hf:
                            _reason = _hf.read().strip() or "(unspecified)"
                    except Exception:
                        _reason = "(unreadable)"
                    print(f"[HALT] Circuit breaker active: {_reason}")
                    print(f"[HALT] Delete tasks/HALT.flag to resume.")
                    await asyncio.sleep(30)
                    continue

                kill = self.kill_check()
                if kill:
                    print(f"\n[KILL SWITCH] {kill}")
                    break

                try:
                    await self.check_positions(session)
                except Exception as e:
                    print(f"[ERR] check_positions: {e}")

                t1_fired = 0
                try:
                    t1_fired = await asyncio.wait_for(
                        self.scan_strike_crossings(session), timeout=15.0)
                except asyncio.TimeoutError:
                    print("[WARN] Strike scan timed out")
                except Exception as e:
                    print(f"[ERR] strike scan: {e}")

                markets = []
                t234_fired = 0
                _sa = _sl = _sc = _sw = _sig = 0
                try:
                    ts_dbg = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts_dbg}] Fetching crypto markets...")
                    markets = await asyncio.wait_for(
                        fetch_crypto_markets(session), timeout=20.0)
                    ts_dbg = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts_dbg}] Got {len(markets)} crypto markets, scanning windows...")

                    # v9 Fix #6: pre-warm orderbooks for markets approaching
                    # their fire window (60s lead time). Background tasks —
                    # don't block the main loop.
                    # v10: also subscribe IN-WINDOW markets (and 60s ahead)
                    # to the WebSocket so PM pushes order-book updates to us.
                    ws_token_ids: List[str] = []
                    if PREWARM_ORDERBOOKS or WS_ENABLED:
                        for mkt in markets:
                            secs = mkt.get("_secs_left") or 0
                            # T2 fire window is 0-180s. Pre-warm 180-240s ahead.
                            in_or_near_window = (0 < secs <= 240)
                            if not in_or_near_window:
                                continue
                            for tok in (mkt.get("tokens") or []):
                                if not isinstance(tok, dict):
                                    continue
                                tid = tok.get("token_id") or tok.get("id")
                                if not tid:
                                    continue
                                # Stage HTTP pre-warm for the 60s lead range
                                if PREWARM_ORDERBOOKS and 180 < secs <= 240:
                                    asyncio.create_task(
                                        prewarm_orderbook(session, tid))
                                # Stage WS subscribe for everything in window
                                if WS_ENABLED:
                                    ws_token_ids.append(str(tid))
                        if ws_token_ids and WS_ENABLED:
                            asyncio.create_task(
                                WS_CLIENT.subscribe(ws_token_ids))
                    result = await asyncio.wait_for(
                        self.scan_updown_markets(session, markets), timeout=30.0)
                    if isinstance(result, tuple):
                        t234_fired, _sa, _sl, _sc, _sw, _sig = result
                    else:
                        t234_fired = result
                except asyncio.TimeoutError:
                    print("[WARN] Market scan timed out")
                except Exception as e:
                    print(f"[ERR] scan_updown: {e}")

                scan_ms = int((time.time() - loop_start) * 1000)
                log_scan_stats(len(markets), _sig, _sa, _sl, _sc, _sw, scan_ms)

                elapsed    = time.time() - loop_start
                ts         = datetime.now().strftime("%H:%M:%S")
                prices_str = " ".join(f"{c}=${p:,.0f}" for c, p in self.tracker.prices.items())
                ws_icon    = "●" if self.tracker._ws_connected else "○"

                in_window = sum(1 for m in markets
                    if in_entry_window(
                        classify_tier(m.get("question","")) or 0,
                        m.get("_mins_left", 9999)))

                print(f"[{ts}] {ws_icon} Scan #{self.scan_count} | "
                      f"CryptoMkts: {len(markets)} | InWindow: {in_window} | "
                      f"Strikes: {len(self.preloader.strike_markets)} | "
                      f"Fired: {t1_fired+t234_fired} | "
                      f"Open: {len(self.open_positions)} | "
                      f"PnL: ${self.session_pnl:+.2f}")
                print(f"         {prices_str}")

                wait = max(0, SCAN_INTERVAL - elapsed)
                await asyncio.sleep(wait)

        ws_task.cancel()
        opens_task.cancel()
        print(f"\n[STOPPED] PnL: ${self.session_pnl:+.2f} | Trades: {self.trades_fired}")


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    scanner = CryptoGhostScanner()
    try:
        asyncio.run(scanner.run())
    except KeyboardInterrupt:
        print(f"\n[STOPPED] Keyboard interrupt")
