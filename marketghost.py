"""
MarketGhost v3 — 24/7 Polymarket crypto Up/Down market data collector

Strategy: focus sampling on markets in their FINAL 10 minutes.
Adaptive sampling tiers by time-to-close + compression depth.

Added vs v3 base:
  - CLOB orderbook for real bid/ask prices
  - Binance trend (dev_1h, dev_1m, current price) cached per coin
  - ask_velocity: change in cheap_ask per minute
  - required_move_pct: how much BTC/ETH needs to move for cheap side to win

Usage:  python marketghost.py
"""

import asyncio
import aiohttp
import sqlite3
import time
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── CONFIG ────────────────────────────────────────────────────────────────────
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"
DB_PATH     = "marketghost.db"
MIN_LIQ_USD = 0.50
LOOP_SLEEP  = 3


# ── ADAPTIVE SAMPLING ────────────────────────────────────────────────────────
def sample_interval(mins_left: float, cheap_ask: float) -> int | None:
    """Return snapshot interval (seconds) or None to skip (> 10 min left)."""
    if mins_left is None or mins_left <= 0 or mins_left > 10:
        return None
    if   mins_left > 5: base = 60
    elif mins_left > 2: base = 20
    elif mins_left > 1: base =  5
    else:               base =  2
    if cheap_ask is not None:
        if   cheap_ask < 0.01: return 1
        elif cheap_ask < 0.05: return 2
        elif cheap_ask < 0.10: return max(2, base // 4)
        elif cheap_ask < 0.20: return max(5, base // 2)
    return base


def comp_zone(cheap_ask) -> str:
    if cheap_ask is None or cheap_ask >= 0.95: return 'none'
    if cheap_ask < 0.01: return 'extreme'
    if cheap_ask < 0.05: return 'deep'
    if cheap_ask < 0.10: return 'medium'
    if cheap_ask < 0.20: return 'light'
    return 'none'


# ── DATABASE ─────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            market_id        TEXT PRIMARY KEY,
            slug             TEXT,
            question         TEXT,
            coin             TEXT,
            duration_minutes INTEGER,
            start_utc        TEXT,
            end_utc          TEXT,
            first_seen       TEXT,
            open_price       REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id               TEXT NOT NULL,
            ts_utc                  TEXT NOT NULL,
            mins_to_close           REAL,
            up_bid                  REAL,
            up_ask                  REAL,
            down_bid                REAL,
            down_ask                REAL,
            up_token_id             TEXT,
            down_token_id           TEXT,
            volume                  REAL,
            liq_usd                 REAL,
            spread                  REAL,
            cheap_side              TEXT,
            cheap_ask               REAL,
            hour_utc                INTEGER,
            is_in_compression       INTEGER,
            compression_zone        TEXT,
            price_moved_since_last  REAL,
            is_active_compression   INTEGER,
            snapshot_interval_used  INTEGER,
            binance_price           REAL,
            trend_dev_1h            REAL,
            binance_1m_dev          REAL,
            ask_velocity            REAL,
            required_move_pct       REAL,
            UNIQUE(market_id, ts_utc)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS resolutions (
            market_id        TEXT PRIMARY KEY,
            coin             TEXT,
            start_utc        TEXT,
            end_utc          TEXT,
            winner           TEXT,
            up_final_price   REAL,
            down_final_price REAL,
            resolved_at      TEXT
        )
    """)

    # Migrate snapshots table: add columns that may be missing from older DBs
    new_cols = [
        "up_token_id TEXT", "down_token_id TEXT", "volume REAL", "liq_usd REAL",
        "spread REAL", "cheap_side TEXT", "cheap_ask REAL", "hour_utc INTEGER",
        "is_in_compression INTEGER", "compression_zone TEXT",
        "price_moved_since_last REAL", "is_active_compression INTEGER",
        "snapshot_interval_used INTEGER", "binance_price REAL",
        "trend_dev_1h REAL", "binance_1m_dev REAL",
        "ask_velocity REAL", "required_move_pct REAL",
    ]
    for col_def in new_cols:
        try:
            c.execute(f"ALTER TABLE snapshots ADD COLUMN {col_def}")
        except Exception:
            pass  # column already exists

    # Migrate resolutions table: add PM ground truth columns
    for col_def in ["pm_winner TEXT", "binance_winner TEXT", "resolution_source TEXT"]:
        try:
            c.execute(f"ALTER TABLE resolutions ADD COLUMN {col_def}")
        except Exception:
            pass

    c.execute("DROP TABLE IF EXISTS markets_compression_summary")
    c.execute("""
        CREATE TABLE markets_compression_summary (
            market_id                 TEXT PRIMARY KEY,
            min_cheap_ask_ever        REAL,
            compression_zone_depth    TEXT,
            did_reach_extreme         INTEGER,
            did_reach_deep            INTEGER,
            did_reach_medium          INTEGER,
            compression_duration_secs INTEGER,
            total_snapshots           INTEGER,
            compression_snapshots     INTEGER,
            first_snap_ts             TEXT,
            last_snap_ts              TEXT
        )
    """)

    conn.commit()
    conn.close()
    print("[DB] Schema ready")


# ── HELPERS ──────────────────────────────────────────────────────────────────
def get_coin(question: str) -> str:
    q = question.upper()
    for full, short in [("BITCOIN","BTC"), ("ETHEREUM","ETH"), ("SOLANA","SOL")]:
        if full in q: return short
    for c in ["BTC", "ETH", "SOL", "XRP"]:
        if c in q: return c
    return "OTHER"


def parse_mins_left(market: dict, now: datetime) -> float | None:
    try:
        s = (market.get("endDateIso") or market.get("endDate") or "").strip()
        if not s: return None
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - now).total_seconds() / 60.0
    except Exception:
        return None


def parse_liq(market: dict) -> float:
    try: return float(market.get("liquidityAmm") or market.get("liquidity") or 0)
    except Exception: return 0.0


def parse_token_ids(market: dict):
    """Return (up_tok, down_tok) by matching outcomes array.
    Tries clobTokenIds first, then tokens array (newer Gamma API format)."""
    try:
        cti = market.get("clobTokenIds") or market.get("clob_token_ids") or "[]"
        oc  = market.get("outcomes", "[]")
        if isinstance(cti, str): cti = json.loads(cti)
        if isinstance(oc,  str): oc  = json.loads(oc)

        # Fallback: newer Gamma API returns tokens[] array instead of clobTokenIds
        if len(cti) < 2:
            tokens = market.get("tokens") or []
            if isinstance(tokens, list) and len(tokens) >= 2:
                cti = [t.get("token_id") or t.get("id") for t in tokens if isinstance(t, dict)]
                oc  = [t.get("outcome", "") for t in tokens if isinstance(t, dict)]

        if len(cti) < 2: return None, None
        up_tok = down_tok = None
        for i, o in enumerate(oc):
            if str(o).upper() in ("UP", "YES", "HIGHER", "ABOVE"):
                up_tok = cti[i]
            else:
                down_tok = cti[i]
        # Fallback: just use index 0/1
        if not up_tok:   up_tok   = cti[0]
        if not down_tok: down_tok = cti[1]
        return up_tok, down_tok
    except Exception:
        return None, None


# ── CLOB ORDERBOOK ───────────────────────────────────────────────────────────
async def fetch_orderbook(session, token_id: str):
    """Returns (best_bid, best_ask, ask_size) from CLOB."""
    try:
        async with session.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=aiohttp.ClientTimeout(total=6)
        ) as r:
            if r.status != 200: return None, None, 0.0
            data  = await r.json()
            bids  = sorted(data.get("bids", []), key=lambda x: float(x.get("price", 0)), reverse=True)
            asks  = sorted(data.get("asks", []), key=lambda x: float(x.get("price", 1)))
            bid   = float(bids[0]["price"]) if bids else 0.0
            ask   = float(asks[0]["price"]) if asks else 0.0
            size  = float(asks[0]["size"])  if asks else 0.0
            return bid, ask, size
    except Exception:
        return None, None, 0.0


# ── BINANCE TREND (cached per coin, 13s TTL) ─────────────────────────────────
_trend_cache: dict = {}
_TREND_TTL = 13


async def get_binance_trend(session, coin: str):
    """Returns (price_now, dev_1h, dev_1m). Cached 13s."""
    now = time.time()
    cached = _trend_cache.get(coin)
    if cached and now - cached[0] < _TREND_TTL:
        return cached[1], cached[2], cached[3]
    symbol = f"{coin}USDT"
    try:
        async with session.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 2},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200: return None, None, None
            data = await r.json()
            if len(data) < 2: return None, None, None
            price_now    = float(data[1][4])
            price_1h_ago = float(data[0][1])
            dev_1h = round((price_now - price_1h_ago) / price_1h_ago, 6)

        dev_1m = None
        async with session.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 2},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status == 200:
                data = await r.json()
                if len(data) >= 2:
                    dev_1m = round((price_now - float(data[0][1])) / float(data[0][1]), 6)

        _trend_cache[coin] = (now, price_now, dev_1h, dev_1m)
        return price_now, dev_1h, dev_1m
    except Exception:
        return None, None, None


async def get_binance_price_at(session, coin: str, ts: datetime):
    target_ms = int(ts.timestamp() * 1000)
    try:
        async with session.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": f"{coin}USDT", "interval": "1m",
                    "startTime": target_ms - 60000,
                    "endTime":   target_ms + 60000, "limit": 3},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200: return None
            data = await r.json()
            if not data: return None
            return float(min(data, key=lambda c: abs(c[0] - target_ms))[4])
    except Exception:
        return None


# ── MARKET DISCOVERY ─────────────────────────────────────────────────────────
def build_expected_slugs() -> list:
    """Builds slug candidates for current + upcoming 5m and 15m markets.
    Format: {coin}-updown-{5m|15m}-{unix_end_timestamp}
    Covers BTC/ETH/SOL/XRP/BNB. Returns list of (coin, slug) tuples."""
    import time as _time
    now_ts = int(_time.time())
    SLUG_COINS = [("BTC","btc"), ("ETH","eth"), ("SOL","sol"), ("XRP","xrp"), ("BNB","bnb")]

    curr_5m = ((now_ts // 300) + 1) * 300
    slots_5m = [(curr_5m + i * 300, "5m") for i in range(-1, 4)]

    curr_15m = ((now_ts // 900) + 1) * 900
    slots_15m = [(curr_15m + i * 900, "15m") for i in range(-1, 3)]

    slugs = []
    for end_ts, interval in slots_5m + slots_15m:
        for coin, slug_coin in SLUG_COINS:
            slugs.append((coin, f"{slug_coin}-updown-{interval}-{end_ts}"))
    return slugs


async def _lookup_slug(session, slug: str):
    try:
        async with session.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug},
            timeout=aiohttp.ClientTimeout(total=6)
        ) as r:
            if r.status != 200: return None
            data = await r.json()
            return data[0] if isinstance(data, list) and data else None
    except Exception:
        return None


async def fetch_markets(session) -> list:
    """Slug-based discovery (primary) + Gamma sweep fallback.
    Slug lookup returns full market objects with clobTokenIds reliably."""
    seen_ids = set()
    markets  = []

    # Primary: hit known slugs directly
    for coin, slug in build_expected_slugs():
        m = await _lookup_slug(session, slug)
        if not m:
            continue
        mid = str(m.get("id", ""))
        if not mid or mid in seen_ids:
            continue
        seen_ids.add(mid)
        markets.append(m)
        await asyncio.sleep(0.05)

    # Fallback: Gamma sweep catches any markets the slug pattern missed
    if len(markets) < 3:
        try:
            async with session.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "archived": "false", "closed": "false",
                        "limit": 200, "order": "endDateIso", "ascending": "true"},
                timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                if r.status == 200:
                    for m in (await r.json() or []):
                        mid = str(m.get("id", ""))
                        if mid in seen_ids: continue
                        q = (m.get("question") or "").lower()
                        if (any(x in q for x in ["up or down", "higher or lower"]) and
                                any(x in q for x in ["bitcoin","ethereum","solana","xrp","btc","eth","sol","bnb"])):
                            seen_ids.add(mid)
                            markets.append(m)
        except Exception as e:
            print(f"[WARN] fetch_markets fallback: {e}")

    return markets


# ── RESOLUTION (PM ground truth) ─────────────────────────────────────────────
async def fetch_pm_winner(session, slug: str):
    """Returns 'UP'/'DOWN' from Polymarket's actual oracle, or None if not finalized."""
    try:
        async with session.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug, "closed": "true"},
            timeout=aiohttp.ClientTimeout(total=6)
        ) as r:
            if r.status != 200: return None
            data = await r.json()
            if not data: return None
            cid = data[0].get("conditionId")
            if not cid: return None
        async with session.get(
            f"{CLOB_API}/markets/{cid}",
            timeout=aiohttp.ClientTimeout(total=6)
        ) as cr:
            if cr.status != 200: return None
            cm = await cr.json()
            for tok in cm.get("tokens", []):
                if tok.get("winner"):
                    outcome = (tok.get("outcome") or "").upper()
                    if outcome in ("UP", "YES", "HIGHER", "ABOVE"): return "UP"
                    if outcome in ("DOWN", "NO", "LOWER", "BELOW"):  return "DOWN"
                    return outcome
    except Exception:
        pass
    return None


