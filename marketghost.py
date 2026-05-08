"""
MarketGhost v1.0 — 24/7 Polymarket Crypto Data Collector
=========================================================
Runs alongside your trading bot, silently collecting market data.
Does NOT trade. Does NOT interfere. Read-only observation.

What it does:
  1. Every 60 seconds, fetches every active BTC/ETH/SOL/XRP Up/Down market
  2. Stores a full snapshot: bid/ask, liquidity, volume, minutes-to-close
  3. When markets end, records the outcome from Binance (ground truth)
  4. All data lives in marketghost.db (separate from trading DB)

Why this matters:
  - Lets you BACKTEST filter ideas against real data, not just guesses
  - Answers questions like "does WR improve at X price?" with actual stats
  - Builds a 30+ day archive so you can validate strategy over time
  - Completely independent of the trading bot — can't break it

Run with: python marketghost.py
Stop with: Ctrl+C
"""

import asyncio
import aiohttp
import sqlite3
import os
import sys
import re
import json
import time
from datetime import datetime, timezone, timedelta

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    from dotenv import load_dotenv
    SD = os.path.dirname(os.path.abspath(__file__))
    ep = os.path.join(SD, ".env")
    if os.path.exists(ep):
        load_dotenv(ep, override=True)
except ImportError:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(SCRIPT_DIR, "marketghost.db")
GAMMA_API   = "https://gamma-api.polymarket.com"
DATA_API    = "https://data-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"

# Wallet-based discovery removed by default — Polymarket-only.
# Set WALLET_DISCOVERY=true in .env to re-enable third-party wallet
# market discovery. Default = false.
DISCOVERY_WALLET = ""
USE_WALLET_DISCOVERY = os.getenv("WALLET_DISCOVERY", "false").strip().lower() in ("true", "1", "yes")
HEADERS = {"User-Agent": "Mozilla/5.0"}

SNAPSHOT_INTERVAL = 15   # seconds between snapshots
RESOLUTION_INTERVAL = 300  # check for resolutions every 5 min

# ─── DB SETUP ─────────────────────────────────────────────────────────────────
def db_connect():
    return sqlite3.connect(DB_PATH, timeout=10)

