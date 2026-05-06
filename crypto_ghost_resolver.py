"""
Crypto Ghost Resolver v8 — Chainlink-direct (PM's actual UMA source)
======================================================================
Polymarket's "Will [BTC|ETH] be Up" 5-minute crypto markets settle via
Chainlink price feeds on Polygon (BTC/USD, ETH/USD, etc) with UMA
Optimistic Oracle as dispute layer. v8 reads those feeds DIRECTLY from
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

v8 changes:
  ! Chainlink-direct queries via web3.py (offloaded to a thread to keep
    the asyncio loop non-blocking). Same RPC list as the redeemer.
  ! POLYGON_RPC env var lets you point at Alchemy/Infura/QuickNode for
    faster/more-reliable lookups (recommended for high trade volume).
  ! If web3 not installed or Polygon unreachable, resolver continues
    on Coinbase → Binance. Bot never stalls on RPC issues.

v7 (kept the PM rule, replaced source):
  ! Coinbase 1m primary + Binance fallback. Correct rule, but not the
    LITERAL source PM uses. v8 fixes that.

v5.1 carryover (cherry-picked from cg123):
  + condition_id / redeemed / redeemed_at / redeem_tx_hash columns
  + fetch_condition_id() helper (gamma + data-api fallback)
  + Telegram alert on win/loss (silent if creds missing)
  + ZoneInfo("America/New_York") with fallback to fixed -4 offset
"""

import asyncio
import aiohttp
import sqlite3
import os
import sys
import re
from datetime import datetime, timezone, timedelta

# DST-safe ET timezone — falls back to fixed -4 offset if tzdata missing
try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:
    _ET_TZ = None

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    from dotenv import load_dotenv
    SD = os.path.dirname(os.path.abspath(__file__))
    ep = os.path.join(SD, ".env")
    if os.path.exists(ep):
        load_dotenv(ep, override=True)
        print(f"[ENV] Loaded: {ep}")
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
            print("[CL] web3 not installed → Chainlink disabled. "
                  "Install:  pip install web3")
            return False
        for rpc in POLYGON_RPCS:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
                if w3.is_connected():
                    self.w3 = w3
                    self.connected = True
                    print(f"[CL] Connected to Polygon: {rpc}")
                    break
            except Exception:
                continue
        if not self.connected:
            print("[CL] No Polygon RPC reachable → using Coinbase fallback.")
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
PM_VERIFY_DELAY  = int(os.getenv("PM_VERIFY_DELAY",  "60"))   # initial wait before first attempt
PM_VERIFY_RETRY  = int(os.getenv("PM_VERIFY_RETRY",  "5"))    # attempts before giving up
PM_VERIFY_BACKOFF= int(os.getenv("PM_VERIFY_BACKOFF","45"))   # additional sleep between retries
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(SCRIPT_DIR,
                "crypto_ghost_PAPER.db" if PAPER_TRADE else "crypto_ghost.db")

GAMMA_API    = "https://gamma-api.polymarket.com"
DATA_API     = "https://data-api.polymarket.com"
BINANCE_API  = "https://api.binance.com"
COINBASE_API = "https://api.exchange.coinbase.com"
POLL_INTERVAL = 10

# Coinbase product map for crypto markets (= what PM UMA oracle uses)
COINBASE_PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
}

def db_connect():
    return sqlite3.connect(DB_PATH, timeout=10)

def ensure_schema():
    if not os.path.exists(DB_PATH):
        return
    conn = db_connect()
    for ddl in [
        "ALTER TABLE trades ADD COLUMN price_start REAL",
        "ALTER TABLE trades ADD COLUMN price_end REAL",
        "ALTER TABLE trades ADD COLUMN resolution_verified INTEGER DEFAULT 0",
        # v5.1: condition_id required by redeemer for on-chain claims
        "ALTER TABLE trades ADD COLUMN condition_id TEXT",
        "ALTER TABLE trades ADD COLUMN redeemed INTEGER DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN redeemed_at TEXT",
        "ALTER TABLE trades ADD COLUMN redeem_tx_hash TEXT",
        "ALTER TABLE trades ADD COLUMN pm_result TEXT DEFAULT 'pending'",
    ]:
        try:
            conn.execute(ddl)
        except Exception:
            pass
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
      1. If market_id already looks like a condition_id, use it.
      2. Try gamma API by clob_token_ids.
      3. Try data-api positions by assetId.
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
                     "(SELECT id FROM events ORDER BY id DESC LIMIT 100)")
        conn.commit()
        conn.close()
    except Exception:
        pass