async def fetch_resolutions(session, conn):
    """Resolve closed markets using PM oracle first, Binance fallback after 60 min."""
    now = datetime.now(timezone.utc)
    pending = conn.execute("""
        SELECT m.market_id, m.coin, m.start_utc, m.end_utc, m.slug
        FROM markets m
        LEFT JOIN resolutions r ON m.market_id = r.market_id
        WHERE r.market_id IS NULL
          AND datetime(m.end_utc) < datetime(?)
        ORDER BY m.end_utc DESC LIMIT 50
    """, (now.isoformat(),)).fetchall()

    if not pending:
        return

    res_pm = res_bnc = skipped = 0
    for market_id, coin, start_iso, end_iso, slug in pending:
        try:
            end_utc = datetime.fromisoformat(end_iso)
        except Exception:
            continue
        secs_since = (now - end_utc).total_seconds()

        # 15-min grace: PM oracle needs time to finalize
        if secs_since < 900:
            skipped += 1
            continue

        # Primary: PM ground truth
        pm_winner = None
        if slug:
            pm_winner = await fetch_pm_winner(session, slug)

        # Always fetch Binance for audit trail
        symbol = f"{coin}USDT"
        try:
            start_utc = datetime.fromisoformat(start_iso)
        except Exception:
            start_utc = None
        p_start = await get_binance_price_at(session, coin, start_utc) if start_utc else None
        p_end   = await get_binance_price_at(session, coin, end_utc)
        binance_winner = ("UP" if p_end >= p_start else "DOWN") if (p_start and p_end) else None

        # Decide canonical winner
        if pm_winner in ("UP", "DOWN"):
            winner = pm_winner
            source = "polymarket"
            res_pm += 1
        elif secs_since < 3600:
            # PM not resolved yet, market still fresh — skip, retry next cycle
            skipped += 1
            continue
        elif binance_winner in ("UP", "DOWN"):
            winner = binance_winner
            source = "binance_fallback"
            res_bnc += 1
        else:
            skipped += 1
            continue

        final_snap = conn.execute("""
            SELECT up_ask, down_ask FROM snapshots
            WHERE market_id=? ORDER BY ts_utc DESC LIMIT 1
        """, (market_id,)).fetchone()

        conn.execute("""
            INSERT OR REPLACE INTO resolutions
            (market_id,coin,start_utc,end_utc,winner,
             up_final_price,down_final_price,resolved_at,
             pm_winner,binance_winner,resolution_source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (market_id, coin, start_iso, end_iso, winner,
              final_snap[0] if final_snap else None,
              final_snap[1] if final_snap else None,
              now.isoformat(), pm_winner, binance_winner, source))
        await asyncio.sleep(0.15)

    conn.commit()
    if res_pm or res_bnc:
        print(f"[RES] PM={res_pm}  Binance fallback={res_bnc}  skipped={skipped}")


# ── DB WRITES ─────────────────────────────────────────────────────────────────
def upsert_market(conn, market: dict, open_price=None):
    c = conn.cursor()
    duration = None
    try:
        s = datetime.fromisoformat((market.get("creationTime") or "").replace("Z", "+00:00"))
        e = datetime.fromisoformat((market.get("endDateIso")   or "").replace("Z", "+00:00"))
        duration = int((e - s).total_seconds() / 60)
    except Exception:
        pass
    c.execute("""
        INSERT OR IGNORE INTO markets
        (market_id,slug,question,coin,duration_minutes,start_utc,end_utc,first_seen,open_price)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (market.get("id"), market.get("slug"), market.get("question"),
          get_coin(market.get("question", "")), duration,
          market.get("creationTime"), market.get("endDateIso"),
          datetime.now(timezone.utc).isoformat(),
          open_price))
    conn.commit()


