"""
GHOST LATTICE v10 — Resolver (Chainlink-direct + PM gamma verify)
======================================================================
Polymarket's "Will [BTC|ETH] be Up" 5-minute crypto markets settle via
Chainlink price feeds on Polygon (BTC/USD, ETH/USD, etc) with UMA
Optimistic Oracle as dispute layer. Reads those feeds DIRECTLY from
the on-chain aggregator contracts — same exact data UMA sees.

Settlement rule (per polymarket.com docs):

    if p_end >= p_start: market resolves UP   (UP YES pays $1)
    if p_end <  p_start: market resolves DOWN (DOWN YES pays $1)

Flat candle (p_end == p_start) resolves UP. UP gets ties.
DOWN requires a STRICT drop to win.

Resolution chain (first that returns prices wins):
  1. Chainlink AggregatorV3 on Polygon  ← what PM's UMA actually uses
  2. Coinbase 1m candles                ← close approximation
  3. Binance USDT 1m klines             ← last-resort fallback

Tag in log: [CL]=chainlink, [CB]=coinbase, [BIN]=binance fallback.
Chainlink queries via web3.py (threaded, non-blocking asyncio loop).
POLYGON_RPC env var → point at Alchemy/Infura/QuickNode for reliability.
Fail-open: if Polygon unreachable, falls through to Coinbase → Binance.

DB: GHOST_LATTICE.db | Spawned by: GHOST_LATTICE_dashboard.py
"""

import asyncio
import aiohttp
import sqlite3
import os
import sys
import re
from datetime import datetime, timezone, timedelta

# ── Force UTF-8 stdout/stderr so unicode chars don't crash on Windows cp1252
# This must run before any print() that could contain non-ASCII characters.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# DST-safe ET timezone — falls back to fixed -4 offset if tzdata missing
try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:
    _ET_TZ = None

if sys.platform == "win32":
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    from dotenv import load_dotenv
    SD = os.path.dirname(os.path.abspath(__file__))
    ep = os.path.join(SD, ".env")
    if os.path.exists(ep):
        load_dotenv(ep, override=True)
        print(f"[ENV] .env ok")
except ImportError:
    pass

# Optional Telegram alerts — silent no-op if module / creds missing
try:
    from telegram_alerts import send_alert as _tg_send
    TELEGRAM_ENABLED = True
except Exception:
    TELEGRAM_ENABLED = False
    async def _tg_send(msg: str): pass

# ─────────────────────────────────────────────────────────────────────
# CHAINLINK DIRECT — read PM's actual UMA settlement source on Polygon.
# If web3 isn't installed or RPC won't connect, resolver continues on
# Coinbase → Binance fallback so the bot never stalls.
# ─────────────────────────────────────────────────────────────────────
try:
    from web3 import Web3
    _WEB3_AVAILABLE = True
except ImportError:
    _WEB3_AVAILABLE = False

# POLYGON_RPC env var lets you point at Alchemy/Infura/QuickNode for speed.
POLYGON_RPCS = [r for r in [
    os.getenv("POLYGON_RPC", "").strip(),
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
] if r]

# Chainlink AggregatorV3 addresses on Polygon mainnet (chain id 137).
# Source: https://docs.chain.link/data-feeds/price-feeds/addresses?network=polygon
CHAINLINK_FEEDS = {
    "BTC": "0xc907E116054Ad103354f2D350FD2514433D57F6f",  # BTC/USD
    "ETH": "0xF9680D99D6C9589e2a93a78A04A279e509205945",  # ETH/USD
    "BNB": "0x82a6c4AF830caa6c97bb504425f6A66165C2c26e",  # BNB/USD
    "SOL": "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC",  # SOL/USD (kept for reference)
    "XRP": "0x785ba89291f676b5386652eB12b30cF361020694",  # XRP/USD (kept for reference)
}