def parse_market_times(question: str):
    m = re.search(
        r'(\w+)\s+(\d+),\s+(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)',
        question, re.IGNORECASE
    )
    if not m:
        return None, None

    month_name, day, h1, m1, ap1, h2, m2, ap2 = m.groups()
    months = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
              "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
    mo = months.get(month_name.lower())
    if not mo:
        return None, None

    year = datetime.now(timezone.utc).year

    def to24(h_str, ap):
        h = int(h_str)
        if ap.upper() == "PM" and h != 12: h += 12
        if ap.upper() == "AM" and h == 12: h = 0
        return h

    try:
        if _ET_TZ is not None:
            # DST-safe: build the wall-clock ET datetime, then convert to UTC.
            start_et = datetime(year, mo, int(day), to24(h1, ap1), int(m1), tzinfo=_ET_TZ)
            end_et   = datetime(year, mo, int(day), to24(h2, ap2), int(m2), tzinfo=_ET_TZ)
            start_utc = start_et.astimezone(timezone.utc)
            end_utc   = end_et.astimezone(timezone.utc)
        else:
            # Fallback: assume EDT (UTC-4). Off by 1h Nov–Mar.
            start_utc = datetime(year, mo, int(day), to24(h1, ap1), int(m1),
                                 tzinfo=timezone.utc) + timedelta(hours=4)
            end_utc   = datetime(year, mo, int(day), to24(h2, ap2), int(m2),
                                 tzinfo=timezone.utc) + timedelta(hours=4)
        if end_utc < start_utc:
            end_utc += timedelta(days=1)
        return start_utc, end_utc
    except Exception:
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
    urls = [
        f"{GAMMA_API}/markets?clob_token_ids={token_id}",
    ]
    if market_id:
        urls.append(f"{GAMMA_API}/markets?id={market_id}")
    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    continue
                data = await r.json()
                if not isinstance(data, list) or not data:
                    continue
                m = data[0]
                if not m.get("closed"):
                    return "pending", None
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
                return ("won" if settled >= 0.5 else "lost"), settled
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
    "chainlink":          9,
    "coinbase":           1,
    "binance_fallback":   2,
    "binance":            2,
    # v9.2 PM gamma verification codes:
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
        """, (
            status, pnl,
            datetime.now(timezone.utc).isoformat(),
            p_start, p_end,
            code,
            condition_id,
            trade_id
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[WARN] DB write failed: {e}")


async def verify_against_pm_gamma(session, trade_id: int, token_id: str,
                                   market_id: str, local_status: str,
                                   local_pnl: float, size: float,
                                   entry: float, coin: str, side: str):
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
        print(f"[PM-VERIFY] ⚠ #{trade_id} {coin} {side} — gave up after "
              f"{PM_VERIFY_RETRY} attempts (PM still pending)")
        return

    if pm_status == local_status:
        # Match — just stamp the verification code so audits can see it
        try:
            conn = db_connect()
            conn.execute("""
                UPDATE trades SET resolution_verified = ?
                WHERE id = ? AND status = ?
            """, (RESOLUTION_CODE["pm_gamma_confirm"], trade_id, local_status))
            conn.commit()
            conn.close()
            print(f"[PM-VERIFY] ✓ #{trade_id} {coin} {side} PM={pm_status} "
                  f"matches local — confirmed (settled={pm_settled})")
        except Exception as e:
            print(f"[PM-VERIFY] DB stamp failed: {e}")
        return

    # ── OVERRIDE — PM disagrees with local resolution ──
    if pm_status == "won":
        new_pnl = round((size / entry) - size, 2) if entry > 0 else 0
        new_icon = "✅"
    else:
        new_pnl = -float(size)
        new_icon = "❌"
    delta = new_pnl - local_pnl
    print(f"[PM-VERIFY] *** OVERRIDE #{trade_id} {coin} {side}: "
          f"local={local_status} ${local_pnl:+.2f} → PM={pm_status} ${new_pnl:+.2f} "
          f"(delta ${delta:+.2f}, settled={pm_settled})")
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
        conn.close()
        log_event(new_icon, f"PM override #{trade_id} {coin}: {local_status}→{pm_status} "
                            f"${delta:+.2f}")
    except Exception as e:
        print(f"[PM-VERIFY] DB override failed: {e}")


async def _query_pm_by_market_id(session, market_id: str, our_outcome: str):
    """Fallback: query gamma by numeric market_id + outcome name."""
    if not market_id or not str(market_id).strip().isdigit():
        return None
    try:
        url = f"{GAMMA_API}/markets/{market_id}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                               headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status != 200:
                return None
            data = await r.json()
        import json as _json
        op = data.get("outcomePrices", "[]")
        oc = data.get("outcomes", "[]")
        if isinstance(op, str): op = _json.loads(op)
        if isinstance(oc, str): oc = _json.loads(oc)
        if not op or not oc:
            return None
        prices = [float(p) for p in op]
        if max(prices) < 0.99:
            return None  # not settled yet
        winner_idx = prices.index(max(prices))
        winner = str(oc[winner_idx]).upper()
        our    = (our_outcome or '').upper()
        return "won" if winner == our else "lost"
    except Exception:
        return None


async def pm_oracle_checker(session):
    """Background task: verify closed trades against PM gamma every 30s.
    Runs in the resolver so it works 24/7 on VPS without the dashboard."""
    while True:
        await asyncio.sleep(30)
        try:
            conn = db_connect()
            rows = conn.execute("""
                SELECT id, token_id, market_id, outcome, status,
                       size_usdc, entry_price, question, tier, coin
                FROM trades
                WHERE status IN ('won','lost')
                  AND COALESCE(pm_result,'pending') = 'pending'
                  AND closed_at > datetime('now','-30 days')
            """).fetchall()
            conn.close()
        except Exception:
            continue

        if rows:
            print(f"[PM-ORC] Checking {len(rows)} pending trades...")

        for row in rows:
            (tid, token_id, market_id, outcome, status,
             size_usdc, entry_price, question, tier, coin) = row

            pm_status = None
            pm_settled = None

            # Primary: token_id via clob_token_ids lookup
            if token_id:
                try:
                    pm_status, pm_settled = await resolve_via_polymarket(
                        session, token_id, market_id or '')
                    if pm_status == 'pending':
                        pm_status = None
                except Exception:
                    pm_status = None

            # Fallback: market_id + outcome name
            if pm_status is None:
                result = await _query_pm_by_market_id(session, market_id or '', outcome or '')
                if result is not None:
                    pm_status = result

            if pm_status is None:
                continue

            if pm_status == status:
                try:
                    conn = db_connect()
                    conn.execute("UPDATE trades SET pm_result=? WHERE id=?",
                                 (pm_status, tid))
                    conn.commit()
                    conn.close()
                    print(f"[PM-ORC] ✓ #{tid} {coin} confirmed: {pm_status} "
                          f"(settled={pm_settled})")
                except Exception:
                    pass
            else:
                side = (outcome or '').upper()
                old_pnl = size_usdc if status == 'won' else -size_usdc
                if pm_status == 'won':
                    new_pnl = round(size_usdc / entry_price - size_usdc, 2) if entry_price else 0
                    new_icon = "✅"
                else:
                    new_pnl = -float(size_usdc)
                    new_icon = "❌"
                delta = new_pnl - old_pnl
                try:
                    conn = db_connect()
                    conn.execute("""
                        UPDATE trades
                        SET status=?, pnl=?, pm_result=?, resolution_verified=?
                        WHERE id=?
                    """, (pm_status, new_pnl, pm_status,
                          RESOLUTION_CODE["pm_gamma_override"], tid))
                    conn.commit()
                    conn.close()
                    log_event(new_icon,
                              f"PM oracle #{tid} {coin}: {status}→{pm_status} ${delta:+.2f}")
                    print(f"[PM-ORC] *** OVERRIDE #{tid} {coin} {side}: "
                          f"{status}→{pm_status} ${delta:+.2f}")
                except Exception as e:
                    print(f"[PM-ORC] DB write failed: {e}")
                    continue

                if TELEGRAM_ENABLED:
                    old_emoji = "✅" if status == "won" else "❌"
                    try:
                        await _tg_send(
                            f"⚠️ <b>Oracle Correction — Trade #{tid}</b>\n"
                            f"{side} {question or 'N/A'}\n\n"
                            f"Entry: <b>${size_usdc:.2f}</b> @ ask <b>${entry_price:.3f}</b>\n\n"
                            f"Resolver: {old_emoji} {status.upper()}\n"
                            f"Oracle:   {new_icon} {pm_status.upper()}\n\n"
                            f"P&amp;L: <b>${old_pnl:+.2f}</b> → <b>${new_pnl:+.2f}</b>"
                        )
                    except Exception:
                        pass


async def main():
    tg     = "ON" if TELEGRAM_ENABLED else "off"
    verify = "ON" if PM_VERIFY else "off"
    print(f"""
