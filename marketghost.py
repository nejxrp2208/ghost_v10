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
BINANCE_API = "https://api.binance.us"

# Wallet-based discovery removed by default — Polymarket-only.
# Set WALLET_DISCOVERY=true in .env to re-enable third-party wallet
# market discovery. Default = false.
DISCOVERY_WALLET = ""
USE_WALLET_DISCOVERY = os.getenv("WALLET_DISCOVERY", "false").strip().lower() in ("true", "1", "yes")
HEADERS = {"User-Agent": "Mozilla/5.0"}

SNAPSHOT_INTERVAL = 60   # seconds between snapshots
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
            FOREIGN KEY (market_id) REFERENCES markets(market_id)
        );

        CREATE TABLE IF NOT EXISTS resolutions (
            market_id TEXT PRIMARY KEY,
            coin TEXT,
            start_utc TEXT,
            end_utc TEXT,
            price_start REAL,
            price_end REAL,
            winner TEXT,          -- 'UP' or 'DOWN'
            up_final_price REAL,
            down_final_price REAL,
            resolved_at TEXT,
            FOREIGN KEY (market_id) REFERENCES markets(market_id)
        );

        CREATE INDEX IF NOT EXISTS idx_snap_market ON snapshots(market_id);
        CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts_utc);
        CREATE INDEX IF NOT EXISTS idx_mkt_end ON markets(end_utc);
    """)
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
    """Build Polymarket slugs for current + upcoming 5-min Up/Down markets."""
    slugs = []
    now_et = datetime.now(timezone.utc) - timedelta(hours=4)  # EDT

    months = ["january","february","march","april","may","june",
              "july","august","september","october","november","december"]
    month = months[now_et.month - 1]
    day   = now_et.day

    for delta in range(-2, 8):
        t = now_et + timedelta(minutes=delta * 5)
        t_min  = (t.minute // 5) * 5
        t2_min = t_min + 5
        t_hour  = t.hour
        t_hour2 = t_hour + (1 if t2_min >= 60 else 0)
        if t2_min >= 60: t2_min -= 60

        def fmt(h, m):
            suf = "am" if h < 12 else "pm"
            h12 = h % 12 or 12
            return f"{h12:02d}{m:02d}{suf}" if m else f"{h12}{suf}"

        t1_str = fmt(t_hour, t_min)
        t2_str = fmt(t_hour2, t2_min)
        if t1_str.startswith("0"): t1_str = t1_str[1:]
        if t2_str.startswith("0"): t2_str = t2_str[1:]

        for coin_slug in ["bitcoin", "ethereum", "solana", "xrp"]:
            slug = f"{coin_slug}-up-or-down-{month}-{day}-{t1_str}-{t2_str}-et"
            label = "XRP" if coin_slug == "xrp" else coin_slug.upper()[:3]
            slugs.append((label, slug))

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
    """Discover markets via wallet activity + slug fallback."""
    markets = []
    seen_ids = set()

    # Method 1: Wallet watching (most reliable)
    wallet_markets = await fetch_via_wallet(session)
    for cid, info in wallet_markets.items():
        m = await lookup_market_by_condition_id(session, cid)
        if not m:
            continue
        mid = str(m.get("id", ""))
        if not mid or mid in seen_ids:
            continue
        seen_ids.add(mid)

        q = m.get("question", "")
        detected = detect_coin(q)
        if not detected:
            continue
        start_utc, end_utc = parse_market_times(q)
        if not end_utc:
            continue

        markets.append({
            "market_id": mid,
            "slug": m.get("slug", ""),
            "question": q,
            "coin": detected,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "duration_min": int((end_utc - start_utc).total_seconds() / 60),
            "clob_token_ids": m.get("clobTokenIds", "[]"),
            "outcomes": m.get("outcomes", "[]"),
            "volume": float(m.get("volume", 0) or 0),
            "liquidity": float(m.get("liquidity", 0) or 0),
        })
        await asyncio.sleep(0.05)

    # Method 2: Slug fallback (for markets the wallet hasn't traded)
    if len(markets) < 4:  # Only if wallet method didn't find much
        slugs = build_expected_slugs()
        for coin, slug in slugs:
            m = await lookup_market_by_slug(session, slug)
            if not m:
                continue
            mid = str(m.get("id", ""))
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)

            q = m.get("question", "")
            detected = detect_coin(q) or coin
            start_utc, end_utc = parse_market_times(q)
            if not end_utc:
                continue

            markets.append({
                "market_id": mid,
                "slug": m.get("slug", slug),
                "question": q,
                "coin": detected,
                "start_utc": start_utc,
                "end_utc": end_utc,
                "duration_min": int((end_utc - start_utc).total_seconds() / 60),
                "clob_token_ids": m.get("clobTokenIds", "[]"),
                "outcomes": m.get("outcomes", "[]"),
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("liquidity", 0) or 0),
            })
            await asyncio.sleep(0.05)

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
            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(asks[0]["price"]) if asks else 0.0
            # Liquidity = size at best ask
            liq = float(asks[0]["size"]) if asks else 0.0
            return best_bid, best_ask, liq
    except Exception:
        return None, None, 0

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
            "SELECT market_id FROM markets WHERE market_id = ?", (m["market_id"],)
        ).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO markets
                (market_id, slug, question, coin, duration_minutes,
                 start_utc, end_utc, first_seen)
                VALUES (?,?,?,?,?,?,?,?)
            """, (m["market_id"], m["slug"], m["question"], m["coin"],
                  m["duration_min"], m["start_utc"].isoformat(),
                  m["end_utc"].isoformat(), now.isoformat()))
            registered += 1

        # Only snapshot if market is still open
        if now > m["end_utc"]:
            continue

        # Fetch orderbooks
        up_bid, up_ask, up_liq = await fetch_orderbook(session, up_token)
        down_bid, down_ask, down_liq = await fetch_orderbook(session, down_token)

        if up_ask is None and down_ask is None:
            continue

        mins_to_close = (m["end_utc"] - now).total_seconds() / 60.0
        spread = (up_ask or 0) - (up_bid or 0) if up_ask and up_bid else 0

        conn.execute("""
            INSERT INTO snapshots
            (market_id, ts_utc, mins_to_close,
             up_bid, up_ask, down_bid, down_ask,
             up_token_id, down_token_id,
             volume, liquidity, spread)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (m["market_id"], now.isoformat(), mins_to_close,
              up_bid, up_ask, down_bid, down_ask,
              up_token, down_token,
              m["volume"], max(up_liq, down_liq), spread))
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
    """For markets that ended but aren't resolved yet, fetch Binance prices."""
    conn = db_connect()
    now = datetime.now(timezone.utc)

    pending = conn.execute("""
        SELECT m.market_id, m.coin, m.start_utc, m.end_utc, m.question
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
    for market_id, coin, start_iso, end_iso, question in pending:
        try:
            start_utc = datetime.fromisoformat(start_iso)
            end_utc   = datetime.fromisoformat(end_iso)
        except Exception:
            continue

        # Need 60s grace for Binance 1m candle
        if (now - end_utc).total_seconds() < 60:
            continue

        symbol = f"{coin}USDT"
        p_start = await get_binance_price_at(session, symbol, start_utc)
        p_end   = await get_binance_price_at(session, symbol, end_utc)
        if p_start is None or p_end is None:
            continue

        winner = "UP" if p_end >= p_start else "DOWN"

        # Get final snapshot for final prices
        final_snap = conn.execute("""
            SELECT up_ask, down_ask FROM snapshots
            WHERE market_id = ?
            ORDER BY ts_utc DESC LIMIT 1
        """, (market_id,)).fetchone()
        up_final  = final_snap[0] if final_snap else None
        down_final= final_snap[1] if final_snap else None

        conn.execute("""
            INSERT OR REPLACE INTO resolutions
            (market_id, coin, start_utc, end_utc,
             price_start, price_end, winner,
             up_final_price, down_final_price, resolved_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (market_id, coin, start_iso, end_iso,
              p_start, p_end, winner,
              up_final, down_final, now.isoformat()))
        resolved += 1
        await asyncio.sleep(0.2)

    conn.commit()
    conn.close()

    if resolved:
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"[{ts}] 🏁 Resolved {resolved} markets via Binance")
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

                now_ts = asyncio.get_event_loop().time()
                if now_ts - last_resolve >= RESOLUTION_INTERVAL:
                    await resolve_pending(session)
                    last_resolve = now_ts

                # Print stats every 10 minutes
                if now_ts - last_stats >= 600:
                    print_stats()
                    last_stats = now_ts

            except Exception as e:
                print(f"[WARN] Loop error: {e}")

            await asyncio.sleep(SNAPSHOT_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOPPED] MarketGhost exited. Data saved.")
        print_stats()