AGG_V3_ABI = [
    {"inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "latestRoundData",
     "outputs": [{"name": "roundId", "type": "uint80"},
                 {"name": "answer", "type": "int256"},
                 {"name": "startedAt", "type": "uint256"},
                 {"name": "updatedAt", "type": "uint256"},
                 {"name": "answeredInRound", "type": "uint80"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_roundId", "type": "uint80"}], "name": "getRoundData",
     "outputs": [{"name": "roundId", "type": "uint80"},
                 {"name": "answer", "type": "int256"},
                 {"name": "startedAt", "type": "uint256"},
                 {"name": "updatedAt", "type": "uint256"},
                 {"name": "answeredInRound", "type": "uint80"}],
     "stateMutability": "view", "type": "function"},
]


class _ChainlinkFeed:
    """Per-coin AggregatorV3 wrapper. Resolves price as-of any unix ts."""
    AVG_SECONDS_PER_ROUND = 30
    MAX_WALK_STEPS = 200  # live mode: target is seconds past close → small walk

    def __init__(self, w3, address):
        self.contract = w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=AGG_V3_ABI
        )
        self.decimals = self.contract.functions.decimals().call()
        self.scale = 10 ** self.decimals

    def _latest(self):
        rd = self.contract.functions.latestRoundData().call()
        return rd[0], rd[1], rd[3]  # roundId, answer, updatedAt

    def _get_round(self, rid):
        try:
            rd = self.contract.functions.getRoundData(rid).call()
            if rd[3] == 0:
                return None
            return rd[0], rd[1], rd[3]
        except Exception:
            return None

    def price_at(self, target_unix):
        """
        Find the Chainlink round whose updatedAt is CLOSEST to target_unix
        (could be slightly before or slightly after). PM's Data Streams
        update at sub-second cadence, so the round nearest the boundary
        — not the last round before it — is the better approximation.
        """
        latest_rid, latest_ans, latest_upd = self._latest()
        if target_unix >= latest_upd:
            return latest_ans / self.scale

        seconds_back = latest_upd - target_unix
        rounds_back = max(1, seconds_back // self.AVG_SECONDS_PER_ROUND)
        rid = latest_rid - rounds_back
        rd = self._get_round(rid)

        if rd is None:
            # Phase gap — slow walk back from latest
            rid = latest_rid
            answer = latest_ans
            upd = latest_upd
            steps = 0
            while upd > target_unix and steps < self.MAX_WALK_STEPS:
                rid -= 1
                rd = self._get_round(rid)
                if rd is None:
                    return None
                _, answer, upd = rd
                steps += 1
            # Now upd <= target_unix; check the next forward round too
            # to see if it's closer in time
            fwd = self._get_round(rid + 1)
            if fwd is not None:
                _, na, nu = fwd
                if abs(nu - target_unix) < abs(upd - target_unix):
                    return na / self.scale
            return answer / self.scale

        _, answer, upd = rd

        # Walk to find the round straddling target_unix (one before, one after)
        # then pick the one whose updatedAt is closer in time.
        if upd > target_unix:
            # Walk back until we find one ≤ target
            steps = 0
            after_upd = upd
            after_ans = answer
            while upd > target_unix and steps < self.MAX_WALK_STEPS:
                after_upd, after_ans = upd, answer
                rid -= 1
                rd = self._get_round(rid)
                if rd is None:
                    return None
                _, answer, upd = rd
                steps += 1
            # Now: upd <= target < after_upd. Pick closer.
            if abs(after_upd - target_unix) < abs(upd - target_unix):
                return after_ans / self.scale
            return answer / self.scale
        else:
            # Walk forward until just past target, tracking before/after.
            before_upd = upd
            before_ans = answer
            steps = 0
            while steps < self.MAX_WALK_STEPS:
                next_rid = rid + 1
                if next_rid > latest_rid:
                    break
                rd = self._get_round(next_rid)
                if rd is None:
                    break
                _, na, nu = rd
                if nu > target_unix:
                    # nu is the after-side
                    if abs(nu - target_unix) < abs(before_upd - target_unix):
                        return na / self.scale
                    return before_ans / self.scale
                rid, before_ans, before_upd = next_rid, na, nu
                steps += 1
            # Walked off the end without crossing — return last seen
            return before_ans / self.scale


class _ChainlinkResolver:
    """Lazy-init singleton: holds Polygon w3 + per-coin feed contracts."""
    def __init__(self):
        self.w3 = None
        self.feeds = {}
        self.connected = False
        self.tried = False

    def _connect(self):
        if self.tried:
            return self.connected
        self.tried = True
        if not _WEB3_AVAILABLE:
            print("[CL] web3 not installed -> Chainlink disabled. "
                  "Install:  pip install web3")
            return False
        for rpc in POLYGON_RPCS:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
                if w3.is_connected():
                    self.w3 = w3
                    self.connected = True
                    rpc_short = rpc.split("//")[-1][:30]
                    print(f"[CL] RPC: {rpc_short}")
                    break
            except Exception:
                continue
        if not self.connected:
            print("[CL] No Polygon RPC -> Coinbase fallback")
            return False
        for coin, addr in CHAINLINK_FEEDS.items():
            try:
                self.feeds[coin] = _ChainlinkFeed(self.w3, addr)
                print(f"[CL] Feed ready: {coin}/USD ({self.feeds[coin].decimals} dec)")
            except Exception as e:
                print(f"[CL] {coin} feed init failed: {e}")
        return True

    def get_prices(self, coin, start_unix, end_unix):
        """Sync, blocking. Returns (p_start, p_end) or (None, None)."""
        if not self.connected and not self._connect():
            return None, None
        feed = self.feeds.get(coin.upper())
        if feed is None:
            return None, None
        try:
            ps = feed.price_at(start_unix)
            pe = feed.price_at(end_unix)
            return ps, pe
        except Exception as e:
            print(f"[CL] {coin} lookup failed: {e}")
            return None, None


CHAINLINK = _ChainlinkResolver()


async def get_chainlink_prices(coin: str, start_utc, end_utc):
    """Async wrapper around sync web3 calls — runs in thread pool."""
    start_unix = int(start_utc.timestamp())
    end_unix = int(end_utc.timestamp())
    return await asyncio.to_thread(CHAINLINK.get_prices, coin, start_unix, end_unix)

PAPER_TRADE = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true","1","yes")

# v9.2: Mode B — after local resolution, query PM gamma to verify match.
# Spawns a background task that waits PM_VERIFY_DELAY seconds, queries
# /markets for the actual outcomePrices, and OVERRIDES the local result
# in the DB if PM disagrees.
PM_VERIFY        = os.getenv("PM_VERIFY", "true").strip().lower() in ("true","1","yes")
# v12.2: minimum seconds to wait after market close before querying PM gamma.
# Prevents reading transient orderbook outcomePrices before UMA finalizes.
# Default 120s; can lower if you observe PM settling faster.
RESOLUTION_GRACE_SECS = int(os.getenv("RESOLUTION_GRACE_SECS", "120"))
PM_VERIFY_DELAY  = int(os.getenv("PM_VERIFY_DELAY",  "60"))   # initial wait before first attempt
PM_VERIFY_RETRY  = int(os.getenv("PM_VERIFY_RETRY",  "5"))    # attempts before giving up
PM_VERIFY_BACKOFF= int(os.getenv("PM_VERIFY_BACKOFF","45"))   # additional sleep between retries
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(SCRIPT_DIR,
                "GHOST_LATTICE_PAPER.db" if PAPER_TRADE else "GHOST_LATTICE.db")

GAMMA_API    = "https://gamma-api.polymarket.com"
DATA_API     = "https://data-api.polymarket.com"
BINANCE_API  = "https://api.binance.us"
COINBASE_API = "https://api.exchange.coinbase.com"
POLL_INTERVAL = 3   # 2026-05-09: 10 → 3. Faster pickup of newly-settled trades.

# Coinbase product map for crypto markets (= what PM UMA oracle uses)
COINBASE_PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
}

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")   # concurrent reader + writer safe
    conn.execute("PRAGMA busy_timeout=15000") # wait up to 15s instead of failing
    conn.execute("PRAGMA wal_autocheckpoint=100")  # prevent WAL bloat
    return conn

def ensure_schema():
    """Self-healing: create trades table if missing, then backfill any
    missing columns. Runs on every startup so a wiped DB can't trigger
    'no such column' loop errors."""
    conn = db_connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, tier INTEGER, coin TEXT,
            market_id TEXT, token_id TEXT, question TEXT,
            entry_price REAL, size_usdc REAL, order_id TEXT,
            outcome TEXT DEFAULT 'UP', status TEXT DEFAULT 'open',
            pnl REAL DEFAULT 0, closed_at TEXT
        )
    """)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    for name, decl in [
        ("outcome", "TEXT DEFAULT 'UP'"),
        ("status", "TEXT DEFAULT 'open'"),
        ("pnl", "REAL DEFAULT 0"),
        ("closed_at", "TEXT"),
        ("price_start", "REAL"),
        ("price_end", "REAL"),
        ("resolution_verified", "INTEGER DEFAULT 0"),
        ("condition_id", "TEXT"),
        ("redeemed", "INTEGER DEFAULT 0"),
        ("redeemed_at", "TEXT"),
        ("redeem_tx_hash", "TEXT"),
    ]:
        if name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {decl}")
        except Exception:
            pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, icon TEXT, msg TEXT
        )
    """)
    conn.commit()
    conn.close()