def store_snapshot(conn, snap: dict) -> bool:
    try:
        conn.execute("""
            INSERT OR IGNORE INTO snapshots
            (market_id,ts_utc,mins_to_close,
             up_bid,up_ask,down_bid,down_ask,up_token_id,down_token_id,
             volume,liq_usd,spread,cheap_side,cheap_ask,hour_utc,
             is_in_compression,compression_zone,
             price_moved_since_last,is_active_compression,snapshot_interval_used,
             binance_price,trend_dev_1h,binance_1m_dev,ask_velocity,required_move_pct)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (snap["market_id"], snap["ts_utc"], snap.get("mins_to_close"),
              snap.get("up_bid"),   snap.get("up_ask"),
              snap.get("down_bid"), snap.get("down_ask"),
              snap.get("up_tok"),   snap.get("down_tok"),
              snap.get("volume"), snap.get("liq_usd"), snap.get("spread"),
              snap.get("cheap_side"), snap.get("cheap_ask"), snap.get("hour_utc"),
              1 if snap.get("in_comp") else 0, snap.get("zone"),
              snap.get("price_moved"), 1 if snap.get("is_active") else 0,
              snap.get("interval"),
              snap.get("binance_price"), snap.get("trend_dev_1h"),
              snap.get("binance_1m_dev"), snap.get("ask_velocity"),
              snap.get("required_move_pct")))
        conn.commit()
        return True
    except Exception as e:
        print(f"[ERROR] store_snapshot: {e}")
        return False


def rebuild_compression_summary(conn):
    try:
        conn.execute("""
            INSERT OR REPLACE INTO markets_compression_summary
            SELECT
                market_id,
                MIN(cheap_ask),
                CASE WHEN MIN(cheap_ask)<0.01 THEN 'extreme'
                     WHEN MIN(cheap_ask)<0.05 THEN 'deep'
                     WHEN MIN(cheap_ask)<0.10 THEN 'medium'
                     WHEN MIN(cheap_ask)<0.20 THEN 'light'
                     ELSE 'none' END,
                CAST(MIN(cheap_ask)<0.01 AS INTEGER),
                CAST(MIN(cheap_ask)<0.05 AS INTEGER),
                CAST(MIN(cheap_ask)<0.10 AS INTEGER),
                CAST(
                    (julianday(MAX(CASE WHEN is_in_compression=1 THEN ts_utc END))
                   - julianday(MIN(CASE WHEN is_in_compression=1 THEN ts_utc END)))*86400
                AS INTEGER),
                COUNT(*),
                SUM(is_in_compression),
                MIN(ts_utc),
                MAX(ts_utc)
            FROM snapshots WHERE cheap_ask IS NOT NULL
            GROUP BY market_id
        """)
        conn.commit()
        print("[INFO] Compression summary rebuilt")
    except Exception as e:
        print(f"[ERROR] rebuild_compression_summary: {e}")


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
async def main():
    init_db()

    last_snap_ts    = defaultdict(float)
    last_cheap_ask  = {}
    last_mins       = {}
    open_prices     = {}   # market_id → open_price (Binance at start)

    connector = aiohttp.TCPConnector(limit_per_host=5, limit=50)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
    ) as session:

        conn      = sqlite3.connect(DB_PATH, timeout=10)
        iteration = 0
        res_tick  = 0

        while True:
            try:
                iteration += 1
                now    = datetime.now(timezone.utc)
                now_ts = time.time()
                print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')} UTC]  Iter {iteration}")

                markets = await fetch_markets(session)
                print(f"[INFO] {len(markets)} active markets found")

                ins = skip_early = skip_liq = skip_int = skip_np = 0

                for market in markets:
                    mid = market.get("id")
                    if not mid: continue

                    mins_left = parse_mins_left(market, now)
                    if mins_left is None or mins_left > 10:
                        skip_early += 1
                        continue

                    coin = get_coin(market.get("question", ""))

                    # Token IDs
                    up_tok, down_tok = parse_token_ids(market)
                    if not up_tok or not down_tok:
                        skip_np += 1
                        continue

                    # CLOB orderbook (real bid/ask)
                    up_bid,   up_ask,   up_liq   = await fetch_orderbook(session, up_tok)
                    down_bid, down_ask, down_liq = await fetch_orderbook(session, down_tok)

                    # Fallback to outcomePrices if CLOB fails
                    if up_ask is None or down_ask is None:
                        op = market.get("outcomePrices", "[]")
                        if isinstance(op, str):
                            try: op = json.loads(op)
                            except Exception: op = []
                        if op and len(op) >= 2:
                            up_ask   = up_ask   or float(op[0])
                            down_ask = down_ask or float(op[1])

                    if up_ask is None or down_ask is None:
                        skip_np += 1
                        continue

                    cheap_side = "UP"   if up_ask <= down_ask else "DOWN"
                    cheap_ask  = up_ask if cheap_side == "UP"  else down_ask

                    liq_usd = parse_liq(market)
                    if liq_usd < MIN_LIQ_USD:
                        skip_liq += 1
                        continue

                    interval = sample_interval(mins_left, cheap_ask)
                    if interval is None:
                        skip_early += 1
                        continue

                    prev_ask  = last_cheap_ask.get(mid)
                    price_moved = abs(cheap_ask - prev_ask) if prev_ask is not None else None
                    is_active   = price_moved is not None and price_moved > 0.02

                    if not is_active and (now_ts - last_snap_ts[mid]) < interval:
                        skip_int += 1
                        continue

                    # Register market if first time seen
                    if mid not in open_prices:
                        open_price = None
                        try:
                            start_str = (market.get("creationTime") or "").replace("Z", "+00:00")
                            if start_str:
                                start_utc = datetime.fromisoformat(start_str)
                                if start_utc.tzinfo is None:
                                    start_utc = start_utc.replace(tzinfo=timezone.utc)
                                open_price = await get_binance_price_at(session, coin, start_utc)
                        except Exception:
                            pass
                        open_prices[mid] = open_price
                        upsert_market(conn, market, open_price)

                    # Binance trend (cached per coin)
                    bnc_price, dev_1h, dev_1m = await get_binance_trend(session, coin)

                    # ask_velocity: change in cheap_ask per minute
                    ask_velocity = None
                    prev_mins = last_mins.get(mid)
                    if prev_ask is not None and prev_mins is not None and cheap_ask is not None:
                        time_diff = prev_mins - mins_left
                        if time_diff > 0:
                            ask_velocity = round((cheap_ask - prev_ask) / time_diff, 6)

                    # required_move_pct: how much coin needs to move for cheap side to win
                    required_move_pct = None
                    open_p = open_prices.get(mid)
                    if bnc_price and open_p:
                        if cheap_side == "UP":
                            required_move_pct = round((open_p - bnc_price) / bnc_price, 6)
                        else:
                            required_move_pct = round((bnc_price - open_p) / bnc_price, 6)

                    zone = comp_zone(cheap_ask)
                    snap = {
                        "market_id":        mid,
                        "ts_utc":           now.isoformat(),
                        "mins_to_close":    mins_left,
                        "up_bid":           up_bid,   "up_ask":   up_ask,
                        "down_bid":         down_bid, "down_ask": down_ask,
                        "up_tok":           up_tok,   "down_tok": down_tok,
                        "volume":           float(market.get("volume", 0) or 0),
                        "liq_usd":          liq_usd,
                        "spread":           abs(up_ask - down_ask),
                        "cheap_side":       cheap_side,
                        "cheap_ask":        cheap_ask,
                        "hour_utc":         now.hour,
                        "in_comp":          zone != "none",
                        "zone":             zone,
                        "price_moved":      price_moved,
                        "is_active":        is_active,
                        "interval":         interval,
                        "binance_price":    bnc_price,
                        "trend_dev_1h":     dev_1h,
                        "binance_1m_dev":   dev_1m,
                        "ask_velocity":     ask_velocity,
                        "required_move_pct": required_move_pct,
                    }

                    if store_snapshot(conn, snap):
                        ins += 1
                        last_snap_ts[mid]   = now_ts
                        last_cheap_ask[mid] = cheap_ask
                        last_mins[mid]      = mins_left

                print(f"[INFO] Inserted {ins} | "
                      f"too-early={skip_early}  liq={skip_liq}  "
                      f"interval={skip_int}  no-prices={skip_np}")

                res_tick += 1
                if res_tick % 5 == 0:
                    await fetch_resolutions(session, conn)
                if iteration % 24 == 0:
                    rebuild_compression_summary(conn)

                await asyncio.sleep(LOOP_SLEEP)

            except (KeyboardInterrupt, asyncio.CancelledError):
                print("\n[STOP] Shutting down...")
                rebuild_compression_summary(conn)
                conn.close()
                break
            except Exception as e:
                print(f"[ERROR] Iter {iteration}: {e}")
                await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(main())