╔═══════════════════════════════════════════════════════════╗
║   CRYPTO GHOST RESOLVER v9.2 — Chainlink + PM verify      ║
║   Mode: {"PAPER" if PAPER_TRADE else "LIVE":<8} | Telegram: {tg:<6} | PM-verify: {verify:<6}     ║
║   Primary: Chainlink (Polygon) | Fallback: Coinbase → BIN ║
║   DB: {os.path.basename(DB_PATH):<50}  ║
╚═══════════════════════════════════════════════════════════╝

Resolution rule (per Polymarket settlement docs):
  p_end >= p_start  →  UP wins   (flat counts as UP)
  p_end <  p_start  →  DOWN wins (DOWN requires strict drop)

Source tag in log:
  [CL]=chainlink   [CB]=coinbase fallback   [BIN]=binance fallback
PM verification ({PM_VERIFY_DELAY}s after close, up to {PM_VERIFY_RETRY} retries):
  [PM-VERIFY] ✓  PM matches local → confirmed
  [PM-VERIFY] *** OVERRIDE → DB updated with PM's actual result
    """)

    # Pre-warm the Chainlink connection so first trade isn't slow.
    await asyncio.to_thread(CHAINLINK._connect)

    ensure_schema()

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        asyncio.create_task(pm_oracle_checker(session))
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
                    for trade in open_trades:
                        (trade_id, tier, coin, entry, size, outcome, question,
                         market_id, token_id, existing_cid) = trade
                        ts = datetime.now().strftime("%H:%M:%S")

                        start_utc, end_utc = parse_market_times(question)
                        if start_utc is None:
                            print(f"[{ts}] ⚠ T{tier} {coin} #{trade_id}: can't parse question")
                            continue

                        now_utc = datetime.now(timezone.utc)
                        if now_utc < end_utc:
                            secs_to_close = (end_utc - now_utc).total_seconds()
                            print(f"[{ts}] ⏳ T{tier} {coin} #{trade_id} | {secs_to_close/60:.1f}min until close")
                            continue

                        bought_up = outcome.upper() in ("UP","YES","HIGHER","ABOVE")
                        side = "UP" if bought_up else "DOWN"
                        secs_after = (now_utc - end_utc).total_seconds()

                        # ── PRIMARY: Chainlink (PM's UMA source) → CB → BIN ──
                        status, p_start, p_end, resolution_source = (
                            await resolve_via_polymarket_source(
                                session, coin, start_utc, end_utc, bought_up
                            )
                        )

                        if status == "pending":
                            # <60s past close, or all 3 sources unreachable — retry.
                            print(f"[{ts}] ⏳ T{tier} {coin} #{trade_id} {side} | "
                                  f"closed {secs_after:.0f}s ago, waiting for price")
                            continue
                        if status not in ("won", "lost"):
                            continue

                        if status == "won":
                            pnl = round(size / entry - size, 2)
                            icon = "✅"
                        else:
                            pnl = -size
                            icon = "❌"

                        # Best-effort condition_id backfill — only for live, only if missing.
                        cid = existing_cid or ""
                        if not PAPER_TRADE and not cid:
                            try:
                                cid = await fetch_condition_id(session, token_id, market_id)
                            except Exception:
                                cid = ""

                        close_trade(trade_id, status, pnl, p_start, p_end,
                                    cid, source=resolution_source or "")

                        # v9.2 Mode B: spawn background PM gamma verification
                        if PM_VERIFY:
                            asyncio.create_task(
                                verify_against_pm_gamma(
                                    session, trade_id, token_id, market_id,
                                    status, pnl, size, entry, coin, side
                                )
                            )

                        if p_start and p_end:
                            price_change = ((p_end - p_start) / p_start) * 100
                            price_str = f"${p_start:.2f} → ${p_end:.2f} ({price_change:+.3f}%)"
                            tg_price  = f"{p_start:.2f} → {p_end:.2f} ({price_change:+.3f}%)"
                        else:
                            price_change = 0.0
                            price_str = "(price n/a)"
                            tg_price  = ""
                        src_tag = {
                            "chainlink": "[CL]",
                            "coinbase":  "[CB]",
                            "binance_fallback": "[BIN]",
                        }.get(resolution_source, "[?]")
                        print(f"[{ts}] {icon} T{tier} {coin} #{trade_id} {side} | "
                              f"{status.upper()} ${pnl:+.2f} {src_tag} | {price_str}")
                        if cid:
                            print(f"          condition_id = {cid[:18]}…")

                        # Fire-and-forget Telegram alert. Silent if creds missing.
                        if TELEGRAM_ENABLED:
                            try:
                                await _tg_send(
                                    f"{icon} <b>T{tier} {coin} {side}</b> "
                                    f"{status.upper()} <code>${pnl:+.2f}</code> {src_tag} "
                                    f"<b>#{trade_id}</b>\n"
                                    f"{question[:80]}\n"
                                    f"{tg_price}".rstrip()
                                )
                            except Exception:
                                pass

                        await asyncio.sleep(0.2)

            except Exception as e:
                print(f"[WARN] Loop error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOPPED] Resolver v8 exited.")