def is_condition_id(s: str) -> bool:
    """Hex condition_id = 0x + 64 hex chars."""
    if not s:
        return False
    s = s.lower()
    if s.startswith("0x"):
        s = s[2:]
    return len(s) == 64 and all(c in "0123456789abcdef" for c in s)


async def fetch_condition_id(session, token_id: str, market_id: str) -> str:
    """
    Resolve a condition_id for a trade so the redeemer can claim winnings.
    Order:
      1. If market_id already looks like a hex condition_id, use it directly.
      2. Try gamma API by clob_token_ids (handles both hex and integer market IDs).
      3. Try gamma API by integer market_id directly — handles gamma-format IDs
         (e.g. "2153666") that would silently fail the hex check above.
      4. Try data-api positions by assetId.
    Returns "" if nothing works (paper mode is fine without it).
    """
    if is_condition_id(market_id):
        return market_id if market_id.startswith("0x") else f"0x{market_id}"

    if token_id:
        try:
            async with session.get(
                f"{GAMMA_API}/markets",
                params={"clob_token_ids": token_id},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    items = data.get("data", data) if isinstance(data, dict) else data
                    if isinstance(items, list) and items:
                        cid = items[0].get("condition_id", "") or items[0].get("conditionId", "")
                        if cid:
                            return cid
        except Exception:
            pass

    # Gamma integer ID fallback — market_id like "2153666" won't pass
    # is_condition_id() but gamma can look it up directly by numeric id.
    if market_id and not is_condition_id(market_id):
        try:
            async with session.get(
                f"{GAMMA_API}/markets",
                params={"id": market_id},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    items = data.get("data", data) if isinstance(data, dict) else data
                    if isinstance(items, list) and items:
                        cid = items[0].get("condition_id", "") or items[0].get("conditionId", "")
                        if cid:
                            return cid
        except Exception:
            pass

    if token_id:
        try:
            async with session.get(
                f"{DATA_API}/positions",
                params={"assetId": token_id},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    items = data if isinstance(data, list) else []
                    if items:
                        cid = items[0].get("conditionId", "")
                        if cid:
                            return cid
        except Exception:
            pass

    return ""

def log_event(icon, msg):
    try:
        conn = db_connect()
        conn.execute("INSERT INTO events (ts, icon, msg) VALUES (?,?,?)",
                     (datetime.now().strftime("%H:%M:%S"), icon, str(msg)[:120]))
        conn.execute("DELETE FROM events WHERE id NOT IN "
                     "(SELECT id FROM events ORDER BY id DESC LIMIT 500)")
        conn.commit()
        conn.close()
    except Exception:
        pass

def parse_market_times(question: str):
    """
    Parse start/end UTC from a Polymarket question string.
    Handles all four tier formats:

      T2 hourly  : "May 7, 2:30PM-3:00PM"       H:MM AP - H:MM AP same day
      T3 4-hour  : "May 7, 2:00PM-6:00PM"        same regex as T2
      T4 daily   : "May 7, 7PM ET"               single time, no minutes
                   "May 7, 7:00PM ET"             single time with minutes
                   "May 7, 7PM-May 8, 7PM"        cross-day range
      T1 strike  : "May 7, 2:30PM-3:00PM"        same as T2
    """
    months = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
              "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
    year = datetime.now(timezone.utc).year

    def to24(h_str, m_str, ap):
        h = int(h_str)
        m = int(m_str) if m_str else 0
        if ap.upper() == "PM" and h != 12: h += 12
        if ap.upper() == "AM" and h == 12: h = 0
        return h, m

    def build_utc(mo, day, h, m):
        try:
            if _ET_TZ is not None:
                dt_et = datetime(year, mo, int(day), h, m, tzinfo=_ET_TZ)
                return dt_et.astimezone(timezone.utc)
            else:
                return datetime(year, mo, int(day), h, m,
                                tzinfo=timezone.utc) + timedelta(hours=4)
        except Exception:
            return None

    # ── Pattern 1: "Month D, H:MMAM-H:MMPM" (T2 / T3 / T1 hourly range) ──────
    m = re.search(
        r'(\w+)\s+(\d+),\s+(\d+):(\d+)\s*(AM|PM)\s*-\s*(\d+):(\d+)\s*(AM|PM)',
        question, re.IGNORECASE
    )
    if m:
        mn, day, h1, mi1, ap1, h2, mi2, ap2 = m.groups()
        mo = months.get(mn.lower())
        if mo:
            H1, M1 = to24(h1, mi1, ap1)
            H2, M2 = to24(h2, mi2, ap2)
            s = build_utc(mo, day, H1, M1)
            e = build_utc(mo, day, H2, M2)
            if s and e:
                if e < s: e += timedelta(days=1)
                return s, e

    # ── Pattern 2: "Month D, HAM/PM" or "Month D, H:MMAM/PM" (T4 single time) ─
    # e.g. "May 7, 7PM ET"  or  "May 7, 7:00PM ET"
    m = re.search(
        r'(\w+)\s+(\d+),\s+(\d+)(?::(\d+))?\s*(AM|PM)',
        question, re.IGNORECASE
    )
    if m:
        mn, day, h2, mi2, ap2 = m.groups()
        mo = months.get(mn.lower())
        if mo:
            H2, M2 = to24(h2, mi2, ap2)
            e = build_utc(mo, day, H2, M2)
            if e:
                # T4 daily = 24h window ending at close time
                s = e - timedelta(hours=24)
                return s, e

    # ── Pattern 3: cross-day "Month D, HAP-Month D, HAP" (T4 overnight range) ──
    m = re.search(
        r'(\w+)\s+(\d+),\s+(\d+)(?::(\d+))?\s*(AM|PM)\s*-\s*(\w+)\s+(\d+),\s+(\d+)(?::(\d+))?\s*(AM|PM)',
        question, re.IGNORECASE
    )
    if m:
        mn1, d1, h1, mi1, ap1, mn2, d2, h2, mi2, ap2 = m.groups()
        mo1 = months.get(mn1.lower())
        mo2 = months.get(mn2.lower())
        if mo1 and mo2:
            H1, M1 = to24(h1, mi1, ap1)
            H2, M2 = to24(h2, mi2, ap2)
            s = build_utc(mo1, d1, H1, M1)
            e = build_utc(mo2, d2, H2, M2)
            if s and e:
                if e < s: e += timedelta(days=1)
                return s, e

    return None, None

async def get_binance_price_at(session, symbol: str, ts: datetime):
    target_ms = int(ts.timestamp() * 1000)
    start_ms = target_ms - 60000
    end_ms   = target_ms + 60000

    try:
        async with session.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": "1m",
                    "startTime": start_ms, "endTime": end_ms, "limit": 3},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data:
                return None
            best = min(data, key=lambda c: abs(c[0] - target_ms))
            return float(best[4])
    except Exception:
        return None


async def get_coinbase_price_at(session, product: str, ts: datetime):
    """
    Pull the close of the 1-minute candle that contains `ts` from
    Coinbase Exchange. This is the same data Polymarket's UMA oracle
    uses to resolve "Will [coin] be Up" markets.

    Coinbase returns: [[time, low, high, open, close, volume], ...]
    where time is epoch SECONDS (not ms) and close is index 4.
    """
    target = int(ts.timestamp())
    start_iso = datetime.fromtimestamp(target - 60, tz=timezone.utc).isoformat()
    end_iso   = datetime.fromtimestamp(target + 60, tz=timezone.utc).isoformat()
    try:
        async with session.get(
            f"{COINBASE_API}/products/{product}/candles",
            params={"granularity": "60", "start": start_iso, "end": end_iso},
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Mozilla/5.0"},
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not isinstance(data, list) or not data:
                return None
            best = min(data, key=lambda c: abs(c[0] - target))
            return float(best[4])
    except Exception:
        return None

async def resolve_via_polymarket(session, token_id: str, market_id: str):
    """
    Authoritative resolution via Polymarket gamma-api.
    Returns ('won'|'lost'|'pending', settled_value or None).
    """
    if not token_id:
        return "pending", None
    import json as _json
    # Gamma's /markets defaults to active=true. Closed 5-min crypto markets
    # need closed=true (or active=false) to be returned. Without this the
    # call returns nothing, the verify task hangs for ~5 min, and no
    # Telegram alert fires. (Same bug audit_pm.py hit.)
    urls = [
        f"{GAMMA_API}/markets?clob_token_ids={token_id}&closed=true",
        f"{GAMMA_API}/markets?clob_token_ids={token_id}",  # active fallback
    ]
    if market_id:
        urls.append(f"{GAMMA_API}/markets?id={market_id}&closed=true")
        urls.append(f"{GAMMA_API}/markets?id={market_id}")
    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    continue
                data = await r.json()
                # Bug #6 fix: Gamma sometimes returns {"data":[...]} not bare list
                items = data.get("data", data) if isinstance(data, dict) else data
                if not isinstance(items, list) or not items:
                    continue
                m = items[0]
                if not m.get("closed"):
                    return "pending", None

                # ── Fast-path: winner field (set by PM immediately on settle) ──
                # Gamma tokens carry winner=True/False the moment UMA posts result.
                # This resolves 60-120s faster than waiting for outcomePrices binary.
                _tokens = m.get("tokens") or []
                if isinstance(_tokens, str):
                    try: _tokens = _json.loads(_tokens)
                    except Exception: _tokens = []
                for _t in _tokens:
                    if not isinstance(_t, dict): continue
                    _tid = str(_t.get("token_id") or _t.get("tokenId") or "")
                    if _tid == str(token_id):
                        _winner = _t.get("winner")
                        if _winner is True:
                            return "won", 1.0
                        elif _winner is False:
                            return "lost", 0.0
                        break  # found our token, winner not yet set → fall through

                op  = m.get("outcomePrices")
                cti = m.get("clobTokenIds")
                if isinstance(op, str):
                    try: op = _json.loads(op)
                    except Exception: op = []
                if isinstance(cti, str):
                    try: cti = _json.loads(cti)
                    except Exception: cti = []
                if not op or not cti:
                    return "pending", None
                try:
                    idx = cti.index(str(token_id))
                except ValueError:
                    continue
                try:
                    settled = float(op[idx])
                except (TypeError, ValueError, IndexError):
                    continue
                # v12.1: be VERY strict. Even though closed=True is set,
                # there's a window where outcomePrices still reflect the
                # last orderbook midpoint (e.g. [0.97, 0.03]) before UMA
                # publishes the binary settlement (e.g. [1.00, 0.00]).
                # Require BOTH sides at the binary extremes — if either
                # is in the middle, treat as pending and retry.
                try:
                    other_idx = 1 - idx if len(op) >= 2 else None
                    other_settled = float(op[other_idx]) if other_idx is not None else None
                except (TypeError, ValueError, IndexError):
                    other_settled = None
                # Path A: hard binary (UMA fully posted 1.00/0.00)
                # Path C: relaxed — after RESOLUTION_GRACE_SECS market
                # is clearly settled at 0.95/0.05 even before UMA ticks
                # all the way to 1.00. Cuts 4-7 min settle lag to ~2-3 min.
                fully_settled = (
                    other_settled is not None and (
                        (settled >= 0.99 and other_settled <= 0.01) or   # Path A: hard binary
                        (settled <= 0.01 and other_settled >= 0.99) or   # Path A: hard binary
                        (settled >= 0.90 and other_settled <= 0.10) or   # Path C: relaxed (was 0.95)
                        (settled <= 0.10 and other_settled >= 0.90)      # Path C: relaxed (was 0.05)
                    )
                )
                if not fully_settled:
                    return "pending", settled
                return ("won" if settled >= 0.95 else "lost"), settled
        except Exception:
            continue
    return "pending", None


def _apply_pm_rule(p_start, p_end, bought_up):
    """
    Polymarket's documented settlement rule for crypto Up/Down markets:
        if p_end >= p_start: market resolves UP   (UP YES pays $1)
        if p_end <  p_start: market resolves DOWN (DOWN YES pays $1)

    Flat candle resolves UP — DOWN must be a strict drop to win.
    """
    market_resolves_up = p_end >= p_start
    if bought_up:
        return "won" if market_resolves_up else "lost"
    return "lost" if market_resolves_up else "won"


async def resolve_via_polymarket_source(session, coin: str, start_utc, end_utc, bought_up: bool):
    """
    v10.1: CHAINLINK-ONLY resolution. No Coinbase, no Binance fallback.
    Polymarket settles via Chainlink Data Streams. The on-chain
    AggregatorV3 is the closest free approximation. If Chainlink isn't
    available (RPC down, no feed for coin), the trade stays PENDING and
    the resolver retries on the next loop. We never resolve from a
    different price source than PM, even at the cost of waiting longer.

    Returns ('won'|'lost'|'pending', p_start, p_end, source)
    where source is 'chainlink' or None on pending.
    """
    now = datetime.now(timezone.utc)
    if (now - end_utc).total_seconds() < 60:
        return "pending", None, None, None

    p_start, p_end = await get_chainlink_prices(coin, start_utc, end_utc)
    if p_start is not None and p_end is not None:
        status = _apply_pm_rule(p_start, p_end, bought_up)
        return status, p_start, p_end, "chainlink"

    # No fallback. Pending until Chainlink comes back online.
    return "pending", None, None, None


# Kept for backward compat; new code should use resolve_via_polymarket_source.
async def resolve_via_binance(session, coin: str, start_utc, end_utc, bought_up: bool):
    status, p_start, p_end, _ = await resolve_via_polymarket_source(
        session, coin, start_utc, end_utc, bought_up
    )
    return status, p_start, p_end

# Resolution-source codes recorded in trades.resolution_verified column.
#   0 = unverified / unknown
#   1 = coinbase 1m candle (v7 default)
#   2 = binance USDT 1m kline (v6 fallback / v8 last resort)
#   7 = PM-rule repair script (restore_pm_rule.py)
#   9 = Chainlink AggregatorV3 on Polygon (v8 truth, what UMA reads)
RESOLUTION_CODE = {
    "pm":                10,   # v11: PM gamma primary (source of truth)
    "chainlink":          9,
    "coinbase":           1,
    "binance_fallback":   2,
    "binance":            2,
    # v9.2 PM gamma verification codes (still used when Chainlink-fallback gets re-verified):
    "pm_gamma_confirm":  12,   # local result matched PM exactly
    "pm_gamma_override": 11,   # PM disagreed — local result was overridden
}


def close_trade(trade_id, status, pnl, p_start, p_end,
                condition_id: str = "", source: str = ""):
    """
    Backfill resolution + condition_id + source code. Records which data
    feed actually closed this trade so audits can tell Chainlink (9) from
    Coinbase (1) from Binance fallback (2). condition_id only overwrites
    when currently NULL/empty so the redeemer's view of truth isn't
    clobbered if it set it earlier.
    """
    code = RESOLUTION_CODE.get(source, 1)
    params = (
        status, pnl,
        datetime.now(timezone.utc).isoformat(),
        p_start, p_end,
        code,
        condition_id,
        trade_id
    )
    for attempt in range(3):
        conn = None
        try:
            conn = db_connect()
            conn.execute("""
                UPDATE trades
                SET status = ?,
                    pnl = ?,
                    closed_at = ?,
                    price_start = ?,
                    price_end = ?,
                    resolution_verified = ?,
                    condition_id = CASE
                        WHEN (condition_id IS NULL OR condition_id = '')
                        THEN ?
                        ELSE condition_id
                    END
                WHERE id = ?
            """, params)
            conn.commit()
            return True
        except Exception as e:
            if attempt < 2:
                import time as _t; _t.sleep(0.5 * (attempt + 1))
            else:
                print(f"[DB-CRITICAL] close_trade failed 3x for #{trade_id} {status}: {e}")
                return False


        finally:
            if conn:
                try: conn.close()
                except Exception: pass
async def verify_against_pm_gamma(session, trade_id: int, token_id: str,
                                   market_id: str, local_status: str,
                                   local_pnl: float, size: float,
                                   entry: float, coin: str, side: str,
                                   tier: int = 0, question: str = "",
                                   src_tag: str = "",
                                   p_start: float = 0.0, p_end: float = 0.0):
    """
    v9.2 Mode B: After local resolution, query Polymarket gamma for the
    actual settled outcomePrices. If PM disagrees with local, OVERRIDE
    the trade row in the DB and tag resolution_verified = 11. If PM
    agrees, tag = 12 (confirmed).

    Spawned as a background task so the resolver loop never blocks.
    """
    if not PM_VERIFY:
        return

    # Initial wait — give UMA time to post the result
    await asyncio.sleep(PM_VERIFY_DELAY)

    pm_status = "pending"
    pm_settled = None
    for attempt in range(PM_VERIFY_RETRY):
        try:
            pm_status, pm_settled = await resolve_via_polymarket(session, token_id, market_id)
        except Exception as e:
            print(f"[PM-VERIFY] #{trade_id} gamma error: {e}")
            pm_status = "pending"
        if pm_status in ("won", "lost"):
            break
        # Still pending — wait and retry
        await asyncio.sleep(PM_VERIFY_BACKOFF)

    if pm_status not in ("won", "lost"):
        print(f"[PM-VERIFY] [WRN] #{trade_id} {coin} {side} -- gave up after "
              f"{PM_VERIFY_RETRY} attempts (PM still pending)")
        # Don't leave the user in silence — send the local Chainlink call
        # marked as unverified so they at least know the trade closed.
        if TELEGRAM_ENABLED:
            tg_icon = "[WIN]" if local_status == "won" else "[LOSS]"
            tag = {"chainlink":"[CL]","coinbase":"[CB]","binance_fallback":"[BIN]"}.get(src_tag, "[?]")
            price_line = ""
            if p_start and p_end:
                pc = ((p_end - p_start)/p_start)*100
                price_line = f"\n{p_start:.2f} -> {p_end:.2f} ({pc:+.3f}%)"
            try:
                await _tg_send(
                    f"{tg_icon} <b>T{tier} {coin} {side}</b> "
                    f"{local_status.upper()} <code>${local_pnl:+.2f}</code> {tag} [UNVERIFIED]\n"
                    f"PM gamma didn't respond -- local call may be wrong.\n"
                    f"{(question or '')[:80]}{price_line}"
                )
            except Exception:
                pass
        return

    if pm_status == local_status:
        # Match — just stamp the verification code so audits can see it
        for attempt in range(3):
            conn = None
            try:
                conn = db_connect()
                conn.execute("""
                    UPDATE trades SET resolution_verified = ?
                    WHERE id = ? AND status = ?
                """, (RESOLUTION_CODE["pm_gamma_confirm"], trade_id, local_status))
                conn.commit()
                print(f"[PM-VERIFY] [OK] #{trade_id} {coin} {side} PM={pm_status} "
                      f"matches local -- confirmed (settled={pm_settled})")
                break
            except Exception as e:
                if attempt < 2:
                    import time as _t; _t.sleep(0.5 * (attempt + 1))
                else:
                    print(f"[PM-VERIFY] DB stamp failed 3x #{trade_id}: {e}")
        # Send Telegram with PM-verified result (matched local)
            finally:
                if conn:
                    try: conn.close()
                    except Exception: pass
        if TELEGRAM_ENABLED:
            tg_icon = "[WIN]" if local_status == "won" else "[LOSS]"
            tag = {"chainlink":"[CL]","coinbase":"[CB]","binance_fallback":"[BIN]"}.get(src_tag, "[?]")
            price_line = ""
            if p_start and p_end:
                pc = ((p_end - p_start)/p_start)*100
                price_line = f"\n{p_start:.2f} -> {p_end:.2f} ({pc:+.3f}%)"
            try:
                await _tg_send(
                    f"{tg_icon} <b>T{tier} {coin} {side}</b> "
                    f"{local_status.upper()} <code>${local_pnl:+.2f}</code> {tag} [PM-OK]\n"
                    f"{(question or '')[:80]}{price_line}"
                )
            except Exception:
                pass
        return

    # ── OVERRIDE — PM disagrees with local resolution ──
    if pm_status == "won":
        new_pnl = round((size / entry) - size, 2) if entry > 0 else 0
        new_icon = "[WIN]"
    else:
        new_pnl = -float(size)
        new_icon = "[LOSS]"
    delta = new_pnl - local_pnl
    print(f"[PM-VERIFY] *** OVERRIDE #{trade_id} {coin} {side}: "
          f"local={local_status} ${local_pnl:+.2f} → PM={pm_status} ${new_pnl:+.2f} "
          f"(delta ${delta:+.2f}, settled={pm_settled})")
    for attempt in range(3):
        conn = None
        try:
            conn = db_connect()
            conn.execute("""
                UPDATE trades
                SET status = ?, pnl = ?,
                    resolution_verified = ?
                WHERE id = ?
            """, (pm_status, new_pnl,
                  RESOLUTION_CODE["pm_gamma_override"], trade_id))
            conn.commit()
            log_event(new_icon, f"PM override #{trade_id} {coin}: {local_status}→{pm_status} "
                                f"${delta:+.2f}")
            break
        except Exception as e:
            if attempt < 2:
                import time as _t; _t.sleep(0.5 * (attempt + 1))
            else:
                print(f"[PM-VERIFY] DB override failed 3x #{trade_id}: {e}")
    # Send Telegram with PM's corrected outcome
        finally:
            if conn:
                try: conn.close()
                except Exception: pass
    if TELEGRAM_ENABLED:
        try:
            await _tg_send(
                f"{new_icon} <b>T{tier} {coin} {side}</b> "
                f"{pm_status.upper()} <code>${new_pnl:+.2f}</code> [PM-OVERRIDE]\n"
                f"local was {local_status} (delta ${delta:+.2f})\n"
                f"{(question or '')[:80]}"
            )
        except Exception:
            pass


async def _sync_balance_after_win(trade_id: int, coin: str, tier: int,
                                   pnl: float, size: float, entry: float):
    """
    90 s after a CLOB win: force-sync the CLOB balance so the payout
    shows up immediately in the dashboard, then log the confirmed amount.

    Why needed: the bet cost is deducted the instant the order fills
    (balance drops from e.g. $521 → $518). Polymarket's CLOB credits the
    payout 1-3 min later. Without an explicit update_balance_allowance call
    the dashboard can read a stale cached value for several minutes.
    """
    await asyncio.sleep(90)          # give Polymarket ~90s to settle
    try:
        def _do_sync():
            pk = os.getenv("PRIVATE_KEY", "")
            if not pk:
                return None
            try:
                from py_clob_client_v2.client import ClobClient
                from py_clob_client_v2.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
            except ImportError:
                try:
                    from py_clob_client.client import ClobClient
                    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
                except ImportError:
                    return None
            import time as _time
            creds = ApiCreds(
                api_key        = os.getenv("API_KEY",        os.getenv("CLOB_API_KEY",    "")),
                api_secret     = os.getenv("API_SECRET",     os.getenv("CLOB_SECRET",     "")),
                api_passphrase = os.getenv("API_PASSPHRASE", os.getenv("CLOB_PASSPHRASE", "")),
            )
            client = ClobClient(host="https://clob.polymarket.com", chain_id=137,
                                key=pk, creds=creds, signature_type=0)
            try:
                # Nudge CLOB to re-read on-chain pUSD balance
                client.update_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            except Exception:
                pass
            _time.sleep(3)
            result = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            raw = float(result.get("balance", 0)) if isinstance(result, dict) else 0.0
            return raw / 1_000_000 if raw > 10_000 else raw

        bal = await asyncio.to_thread(_do_sync)
        ts  = datetime.now().strftime("%H:%M:%S")
        if bal is not None:
            print(f"[{ts}] [PAY] T{tier} {coin} #{trade_id} payout settled | "
                  f"CLOB balance ${bal:.2f} (won ${pnl:+.2f})")
            log_event("[PAY]", f"T{tier} {coin} payout ${pnl:+.2f} | bal ${bal:.2f}")
        else:
            print(f"[{ts}] [PAY] T{tier} {coin} #{trade_id} payout settled (paper/no-key)")
    except Exception as e:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [WARN] Post-win balance sync error: {e}")


# ── WS fast-resolution — shared state ────────────────────────────────────────
# token_id → True (won) / False (lost), set by ws_resolution_watch().
# Poll loop checks this first; if present it bypasses RESOLUTION_GRACE_SECS
# and jumps straight to PM gamma verify (which will now return a real result).
_ws_settled: dict = {}
_WS_RESOLVE_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


async def ws_resolution_watch():
    """Subscribe to open-position token IDs on the CLOB WS.
    When any token's best-bid jumps to >=0.95 (won) or best-ask collapses
    to <=0.02 with no bids (lost), flag it in _ws_settled so the poll loop
    resolves immediately without waiting full RESOLUTION_GRACE_SECS.
    Falls back silently if WS drops — polling handles everything as before.
    """
    import json as _json
    while True:
        try:
            async with aiohttp.ClientSession() as _sess:
                async with _sess.ws_connect(
                        _WS_RESOLVE_URL, heartbeat=20,
                        timeout=aiohttp.ClientTimeout(total=None)) as ws:
                    print("[WS-RES] connected — fast settlement watcher active")
                    _subscribed: set = set()

                    while True:
                        # Refresh open token IDs from DB every 15s
                        try:
                            _c = db_connect()
                            _rows = _c.execute(
                                "SELECT token_id FROM trades "
                                "WHERE status='open' AND token_id != ''"
                            ).fetchall()
                            _c.close()
                            _live = {r[0] for r in _rows if r[0]}
                        except Exception:
                            _live = set()

                        # Subscribe to any new token IDs not yet watched
                        _new = _live - _subscribed
                        if _new:
                            await ws.send_json({"type": "market",
                                                "assets_ids": list(_new)})
                            _subscribed.update(_new)
                            print(f"[WS-RES] watching {len(_subscribed)} token(s)")

                        # Read next WS message; 15s timeout triggers subscription refresh
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=15)
                        except asyncio.TimeoutError:
                            continue

                        if msg.type in (aiohttp.WSMsgType.CLOSED,
                                        aiohttp.WSMsgType.ERROR):
                            break

                        try:
                            data = _json.loads(msg.data)
                        except Exception:
                            continue

                        events = data if isinstance(data, list) else [data]
                        for ev in events:
                            if not isinstance(ev, dict):
                                continue
                            ev_type = (ev.get("event_type") or
                                       ev.get("type") or "").lower()
                            if ev_type != "book":
                                continue
                            tid = str(ev.get("asset_id") or
                                      ev.get("token_id") or "")
                            if not tid or tid not in _live or tid in _ws_settled:
                                continue

                            bids = ev.get("bids") or []
                            asks = ev.get("asks") or []

                            def _best(side, fn):
                                try:
                                    vals = [float(x["price"]
                                            if isinstance(x, dict) else x)
                                            for x in side]
                                    return fn(vals) if vals else None
                                except Exception:
                                    return None

                            best_bid = _best(bids, max)
                            best_ask = _best(asks, min)
                            _ts = datetime.now().strftime("%H:%M:%S")

                            if best_bid is not None and best_bid >= 0.95:
                                print(f"[{_ts}] [WS-RES] WIN  {tid[:16]}... "
                                      f"bid={best_bid:.3f} — grace bypassed")
                                _ws_settled[tid] = True

                            elif (best_ask is not None and best_ask <= 0.02
                                  and not bids):
                                print(f"[{_ts}] [WS-RES] LOSS {tid[:16]}... "
                                      f"ask={best_ask:.3f} — grace bypassed")
                                _ws_settled[tid] = False

        except Exception as e:
            print(f"[WS-RES] disconnected: {e} — retry in 10s")
            await asyncio.sleep(10)


async def main():
    tg     = "ON" if TELEGRAM_ENABLED else "off"
    verify = "ON" if PM_VERIFY else "off"
    mode_str = "PAPER" if PAPER_TRADE else "LIVE"
    db_name  = os.path.basename(DB_PATH)
    print("+=====================================================+")
    print("|         ORACLE SNIPER  --  RESOLVER  v10           |")
    print(f"|  Mode: {mode_str:<8}  Telegram: {tg:<3}  PM-verify: {verify:<3}  |")
    print(f"|  DB:   {db_name:<43}|")
    print("+=====================================================+")
    print("[RULE]  p_end >= p_start -> UP   |   p_end < p_start -> DOWN")
    print("[TAGS]  [CL]=Chainlink  [CB]=Coinbase  [BIN]=Binance")
    print(f"[PMVFY] delay={PM_VERIFY_DELAY}s  retries={PM_VERIFY_RETRY}")

    # Pre-warm the Chainlink connection so first trade isn't slow.
    await asyncio.to_thread(CHAINLINK._connect)

    ensure_schema()

    # Launch WS fast-resolution watcher as background task.
    # Runs independently — if it crashes the poll loop continues unaffected.
    _ws_task = asyncio.create_task(ws_resolution_watch())

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                conn = db_connect()
                open_trades = conn.execute("""
                    SELECT id, tier, coin, entry_price, size_usdc,
                           COALESCE(outcome,'UP'), question,
                           COALESCE(market_id,''),
                           COALESCE(token_id,''),
                           COALESCE(condition_id,'')
                    FROM trades
                    WHERE status = 'open'
                    ORDER BY id ASC
                """).fetchall()
                conn.close()

                if not open_trades:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] No open trades. Check in {POLL_INTERVAL}s...")
                else:
                    # Dedup guard: if multiple DB rows exist for the same token_id
                    # (e.g. bot fired the same market twice), only LOG the first one.
                    # All duplicates still get closed in the DB — we just don't
                    # spam the activity panel with identical loss entries.
                    _seen_tokens: set = set()
                    for trade in open_trades:
                        (trade_id, tier, coin, entry, size, outcome, question,
                         market_id, token_id, existing_cid) = trade
                        ts = datetime.now().strftime("%H:%M:%S")

                        start_utc, end_utc = parse_market_times(question)
                        bought_up = outcome.upper() in ("UP","YES","HIGHER","ABOVE")
                        side = "UP" if bought_up else "DOWN"
                        now_utc = datetime.now(timezone.utc)

                        if start_utc is None:
                            # Can't parse time — skip pre-close gate and let PM gamma
                            # decide. If market isn't settled yet PM returns "pending"
                            # and we retry next loop. Never silently abandon the trade.
                            print(f"[{ts}] [?T] T{tier} {coin} #{trade_id}: "
                                  f"unrecognised question format -- trying PM gamma directly")
                            secs_after = 9999   # bypass grace check
                        else:
                            if now_utc < end_utc:
                                secs_to_close = (end_utc - now_utc).total_seconds()
                                print(f"[{ts}] [--] T{tier} {coin} #{trade_id} | "
                                      f"{secs_to_close/60:.1f}min until close")
                                continue
                            secs_after = (now_utc - end_utc).total_seconds()

                        # ── WS fast-path: bypass grace if WS already flagged settled ──
                        if token_id and token_id in _ws_settled:
                            secs_after = max(secs_after, float(RESOLUTION_GRACE_SECS))
                            print(f"[{ts}] [WS-RES] fast-path triggered for "
                                  f"T{tier} {coin} #{trade_id} — skipping grace")

                        # v12.2: wait RESOLUTION_GRACE_SECS after close so UMA
                        # fully settles before reading outcomePrices from gamma.
                        if secs_after < RESOLUTION_GRACE_SECS:
                            print(f"[{ts}] [--] T{tier} {coin} #{trade_id} {side} | "
                                  f"closed {secs_after:.0f}s ago, waiting for "
                                  f"UMA finalization (need {RESOLUTION_GRACE_SECS}s)")
                            continue

                        # ── v12: CLOB-ONLY. Polymarket gamma is the sole source. ──
                        # No Chainlink / Coinbase / Binance fallback. If gamma
                        # is pending or unreachable, the trade stays open and
                        # the loop will retry next iteration. Tradeoff: brief
                        # gamma outages delay settlement. Benefit: every
                        # closed trade reflects PM's actual payout, no
                        # disagreements possible.
                        status = "pending"
                        p_start = p_end = None
                        resolution_source = None

                        pm_status, _pm_settled = await resolve_via_polymarket(
                            session, token_id, market_id)

                        if pm_status in ("won", "lost"):
                            status = pm_status
                            resolution_source = "pm"
                            try:
                                p_start, p_end = await get_chainlink_prices(
                                    coin, start_utc, end_utc)
                            except Exception:
                                p_start = p_end = None

                        if status == "pending":
                            print(f"[{ts}] [--] T{tier} {coin} #{trade_id} {side} | "
                                  f"closed {secs_after:.0f}s ago, waiting for price")
                            continue

                        if status not in ("won", "lost"):
                            continue

                        if status == "won":
                            pnl  = round(size / entry - size, 2)
                            icon = "[WIN]"
                        else:
                            pnl  = -size
                            icon = "[LOSS]"

                        cid = existing_cid
                        if not PAPER_TRADE and not cid:
                            try:
                                cid = await fetch_condition_id(
                                    session, token_id, market_id)
                            except Exception:
                                cid = ""

                        wrote_ok = close_trade(
                            trade_id, status, pnl,
                            p_start, p_end, cid,
                            resolution_source)
                        if not wrote_ok:
                            print(f"[{ts}] [WARN] DB write failed for #{trade_id}"
                                  f", will retry next poll")
                            continue

                        # Clean up WS fast-path entry now that trade is resolved
                        _ws_settled.pop(token_id, None)

                        if status == "won" and not PAPER_TRADE:
                            asyncio.create_task(
                                _sync_balance_after_win(
                                    trade_id, coin, tier, pnl, size, entry))

                        if p_start and p_end:
                            price_change = (p_end - p_start) / p_start * 100
                            price_str = (f"${p_start:.2f} → ${p_end:.2f} "
                                         f"({price_change:+.3f}%)")
                            tg_price  = (f"{p_start:.2f} → {p_end:.2f} "
                                         f"({price_change:+.3f}%)")
                        else:
                            price_change = 0.0
                            price_str    = "(price n/a)"
                            tg_price     = ""

                        src_tag = {"pm": "[PM]", "chainlink": "[CL]",
                                   "coinbase": "[CB]",
                                   "binance_fallback": "[BIN]"}.get(
                            resolution_source, "[?]")

                        print(f"[{ts}] {icon} T{tier} {coin} #{trade_id} {side} | "
                              f"{status.upper()} ${pnl:+.2f} {src_tag} | {price_str}")

                        if cid:
                            print(f"          condition_id = {cid[:18]}...")

                        _is_dup = token_id in _seen_tokens
                        _seen_tokens.add(token_id)

                        if not _is_dup:
                            log_event(icon,
                                      f"T{tier} {coin} {status} ${pnl:+.2f} {src_tag}")

                        # Send telegram now if PM settled it directly (authoritative),
                        # or if PM_VERIFY is disabled. Never send for duplicate tokens.
                        send_now = (resolution_source == "pm"
                                    or not PM_VERIFY) and not _is_dup
                        if send_now and TELEGRAM_ENABLED:
                            verb   = "WON" if status == "won" else "LOST"
                            emoji  = "✅" if status == "won" else "❌"
                            msg    = (f"{emoji} T{tier} {coin} {verb} ${pnl:+.2f} "
                                      f"@ ${entry:.3f} | {tg_price} {src_tag}")
                            asyncio.create_task(_tg_send(msg))

                        await asyncio.sleep(0.2)

            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERR] resolver loop: {e}")

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOPPED] Resolver stopped.")