def init_db():
    conn = db_connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS markets (
            market_id TEXT PRIMARY KEY,
            slug TEXT,
            question TEXT,
            coin TEXT,
            duration_minutes INTEGER,
            start_utc TEXT,
            end_utc TEXT,
            first_seen TEXT,
            open_price REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            ts_utc TEXT,
            mins_to_close REAL,
            up_bid REAL, up_ask REAL,
            down_bid REAL, down_ask REAL,
            up_token_id TEXT, down_token_id TEXT,
            volume REAL,
            liquidity REAL,
            spread REAL,
            binance_price REAL,
            trend_dev_1h REAL,
            liq_usd REAL,
            cheap_side TEXT,
            cheap_ask REAL,
            hour_et INTEGER,
            required_move_pct REAL,
            binance_1m_dev REAL,
            ask_velocity REAL,
            FOREIGN KEY (market_id) REFERENCES markets(market_id)
        );

        CREATE TABLE IF NOT EXISTS resolutions (
            market_id TEXT PRIMARY KEY,
            coin TEXT,
            start_utc TEXT,
            end_utc TEXT,
            price_start REAL,
            price_end REAL,
            winner TEXT,
            up_final_price REAL,
            down_final_price REAL,
            resolved_at TEXT,
            winner_source TEXT DEFAULT 'polymarket',
            FOREIGN KEY (market_id) REFERENCES markets(market_id)
        );

        CREATE INDEX IF NOT EXISTS idx_snap_market ON snapshots(market_id);
        CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts_utc);
        CREATE INDEX IF NOT EXISTS idx_mkt_end ON markets(end_utc);
    """)
    # Migrate existing DB — safe if columns already exist
    for col in ["binance_price REAL", "trend_dev_1h REAL",
                "liq_usd REAL", "cheap_side TEXT", "cheap_ask REAL", "hour_et INTEGER",
                "required_move_pct REAL", "binance_1m_dev REAL", "ask_velocity REAL"]:
        try:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col}")
        except Exception:
            pass
    for col in ["open_price REAL"]:
        try:
            conn.execute(f"ALTER TABLE markets ADD COLUMN {col}")
        except Exception:
            pass
    for col in ["winner_source TEXT"]:
        try:
            conn.execute(f"ALTER TABLE resolutions ADD COLUMN {col}")
        except Exception:
            pass
    conn.commit()
    conn.close()

# ─── PARSING ──────────────────────────────────────────────────────────────────
def parse_market_times(question: str):
    """Returns (start_utc, end_utc) from question text."""
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
    def to24(h, ap):
        h = int(h)
        if ap.upper() == "PM" and h != 12: h += 12
        if ap.upper() == "AM" and h == 12: h = 0
        return h
    try:
        start_et = datetime(year, mo, int(day), to24(h1, ap1), int(m1), tzinfo=timezone.utc)
        end_et   = datetime(year, mo, int(day), to24(h2, ap2), int(m2), tzinfo=timezone.utc)
        start_utc = start_et + timedelta(hours=4)
        end_utc   = end_et   + timedelta(hours=4)
        if end_utc < start_utc:
            end_utc += timedelta(days=1)
        return start_utc, end_utc
    except Exception:
        return None, None

def detect_coin(question: str):
    q = question.lower()
    if 'bitcoin' in q or ' btc ' in q: return 'BTC'
    if 'ethereum' in q or ' eth ' in q: return 'ETH'
    if 'solana' in q or ' sol ' in q: return 'SOL'
    if 'xrp' in q or 'ripple' in q: return 'XRP'
    return None

# ─── MARKET DISCOVERY — Polymarket only (wallet discovery off by default) ──
DATA_API = "https://data-api.polymarket.com"

def is_crypto_updown(title: str) -> bool:
    t = title.lower()
    if "up or down" not in t:
        return False
    return any(c in t for c in ["bitcoin", "ethereum", "solana", "xrp"])

def build_expected_slugs():
    """Build Polymarket slugs using correct unix timestamp format."""
    now = time.time()
    slugs = []
    coins = [("BTC", "btc"), ("ETH", "eth")]
    for interval_name, interval_secs in [("5m", 300), ("15m", 900)]:
        for window in range(-1, 6):
            end_ts = int((now // interval_secs + window) * interval_secs)
            for coin_label, coin_slug in coins:
                slug = f"{coin_slug}-updown-{interval_name}-{end_ts}"
                slugs.append((coin_label, slug))
    return slugs

async def fetch_via_wallet(session):
    """
    Disabled by default — third-party wallet discovery removed.
    Returns empty dict so downstream code still works. Set
    WALLET_DISCOVERY=true in .env AND configure DISCOVERY_WALLET
    locally to re-enable.
    """
    if not USE_WALLET_DISCOVERY or not DISCOVERY_WALLET:
        return {}
    markets = {}
    now = datetime.now(timezone.utc).timestamp()
    try:
        url = (f"{DATA_API}/activity?user={DISCOVERY_WALLET}"
               f"&type=TRADE&limit=100&sortBy=TIMESTAMP&sortDirection=DESC")
        async with session.get(url,
                               headers={"User-Agent": "Mozilla/5.0"},
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return markets
            trades = await r.json()
            if not isinstance(trades, list):
                return markets
            for t in trades:
                age = now - float(t.get("timestamp", 0))
                if age > 1800:
                    continue
                title = t.get("title", "")
                cid   = t.get("conditionId", "")
                if not is_crypto_updown(title) or not cid:
                    continue
                if cid in markets:
                    continue
                markets[cid] = {
                    "condition_id": cid,
                    "question": title,
                    "asset": str(t.get("asset", "")),
                }
    except Exception as e:
        print(f"[WARN] wallet discovery: {e}")
    return markets

async def lookup_market_by_slug(session, slug):
    """Direct slug lookup."""
    try:
        async with session.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug},
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if isinstance(data, list) and data:
                return data[0]
            return None
    except Exception:
        return None

async def lookup_market_by_condition_id(session, cid):
    """Look up full market data by condition_id (more reliable than slug)."""
    try:
        async with session.get(
            f"{GAMMA_API}/markets",
            params={"condition_ids": cid},
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if isinstance(data, list) and data:
                return data[0]
            return None
    except Exception:
        return None

async def fetch_active_markets(session):
    """Discover markets via gamma sweep (primary) + slug scan (fallback)."""
    markets = []
    seen_ids = set()

    def _parse_market(m, coin_fallback=None):
        mid = str(m.get("id", ""))
        if not mid or mid in seen_ids:
            return None
        q = m.get("question", "")
        if "up or down" not in q.lower():
            return None
        detected = detect_coin(q) or coin_fallback
        if not detected:
            return None
        end_utc = start_utc = None

        # Primary: extract end time from slug unix timestamp (most reliable)
        slug = m.get("slug", "")
        slug_match = re.search(r'-(\d{9,11})$', slug)
        if slug_match:
            try:
                end_ts = int(slug_match.group(1))
                end_utc = datetime.fromtimestamp(end_ts, tz=timezone.utc)
                if "-5m-" in slug:
                    start_utc = end_utc - timedelta(minutes=5)
                elif "-15m-" in slug:
                    start_utc = end_utc - timedelta(minutes=15)
            except Exception:
                end_utc = start_utc = None

        # Secondary: regex parse from question text (ET → UTC)
        if not end_utc:
            start_utc, end_utc = parse_market_times(q)

        # Last resort: ISO field — only use if it contains a time component
        if not end_utc:
            try:
                end_iso = m.get("endDateIso") or m.get("endDate") or ""
                if "T" in end_iso or "t" in end_iso:
                    dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    end_utc = dt
            except Exception:
                pass

        if not end_utc:
            return None
        seen_ids.add(mid)
        dur = int((end_utc - start_utc).total_seconds() / 60) if start_utc else 5
        return {
            "market_id": mid,
            "slug": m.get("slug", ""),
            "question": q,
            "coin": detected,
            "start_utc": start_utc or end_utc,
            "end_utc": end_utc,
            "duration_min": dur,
            "clob_token_ids": m.get("clobTokenIds", "[]"),
            "outcomes": m.get("outcomes", "[]"),
            "volume": float(m.get("volume", 0) or 0),
            "liquidity": float(m.get("liquidity", 0) or 0),
        }

    # Method 1: Gamma API active sweep (primary)
    for tag in ["crypto", "bitcoin", "ethereum"]:
        try:
            async with session.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false",
                        "tag": tag, "limit": 100},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    for m in (data if isinstance(data, list) else []):
                        parsed = _parse_market(m)
                        if parsed:
                            markets.append(parsed)
        except Exception as e:
            print(f"[WARN] gamma sweep ({tag}): {e}")
        await asyncio.sleep(0.1)

    # Method 2: Slug scan — always run, slug timestamp gives correct end_utc
    # Reset seen_ids so slug scan can re-parse markets Gamma found with wrong time
    seen_ids.clear()
    now_utc = datetime.now(timezone.utc)
    slug_markets = []
    for coin, slug in build_expected_slugs():
        m = await lookup_market_by_slug(session, slug)
        if not m:
            continue
        parsed = _parse_market(m, coin_fallback=coin)
        if parsed and parsed["end_utc"] > now_utc:
            slug_markets.append(parsed)
        await asyncio.sleep(0.05)

    # Merge: slug_markets override Gamma results for same market_id
    slug_ids = {m["market_id"] for m in slug_markets}
    markets = [m for m in markets if m["market_id"] not in slug_ids]
    markets.extend(slug_markets)

    return markets

async def fetch_orderbook(session, token_id: str):
    """Get best bid/ask for a token."""
    try:
        async with session.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200:
                return None, None, 0
            data = await r.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            # Sort: bids descending (best = highest first), asks ascending (best = lowest first)
            sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
            sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 1)))
            best_bid = float(sorted_bids[0]["price"]) if sorted_bids else 0.0
            best_ask = float(sorted_asks[0]["price"]) if sorted_asks else 0.0
            liq = float(sorted_asks[0]["size"]) if sorted_asks else 0.0
            return best_bid, best_ask, liq
    except Exception:
        return None, None, 0

# ─── BINANCE TREND ────────────────────────────────────────────────────────────
_trend_cache: dict = {}  # coin → (ts, price_now, dev_1h, dev_1m)
_TREND_CACHE_TTL = 13    # refresh every ~13s (aligns with 15s snapshot loop)

async def get_binance_trend(session, coin: str):
    """Returns (current_price, dev_1h, dev_1m) for coin. Cached 13s."""
    now = time.time()
    cached = _trend_cache.get(coin)
    if cached and now - cached[0] < _TREND_CACHE_TTL:
        return cached[1], cached[2], cached[3]

    symbol = f"{coin}USDT"
    try:
        # Fetch last 2 x 1h candles for 1h dev
        async with session.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 2},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200:
                return None, None, None
            data = await r.json()
            if len(data) < 2:
                return None, None, None
            price_1h_ago = float(data[0][1])
            price_now    = float(data[1][4])
            dev_1h = round((price_now - price_1h_ago) / price_1h_ago, 6)

        # Fetch last 2 x 1m candles for 1m dev
        dev_1m = None
        async with session.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 2},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status == 200:
                data = await r.json()
                if len(data) >= 2:
                    price_1m_ago = float(data[0][1])  # open of previous 1m candle
                    dev_1m = round((price_now - price_1m_ago) / price_1m_ago, 6)

        _trend_cache[coin] = (now, price_now, dev_1h, dev_1m)
        return price_now, dev_1h, dev_1m
    except Exception:
        return None, None, None

# ─── BINANCE RESOLUTION ───────────────────────────────────────────────────────
async def get_binance_price_at(session, symbol: str, ts: datetime):
    target_ms = int(ts.timestamp() * 1000)
    try:
        async with session.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": "1m",
                    "startTime": target_ms - 60000,
                    "endTime": target_ms + 60000, "limit": 3},
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

# ─── SNAPSHOT LOOP ────────────────────────────────────────────────────────────
async def snapshot_once(session):
    markets = await fetch_active_markets(session)
    if not markets:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] No active crypto markets found")
        return 0

    conn = db_connect()
    now = datetime.now(timezone.utc)
    registered = 0
    snapped = 0

    for m in markets:
        # Parse token IDs
        try:
            token_ids = json.loads(m["clob_token_ids"]) if isinstance(m["clob_token_ids"], str) else m["clob_token_ids"]
            outcomes  = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
        except Exception:
            continue
        if len(token_ids) < 2 or len(outcomes) < 2:
            continue

        # Figure out which token is UP vs DOWN
        up_token = down_token = None
        for i, oc in enumerate(outcomes):
            oc_upper = str(oc).upper()
            if oc_upper in ("UP","YES","HIGHER","ABOVE"):
                up_token = token_ids[i]
            else:
                down_token = token_ids[i]
        if not up_token or not down_token:
            continue

        # Register market if new
        existing = conn.execute(
            "SELECT market_id, open_price FROM markets WHERE market_id = ?", (m["market_id"],)
        ).fetchone()
        if not existing:
            symbol = f"{m['coin']}USDT"
            open_price = await get_binance_price_at(session, symbol, m["start_utc"])
            conn.execute("""
                INSERT INTO markets
                (market_id, slug, question, coin, duration_minutes,
                 start_utc, end_utc, first_seen, open_price)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (m["market_id"], m["slug"], m["question"], m["coin"],
                  m["duration_min"], m["start_utc"].isoformat(),
                  m["end_utc"].isoformat(), now.isoformat(), open_price))
            registered += 1
            open_price_row = open_price
        else:
            open_price_row = existing[1]

        # Only snapshot if market is active (started AND not yet ended)
        if now < m["start_utc"] or now > m["end_utc"]:
            continue

        # Fetch orderbooks
        up_bid, up_ask, up_liq = await fetch_orderbook(session, up_token)
        down_bid, down_ask, down_liq = await fetch_orderbook(session, down_token)

        if up_ask is None and down_ask is None:
            continue

        mins_to_close = (m["end_utc"] - now).total_seconds() / 60.0
        spread = (up_ask or 0) - (up_bid or 0) if up_ask and up_bid else 0

        # Fetch Binance trend (cached per coin per snapshot cycle)
        bnc_price, trend_dev, trend_dev_1m = await get_binance_trend(session, m["coin"])

        # Derived fields for bot signal analysis
        if up_ask is not None and down_ask is not None:
            if up_ask <= down_ask:
                cheap_side, cheap_ask = "UP", up_ask
            else:
                cheap_side, cheap_ask = "DOWN", down_ask
        elif up_ask is not None:
            cheap_side, cheap_ask = "UP", up_ask
        elif down_ask is not None:
            cheap_side, cheap_ask = "DOWN", down_ask
        else:
            cheap_side, cheap_ask = None, None

        # ask_velocity: change in cheap_ask vs previous snapshot (per minute)
        prev = conn.execute("""
            SELECT cheap_ask, mins_to_close FROM snapshots
            WHERE market_id = ? ORDER BY ts_utc DESC LIMIT 1
        """, (m["market_id"],)).fetchone()
        ask_velocity = None
        if prev and prev[0] is not None and cheap_ask is not None:
            time_diff_min = mins_to_close - prev[1] if prev[1] is not None else None
            if time_diff_min and time_diff_min != 0:
                ask_velocity = round((cheap_ask - prev[0]) / abs(time_diff_min), 6)

        liq_usd = round(cheap_ask * (up_liq if cheap_side == "UP" else down_liq), 4) if cheap_ask else None
        hour_et = (now - timedelta(hours=4)).hour  # UTC-4 approximation (ET)

        # required_move_pct: how much % BTC needs to move for cheap side to win
        required_move_pct = None
        if bnc_price and open_price_row:
            if cheap_side == "UP":
                required_move_pct = round((open_price_row - bnc_price) / bnc_price, 6)
            elif cheap_side == "DOWN":
                required_move_pct = round((bnc_price - open_price_row) / bnc_price, 6)

        conn.execute("""
            INSERT INTO snapshots
            (market_id, ts_utc, mins_to_close,
             up_bid, up_ask, down_bid, down_ask,
             up_token_id, down_token_id,
             volume, liquidity, spread,
             binance_price, trend_dev_1h,
             liq_usd, cheap_side, cheap_ask, hour_et,
             required_move_pct, binance_1m_dev, ask_velocity)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (m["market_id"], now.isoformat(), mins_to_close,
              up_bid, up_ask, down_bid, down_ask,
              up_token, down_token,
              m["volume"], max(up_liq, down_liq), spread,
              bnc_price, trend_dev,
              liq_usd, cheap_side, cheap_ask, hour_et,
              required_move_pct, trend_dev_1m, ask_velocity))
        snapped += 1
        await asyncio.sleep(0.1)  # be nice to CLOB

    conn.commit()
    conn.close()

    ts = datetime.now().strftime('%H:%M:%S')
    if registered:
        print(f"[{ts}] 📸 Snapshot: {snapped} markets | {registered} new registered")
    else:
        print(f"[{ts}] 📸 Snapshot: {snapped} markets")
    return snapped

# ─── RESOLUTION LOOP ──────────────────────────────────────────────────────────
async def resolve_pending(session):
    """For markets that ended but aren't resolved yet, fetch actual outcome from Polymarket."""
    conn = db_connect()
    now = datetime.now(timezone.utc)

    pending = conn.execute("""
        SELECT m.market_id, m.coin, m.start_utc, m.end_utc
        FROM markets m
        LEFT JOIN resolutions r ON m.market_id = r.market_id
        WHERE r.market_id IS NULL
          AND datetime(m.end_utc) < datetime(?)
        ORDER BY m.end_utc DESC
        LIMIT 50
    """, (now.isoformat(),)).fetchall()

    if not pending:
        conn.close()
        return 0

    resolved = 0
    for market_id, coin, start_iso, end_iso in pending:
        try:
            end_utc = datetime.fromisoformat(end_iso)
        except Exception:
            continue

        # Give PM 60s to settle before first attempt
        if (now - end_utc).total_seconds() < 60:
            continue

        # Get token IDs and final ask prices from last snapshot
        last_snap = conn.execute("""
            SELECT up_token_id, down_token_id, up_ask, down_ask FROM snapshots
            WHERE market_id = ?
            ORDER BY ts_utc DESC LIMIT 1
        """, (market_id,)).fetchone()

        if not last_snap or not last_snap[0]:
            continue  # no token IDs stored — can't query PM

        up_token   = last_snap[0]
        up_final   = last_snap[2]
        down_final = last_snap[3]

        # Query Polymarket gamma for actual settled outcomePrices
        winner = None
        try:
            async with session.get(
                f"{GAMMA_API}/markets",
                params={"clob_token_ids": up_token},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, list) and data:
                        m_data = data[0]
                        if m_data.get("closed"):
                            op  = m_data.get("outcomePrices", "[]")
                            oc  = m_data.get("outcomes", "[]")
                            if isinstance(op, str):
                                try: op = json.loads(op)
                                except Exception: op = []
                            if isinstance(oc, str):
                                try: oc = json.loads(oc)
                                except Exception: oc = []
                            if op and oc:
                                prices = [float(p) for p in op]
                                if max(prices) >= 0.99:
                                    winner_idx = prices.index(max(prices))
                                    winner_str = str(oc[winner_idx]).upper()
                                    winner = "UP" if winner_str in ("UP", "YES", "HIGHER", "ABOVE") else "DOWN"
        except Exception as e:
            print(f"[WARN] PM resolve {market_id}: {e}")

        if winner is None:
            # PM not settled yet — will retry next resolution cycle
            continue

        conn.execute("""
            INSERT OR REPLACE INTO resolutions
            (market_id, coin, start_utc, end_utc,
             price_start, price_end, winner,
             up_final_price, down_final_price, resolved_at, winner_source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (market_id, coin, start_iso, end_iso,
              None, None, winner,
              up_final, down_final, now.isoformat(), "polymarket"))
        resolved += 1
        await asyncio.sleep(0.2)

    conn.commit()
    conn.close()

    if resolved:
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"[{ts}] 🏁 Resolved {resolved} markets via Polymarket")
    return resolved

# ─── STATS DISPLAY ────────────────────────────────────────────────────────────
def print_stats():
    conn = db_connect()
    try:
        n_markets = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        n_snaps   = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        n_resolved= conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
        by_coin = conn.execute("""
            SELECT coin, COUNT(*) FROM markets GROUP BY coin
        """).fetchall()
        up_wins = conn.execute("SELECT COUNT(*) FROM resolutions WHERE winner='UP'").fetchone()[0]
        dn_wins = conn.execute("SELECT COUNT(*) FROM resolutions WHERE winner='DOWN'").fetchone()[0]
    finally:
        conn.close()

    print(f"""
╔═══════════════════════════════════════════════════════════╗
║              MARKETGHOST STATS                           ║
╠═══════════════════════════════════════════════════════════╣
║  Markets tracked   : {n_markets:<36} ║
║  Snapshots taken   : {n_snaps:<36} ║
║  Markets resolved  : {n_resolved:<36} ║
║  UP vs DOWN        : {up_wins}W / {dn_wins}W                                 ║""")
    for coin, cnt in by_coin:
        print(f"║  {coin:<4} markets       : {cnt:<36} ║")
    print("╚═══════════════════════════════════════════════════════════╝")

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────
async def main():
    init_db()

    print(f"""
╔═══════════════════════════════════════════════════════════╗
║   MARKETGHOST v1.0 — 24/7 DATA COLLECTOR                 ║
║   DB: {os.path.basename(DB_PATH):<50}  ║
║   Snapshot every {SNAPSHOT_INTERVAL}s | Resolve every {RESOLUTION_INTERVAL}s           ║
║   Read-only — does NOT affect your trading bot           ║
╚═══════════════════════════════════════════════════════════╝

Collecting data on all BTC/ETH/SOL/XRP Up/Down markets...
Press Ctrl+C to stop. Data persists in marketghost.db
    """)

    last_resolve = 0
    last_stats = 0

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                await snapshot_once(session)

                now_ts = time.monotonic()
                if now_ts - last_resolve >= RESOLUTION_INTERVAL:
                    await resolve_pending(session)
                    last_resolve = now_ts

                if now_ts - last_stats >= 600:
                    print_stats()
                    last_stats = now_ts

                await asyncio.sleep(SNAPSHOT_INTERVAL)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[WARN] Loop error: {e}")
                await asyncio.sleep(SNAPSHOT_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOPPED] MarketGhost exited. Data saved.")
        print_stats()
