"""
ghost_brain.py — The Ghost Brain v2
=====================================
Real-time market intelligence engine built specifically for T2 5m/15m markets.
Updates every 10 seconds. Writes ghost_brain.json next to this script
for the scanner to read (path auto-resolves; works on Windows + Linux).

SCORING (0-100):
  [25] Candle direction + acceleration  — last 3 x 1m candles, getting bigger or smaller?
  [20] Price velocity                   — how fast is price moving right now vs normal?
  [15] Volume surge                     — last 2 candles elevated vs 20-candle average?
  [15] Order book imbalance             — bid vs ask pressure right now
  [10] Liquidation pressure             — large positions being wiped = forced momentum
  [8]  Spread tightness                 — tight spread = real move, wide = fake-out
  [7]  Recent high/low breach           — broken through 30min resistance/support?

  Funding rate = multiplier (not score):
    Trading WITH squeeze direction → score * 1.15
    Trading AGAINST squeeze        → score * 0.85

Ghost states:
  ◈ HAUNTING  ≥70  prime conditions — fire freely
  ◇ STALKING  ≥45  good conditions — fire selectively
  · DRIFTING  ≥25  weak — very selective
  ○ DORMANT   <25  dead market — scanner skips entirely

Updates every 10 seconds.
Liquidation WebSocket runs in background — optional but powerful.
"""

import asyncio, aiohttp, json, os, sys, time, math
from datetime import datetime, timezone
from collections import deque
from typing import Dict, List, Tuple, Optional

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    from rich.console import Console
    from rich.live import Live
    from rich.text import Text
    from rich.panel import Panel
    from rich.table import Table
    from rich.layout import Layout
    from rich import box as rbox
except ImportError:
    os.system(f"{sys.executable} -m pip install rich --break-system-packages -q")
    from rich.console import Console
    from rich.live import Live
    from rich.text import Text
    from rich.panel import Panel
    from rich.table import Table
    from rich.layout import Layout
    from rich import box as rbox

try:
    from dotenv import load_dotenv
    sd = os.path.dirname(os.path.abspath(__file__))
    # Look for .env in this dir, parent (GhostScanner/), or hardcoded Linux fallback.
    for ep in [os.path.join(sd, ".env"),
               os.path.join(sd, "..", ".env"),
               os.path.join(sd, "..", "tierbot", ".env"),
               "/root/tierbot/.env"]:
        if os.path.exists(ep):
            load_dotenv(ep, override=True)
            break
except ImportError:
    pass

# ─── CONFIG ───────────────────────────────────────────────────────────────────
# Use Binance.com (matches friend's working setup). Override via env if you
# need binance.us (set BINANCE_REST=https://api.binance.us/api/v3).
BINANCE_REST    = os.getenv("BINANCE_REST",    "https://api.binance.com/api/v3")
BINANCE_FUTURES = os.getenv("BINANCE_FUTURES", "https://fapi.binance.com/fapi/v1")

# Path resolution — works on Windows + Linux. Brain JSON lives next to this file.
_HERE = os.path.dirname(os.path.abspath(__file__))
BRAIN_FILE      = os.path.join(_HERE, "ghost_brain.json")
# Optional Chainlink price cache (Linux deploy only). Falls back gracefully.
CHAINLINK_DB    = os.getenv("CHAINLINK_DB",
                            os.path.join(_HERE, "..", "shared", "prices.db"))
UPDATE_INTERVAL = 10     # seconds between full updates
DISPLAY_REFRESH = 2      # seconds between screen redraws
LIQ_WINDOW      = 60     # seconds of liquidation history to track
LEARN_EVERY_N    = 18    # learn every N brain scans (~3 min at 10s interval)
LEARN_MIN_TRADES = 10    # minimum resolved trades per coin before adapting threshold
LEARN_MIN_BUCKET = 5     # minimum trades in a bucket to trust its WR
LEARN_LIFT       = 1.10  # bucket must beat coin baseline WR by this multiplier (10% lift)
LEARN_ABS_FLOOR  = 0.08  # absolute WR floor: never trust a bucket below 8% WR

COINS   = ["BTC", "ETH", "SOL", "XRP"]

# DB path — same folder as ghost_brain.py. Respects PAPER_TRADE env var.
_PAPER_TRADE = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true","1","yes")
_DB_FILE = os.path.join(_HERE, "GHOST_LATTICE_PAPER.db" if _PAPER_TRADE else "GHOST_LATTICE.db")
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}

# ATR baseline per coin — typical quiet 1m ATR as % of price
ATR_BASELINES = {"BTC": 0.05, "ETH": 0.06, "SOL": 0.10, "XRP": 0.08}

HAUNTING_THRESHOLD = 70
STALKING_THRESHOLD = 45
DRIFTING_THRESHOLD = 25

console = Console()

# ─── LIQUIDATION TRACKER ──────────────────────────────────────────────────────
# Tracks large liquidations in the last 60 seconds per coin
# Populated by the Binance futures liquidation WebSocket
liq_tracker: Dict[str, deque] = {
    coin: deque(maxlen=500) for coin in COINS
}
LIQ_SYMBOLS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH",
               "SOLUSDT": "SOL", "XRPUSDT": "XRP"}

async def liquidation_feed():
    """
    Connect to Binance futures liquidation WebSocket.
    Tracks forced liquidations — large ones = real forced momentum.
    Reconnects automatically on disconnect.
    """
    url = "wss://fstream.binance.com/ws/!forceOrder@arr"
    while True:
        try:
            import websockets
            async with websockets.connect(url, ping_interval=20,
                                          ping_timeout=10) as ws:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(raw)
                    order = data.get("o", {})
                    sym   = order.get("s", "")
                    coin  = LIQ_SYMBOLS.get(sym)
                    if not coin:
                        continue
                    side   = order.get("S", "")   # BUY or SELL
                    qty    = float(order.get("q", 0))
                    price  = float(order.get("p", 0))
                    usd    = qty * price
                    if usd >= 50000:   # Only track $50k+ liquidations
                        liq_tracker[coin].append({
                            "ts":   time.time(),
                            "side": side,
                            "usd":  usd,
                        })
        except Exception:
            await asyncio.sleep(5)

def get_liq_pressure(coin: str) -> Tuple[float, float, str]:
    """
    Returns (long_liq_usd, short_liq_usd, dominant_direction) in last 60s.
    Long liquidations = SELL pressure (longs getting wiped = price dropping)
    Short liquidations = BUY pressure (shorts getting wiped = price rising)
    """
    now     = time.time()
    cutoff  = now - LIQ_WINDOW
    long_liq  = 0.0   # longs getting liquidated = price fell
    short_liq = 0.0   # shorts getting liquidated = price rose

    for liq in liq_tracker[coin]:
        if liq["ts"] < cutoff:
            continue
        if liq["side"] == "SELL":    # long position liquidated
            long_liq += liq["usd"]
        else:                         # short position liquidated
            short_liq += liq["usd"]

    total = long_liq + short_liq
    if total < 50000:
        return long_liq, short_liq, "NEUTRAL"
    if long_liq > short_liq * 2:
        return long_liq, short_liq, "DOWN"   # longs getting rekt = price down
    if short_liq > long_liq * 2:
        return long_liq, short_liq, "UP"     # shorts getting rekt = price up
    return long_liq, short_liq, "NEUTRAL"

# ─── SHARED STATE ─────────────────────────────────────────────────────────────
brain_state = {
    "last_update":  "—",
    "last_full":    0,
    "scan_count":   0,
    "coins": {
        coin: {
            "score":          0,
            "raw_score":      0,
            "state":          "DORMANT",
            "direction":      "NEUTRAL",
            "price":          0.0,
            "history":        [],
            # Confidence window
            "score_history":  [],
            "dir_history":    [],
            "score_5m_min":   0,
            "score_5m_avg":   0,
            "dir_consistent": False,
            "last_real_ts":   0,
            "fetch_failures": 0,
            # Score breakdown
            "s_candle":      0,
            "s_velocity":    0,
            "s_volume":      0,
            "s_book":        0,
            "s_liq":         0,
            "s_spread":      0,
            "s_breakout":    0,
            # Raw metrics
            "candle_dir":    "NEUTRAL",
            "candle_accel":  False,
            "velocity_2m":   0.0,
            "vol_surge":     0.0,
            "book_pressure": 0.5,
            "spread_pct":    0.0,
            "breakout":      False,
            "liq_usd":            0.0,
            "liq_dir":            "NEUTRAL",
            "funding":            0.0,
            "funding_mult":       1.0,
            "price_change":       0.0,
            "chop_score":         0,
            "price_position_15m": 0.5,
            "liq_wall_dir":       "NEUTRAL",
            "liq_wall_strength":  0.0,
            "reversal_score":     0,
            "reversal_dir":       "NEUTRAL",
        }
        for coin in COINS
    }
}

# ─── CANDLE FETCHER ───────────────────────────────────────────────────────────

async def fetch_candles(session, coin: str, limit: int = 35) -> List[dict]:
    sym = SYMBOLS.get(coin)
    if not sym:
        return []
    try:
        async with session.get(
            f"{BINANCE_REST}/klines",
            params={"symbol": sym, "interval": "1m", "limit": limit},
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            if r.status != 200:
                return []
            return [{"ts":     int(c[0]),
                     "open":   float(c[1]),
                     "high":   float(c[2]),
                     "low":    float(c[3]),
                     "close":  float(c[4]),
                     "volume": float(c[5])} for c in await r.json()]
    except Exception:
        return []

async def fetch_orderbook(session, coin: str, levels: int = 10) -> dict:
    sym = SYMBOLS.get(coin)
    if not sym:
        return {}
    try:
        async with session.get(
            f"{BINANCE_REST}/depth",
            params={"symbol": sym, "limit": levels},
            timeout=aiohttp.ClientTimeout(total=4)
        ) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        pass
    return {}

async def fetch_funding(session, coin: str) -> float:
    sym = SYMBOLS.get(coin)
    if not sym:
        return 0.0
    try:
        async with session.get(
            f"{BINANCE_FUTURES}/premiumIndex",
            params={"symbol": sym},
            timeout=aiohttp.ClientTimeout(total=4)
        ) as r:
            if r.status == 200:
                return float((await r.json()).get("lastFundingRate", 0))
    except Exception:
        pass
    return 0.0

def get_chainlink_price(coin: str) -> float:
    """Read price from Chainlink DB — zero latency, no API call."""
    try:
        import sqlite3 as _sq
        conn = _sq.connect(CHAINLINK_DB)
        row  = conn.execute(
            "SELECT price FROM prices WHERE coin=?", (coin,)
        ).fetchone()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception:
        return 0.0

# ─── SCORER ───────────────────────────────────────────────────────────────────

def score_coin(coin: str, candles: List[dict], book: dict,
               funding: float, price: float) -> dict:
    """
    Score a coin 0-100 for T2 trade readiness.
    All signals point to: will price continue in this direction for 3 more minutes?
    """
    if len(candles) < 5 or price == 0:
        return {"score": 0, "direction": "NEUTRAL"}

    base_pct = ATR_BASELINES.get(coin, 0.07)
    total    = 0
    result   = {}

    # ── 1. CANDLE DIRECTION + ACCELERATION (0-25) ─────────────────────────────
    # Direction: are last 3 candles all same color?
    # Acceleration: is each candle body getting BIGGER? (momentum building)
    last3  = candles[-3:]
    bodies = [abs(c["close"] - c["open"]) for c in last3]
    dirs   = [1 if c["close"] > c["open"] else -1 if c["close"] < c["open"] else 0
              for c in last3]

    all_same   = all(d == dirs[-1] for d in dirs) and dirs[-1] != 0
    last2_same = dirs[-1] == dirs[-2] and dirs[-1] != 0
    accelerating = (len(bodies) >= 3 and
                    bodies[-1] > bodies[-2] > bodies[-3] * 0.8)

    if all_same and accelerating:
        candle_score = 25
    elif all_same:
        candle_score = 18
    elif last2_same and accelerating:
        candle_score = 14
    elif last2_same:
        candle_score = 8
    else:
        candle_score = 0

    candle_dir   = "UP" if dirs[-1] > 0 else "DOWN" if dirs[-1] < 0 else "NEUTRAL"
    total       += candle_score
    result["s_candle"]     = candle_score
    result["candle_dir"]   = candle_dir
    result["candle_accel"] = accelerating

    # ── 2. PRICE VELOCITY (0-20) ──────────────────────────────────────────────
    # How fast is price moving right now vs this coin's normal speed?
    velocity_2m = 0.0
    if len(candles) >= 3:
        velocity_2m = (candles[-1]["close"] - candles[-3]["close"]) / candles[-3]["close"] * 100

    vel_ratio  = abs(velocity_2m) / base_pct if base_pct > 0 else 0
    vel_score  = min(20, int(vel_ratio * 10))
    total     += vel_score
    result["s_velocity"]  = vel_score
    result["velocity_2m"] = round(velocity_2m, 4)

    # ── 3. VOLUME SURGE — last 2 candles (0-15) ───────────────────────────────
    # Two consecutive elevated volume candles = sustained conviction
    vols     = [c["volume"] for c in candles]
    avg_vol  = sum(vols[-22:-2]) / 20 if len(vols) >= 22 else sum(vols[:-2]) / max(len(vols)-2, 1)
    last2vol = (vols[-1] + vols[-2]) / 2 if len(vols) >= 2 else vols[-1]
    vol_surge = last2vol / avg_vol if avg_vol > 0 else 1.0

    if vol_surge >= 3.0:    vol_score = 15
    elif vol_surge >= 2.0:  vol_score = 11
    elif vol_surge >= 1.5:  vol_score = 7
    elif vol_surge >= 1.2:  vol_score = 3
    else:                   vol_score = 0

    total    += vol_score
    result["s_volume"]  = vol_score
    result["vol_surge"] = round(vol_surge, 2)

    # ── 4. ORDER BOOK IMBALANCE (0-15) ────────────────────────────────────────
    # Are buyers or sellers stacked up and ready?
    book_score    = 0
    book_pressure = 0.5
    if book:
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        bid_vol = sum(float(b[1]) * float(b[0]) for b in bids[:10])
        ask_vol = sum(float(a[1]) * float(a[0]) for a in asks[:10])
        total_book    = bid_vol + ask_vol
        book_pressure = bid_vol / total_book if total_book > 0 else 0.5

        if book_pressure >= 0.70 or book_pressure <= 0.30:
            book_score = 15
        elif book_pressure >= 0.62 or book_pressure <= 0.38:
            book_score = 10
        elif book_pressure >= 0.55 or book_pressure <= 0.45:
            book_score = 5
        else:
            book_score = 0

    book_dir = ("UP"   if book_pressure > 0.55 else
                "DOWN" if book_pressure < 0.45 else "NEUTRAL")
    total   += book_score
    result["s_book"]        = book_score
    result["book_pressure"] = round(book_pressure, 3)
    result["book_dir"]      = book_dir

    # ── 5. LIQUIDATION PRESSURE (0-10) ────────────────────────────────────────
    # Large forced liquidations = real momentum that won't reverse in 3 minutes
    long_liq, short_liq, liq_dir = get_liq_pressure(coin)
    liq_total = long_liq + short_liq

    if liq_total >= 5_000_000:    liq_score = 10   # $5M+ = massive
    elif liq_total >= 1_000_000:  liq_score = 7    # $1M+
    elif liq_total >= 250_000:    liq_score = 4    # $250k+
    elif liq_total >= 50_000:     liq_score = 2    # $50k+
    else:                         liq_score = 0

    total   += liq_score
    result["s_liq"]    = liq_score
    result["liq_usd"]  = round(liq_total, 0)
    result["liq_dir"]  = liq_dir

    # ── 6. SPREAD TIGHTNESS (0-8) ─────────────────────────────────────────────
    # Tight spread = market makers confident = real move
    # Wide spread = uncertainty = fake-out likely
    spread_score = 0
    spread_pct   = 0.0
    if book:
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            best_bid  = float(bids[0][0])
            best_ask  = float(asks[0][0])
            mid       = (best_bid + best_ask) / 2
            spread_pct = ((best_ask - best_bid) / mid * 100) if mid > 0 else 0

            # Tight if spread < 0.5x baseline, wide if > 2x baseline
            spread_ratio = spread_pct / (base_pct * 0.3) if base_pct > 0 else 1
            if spread_ratio <= 0.5:    spread_score = 8
            elif spread_ratio <= 1.0:  spread_score = 5
            elif spread_ratio <= 1.5:  spread_score = 2
            else:                      spread_score = 0

    total   += spread_score
    result["s_spread"]   = spread_score
    result["spread_pct"] = round(spread_pct, 5)

    # ── 7. RECENT HIGH/LOW BREACH (0-7) ───────────────────────────────────────
    # Has price broken through 30-minute resistance or support in last 2 candles?
    # Breakouts = strong momentum, nothing overhead to stop the move
    breakout      = False
    breakout_score = 0
    if len(candles) >= 32:
        recent30_high = max(c["high"]  for c in candles[-32:-2])
        recent30_low  = min(c["low"]   for c in candles[-32:-2])
        last2_high    = max(c["high"]  for c in candles[-2:])
        last2_low     = min(c["low"]   for c in candles[-2:])

        if last2_high > recent30_high:
            breakout       = True
            breakout_score = 7
        elif last2_low < recent30_low:
            breakout       = True
            breakout_score = 7

    total   += breakout_score
    result["s_breakout"] = breakout_score
    result["breakout"]   = breakout

    # ── FUNDING RATE MULTIPLIER ───────────────────────────────────────────────
    # Doesn't add to score — multiplies it
    # Extreme funding = overcrowded trade = squeeze incoming
    # Trade WITH the squeeze direction gets a boost
    funding_mult = 1.0
    if abs(funding) > 0.0001:
        # Positive funding = longs overcrowded = squeeze = DOWN pressure
        # Negative funding = shorts overcrowded = squeeze = UP pressure
        squeeze_dir = "DOWN" if funding > 0 else "UP"
        if candle_dir == squeeze_dir:
            funding_mult = 1.15   # trading with the squeeze
        elif candle_dir != "NEUTRAL":
            funding_mult = 0.90   # trading against the squeeze

    raw_score   = min(100, total)
    final_score = min(100, int(raw_score * funding_mult))

    result["funding"]      = funding
    result["funding_mult"] = round(funding_mult, 2)
    result["raw_score"]    = raw_score

    # ── DIRECTION ─────────────────────────────────────────────────────────────
    vel_dir = "UP" if velocity_2m > 0.02 else "DOWN" if velocity_2m < -0.02 else "NEUTRAL"
    votes = []
    if vel_dir    != "NEUTRAL":  votes.append(vel_dir);  votes.append(vel_dir)
    if candle_dir != "NEUTRAL":  votes.append(candle_dir)
    if book_dir   != "NEUTRAL":  votes.append(book_dir)
    if liq_dir    != "NEUTRAL":  votes.append(liq_dir)

    if votes.count("UP") > votes.count("DOWN"):
        direction = "UP"
    elif votes.count("DOWN") > votes.count("UP"):
        direction = "DOWN"
    else:
        direction = vel_dir if vel_dir != "NEUTRAL" else "NEUTRAL"

    # ── 1h price change ───────────────────────────────────────────────────────
    price_change = 0.0
    if len(candles) >= 60:
        price_change = (candles[-1]["close"] - candles[-60]["close"]) / candles[-60]["close"] * 100

    # ── CHOP DETECTION ────────────────────────────────────────────────────────
    # Count direction flips in last 15 candle bodies
    # High chop = price reversing frequently = reversal longshot opportunity
    chop_score = 0
    if len(candles) >= 15:
        last15_dirs = [1 if c["close"] > c["open"] else -1 if c["close"] < c["open"] else 0
                       for c in candles[-15:]]
        flips = sum(1 for i in range(1, len(last15_dirs))
                    if last15_dirs[i] != last15_dirs[i-1] and last15_dirs[i] != 0)
        chop_score = flips  # 0-14, higher = choppier

    # ── PRICE POSITION IN 15-MIN RANGE ───────────────────────────────────────
    # Where is current price relative to the 15-minute high/low?
    # 1.0 = at the top, 0.0 = at the bottom
    price_position_15m = 0.5
    if len(candles) >= 15 and price > 0:
        high_15m = max(c["high"] for c in candles[-15:])
        low_15m  = min(c["low"]  for c in candles[-15:])
        rng = high_15m - low_15m
        if rng > 0:
            price_position_15m = (price - low_15m) / rng

    # ── LIQUIDITY WALL DIRECTION ──────────────────────────────────────────────
    # Find which side has a concentrated wall of orders
    # A wall = natural support/resistance = reversal point
    liq_wall_dir = "NEUTRAL"
    liq_wall_strength = 0.0
    if book:
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if len(bids) >= 5 and len(asks) >= 5:
            bid_vols = [float(b[1]) * float(b[0]) for b in bids[:10]]
            ask_vols = [float(a[1]) * float(a[0]) for a in asks[:10]]
            total_bid = sum(bid_vols)
            total_ask = sum(ask_vols)
            max_bid_level = max(bid_vols) if bid_vols else 0
            max_ask_level = max(ask_vols) if ask_vols else 0
            bid_concentration = max_bid_level / total_bid if total_bid > 0 else 0
            ask_concentration = max_ask_level / total_ask if total_ask > 0 else 0

            if bid_concentration > 0.35 and bid_concentration > ask_concentration:
                liq_wall_dir = "UP"      # big bid wall below = support = bounce UP
                liq_wall_strength = bid_concentration
            elif ask_concentration > 0.35 and ask_concentration > bid_concentration:
                liq_wall_dir = "DOWN"    # big ask wall above = resistance = bounce DOWN
                liq_wall_strength = ask_concentration

    # ── REVERSAL SETUP SCORE ──────────────────────────────────────────────────
    # Combines chop + extreme position + liquidity wall
    # High reversal score = good longshot opportunity
    reversal_score = 0
    reversal_dir   = "NEUTRAL"

    if chop_score >= 6:  # choppy enough for reversals
        reversal_score += min(40, chop_score * 4)  # 0-40 pts from chop

        if price_position_15m >= 0.80:      # at top of range
            reversal_dir    = "DOWN"
            reversal_score += 30            # price extended upward
        elif price_position_15m <= 0.20:    # at bottom of range
            reversal_dir    = "UP"
            reversal_score += 30            # price extended downward

        if liq_wall_dir == reversal_dir:    # wall confirms reversal
            reversal_score += 30
        elif liq_wall_dir != "NEUTRAL":
            reversal_score -= 10            # wall contradicts reversal

    reversal_score = max(0, min(100, reversal_score))

    result["score"]              = final_score
    result["direction"]          = direction
    result["price_change"]       = round(price_change, 3)
    result["chop_score"]         = chop_score
    result["price_position_15m"] = round(price_position_15m, 3)
    result["liq_wall_dir"]       = liq_wall_dir
    result["liq_wall_strength"]  = round(liq_wall_strength, 3)
    result["reversal_score"]     = reversal_score
    result["reversal_dir"]       = reversal_dir

    return result

def score_to_state(score: int) -> str:
    if score >= HAUNTING_THRESHOLD:  return "HAUNTING"
    if score >= STALKING_THRESHOLD:  return "STALKING"
    if score >= DRIFTING_THRESHOLD:  return "DRIFTING"
    return "DORMANT"

# ─── LEARNED THRESHOLDS ───────────────────────────────────────────────────────
# Populated by _learn_from_outcomes(). Persisted in ghost_brain.json so the
# scanner can read them even after ghost_brain.py restarts.
_learned_thresholds: dict = {}   # coin -> optimal ghost_score floor


def _learn_from_outcomes(db_path: str):
    """
    Read resolved trades from the DB and compute per-coin optimal ghost_score
    threshold using RELATIVE win-rate lift — not a fixed absolute WR target.

    Background: overall bot WR is ~13-15% (by design — cheap entries, 6-7x payout).
    A fixed 20% threshold would never fire. Instead we compare each score bucket
    against that coin's own baseline WR and look for meaningful improvement.

    Algorithm:
      1. Pull all resolved (won/lost) trades since 2026-05-01 with ghost_score logged.
      2. For each coin, compute baseline_wr = total wins / total trades.
      3. Split into score buckets: [0-24], [25-44], [45-69], [70+].
      4. Find the LOWEST bucket where ALL of:
           (a) n >= LEARN_MIN_BUCKET  (enough data to trust)
           (b) bucket_wr >= baseline_wr * LEARN_LIFT  (beats coin's own average by 10%)
           (c) bucket_wr >= LEARN_ABS_FLOOR  (absolute floor: ≥8% WR — sanity check)
         That bucket's lo becomes the learned threshold for that coin.
      5. Also compute avg ghost_score for won trades vs lost trades. If winners
         consistently score higher, store that signal in the summary for display.
      6. If no bucket meets criteria or fewer than LEARN_MIN_TRADES total resolved
         trades exist for the coin, leave threshold at global default (25).

    Called every LEARN_EVERY_N scans (~3 min) from brain_loop() via thread executor.
    Thread-safe: only writes to module-level dict _learned_thresholds.
    """
    global _learned_thresholds
    try:
        import sqlite3 as _sq
        conn = _sq.connect(db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        # Only trades since 2026-05-01 with ghost_score logged
        rows = conn.execute("""
            SELECT coin, ghost_score, status
            FROM trades
            WHERE ts >= '2026-05-01'
              AND ghost_score IS NOT NULL
              AND status IN ('won', 'lost')
        """).fetchall()
        conn.close()
    except Exception:
        return  # DB locked or missing — skip this learn cycle

    if not rows:
        return

    # Bucket boundaries (lo inclusive, hi inclusive)
    BUCKETS = [(0, 24), (25, 44), (45, 69), (70, 100)]

    # Accumulate per-coin stats
    coin_total: dict = {}   # coin -> {"w": 0, "n": 0}
    coin_buckets: dict = {} # coin -> bucket_lo -> {"w": 0, "n": 0}
    # Track scores for won vs lost to compute means
    coin_scores: dict = {}  # coin -> {"won": [], "lost": []}

    for coin, gscore, status in rows:
        if coin not in COINS or gscore is None:
            continue
        gs = int(gscore)
        coin_total.setdefault(coin, {"w": 0, "n": 0})
        coin_total[coin]["n"] += 1
        if status == "won":
            coin_total[coin]["w"] += 1

        bucket_lo = 0
        for lo, hi in BUCKETS:
            if lo <= gs <= hi:
                bucket_lo = lo
                break
        coin_buckets.setdefault(coin, {}).setdefault(bucket_lo, {"w": 0, "n": 0})
        coin_buckets[coin][bucket_lo]["n"] += 1
        if status == "won":
            coin_buckets[coin][bucket_lo]["w"] += 1

        coin_scores.setdefault(coin, {"won": [], "lost": []})
        coin_scores[coin]["won" if status == "won" else "lost"].append(gs)

    new_thresholds = {}
    for coin in COINS:
        if coin not in coin_total:
            continue
        totals = coin_total[coin]
        if totals["n"] < LEARN_MIN_TRADES:
            continue   # not enough data yet
        baseline_wr = totals["w"] / totals["n"]
        # Minimum WR to beat: 10% lift above baseline, but never below 8% absolute
        target_wr = max(baseline_wr * LEARN_LIFT, LEARN_ABS_FLOOR)

        chosen = 25   # fallback: global DORMANT cutoff
        buckets_hit = []
        for lo, hi in BUCKETS:
            bkt = coin_buckets.get(coin, {}).get(lo, {"w": 0, "n": 0})
            if bkt["n"] < LEARN_MIN_BUCKET:
                continue
            wr = bkt["w"] / bkt["n"]
            if wr >= target_wr:
                chosen = max(lo, 10)   # floor at 10 — don't go full DORMANT-ignore
                buckets_hit.append((lo, round(wr, 3)))
                break   # take the lowest qualifying bucket

        new_thresholds[coin] = chosen

    if new_thresholds:
        _learned_thresholds = new_thresholds


# ─── BRAIN LOOP ───────────────────────────────────────────────────────────────

async def brain_loop():
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        funding_cache = {c: 0.0 for c in COINS}
        funding_last  = 0.0

        while True:
            now = time.time()
            brain_state["scan_count"] += 1

            # ── Learning loop (every LEARN_EVERY_N scans) ─────────────────────
            if brain_state["scan_count"] % LEARN_EVERY_N == 0:
                try:
                    import concurrent.futures as _cf
                    loop = asyncio.get_event_loop()
                    # Run blocking DB read in thread pool so async loop isn't stalled
                    await loop.run_in_executor(None, _learn_from_outcomes, _DB_FILE)
                except Exception:
                    pass

            # Refresh funding every 5 minutes (changes slowly)
            if now - funding_last >= 300:
                for coin in COINS:
                    try:
                        funding_cache[coin] = await fetch_funding(session, coin)
                    except Exception:
                        pass
                funding_last = now

            # Full score update every 10 seconds
            # Stagger requests per coin — price from Chainlink, candles/book from global Binance
            for coin in COINS:
                try:
                    # Price: Chainlink DB first (Linux-only), fall back to last candle close
                    price = get_chainlink_price(coin)

                    # Candles and orderbook from Binance REST (always available)
                    candles = await fetch_candles(session, coin, limit=35)
                    await asyncio.sleep(0.5)
                    book    = await fetch_orderbook(session, coin, levels=10)
                    await asyncio.sleep(0.5)

                    # Chainlink not available (Windows) → use last candle close price
                    if price <= 0 and candles:
                        price = candles[-1]["close"]
                    # Still 0 → keep last known value
                    if price <= 0:
                        price = brain_state["coins"][coin]["price"]

                    if price > 0:
                        brain_state["coins"][coin]["price"] = price

                    if candles and price > 0:
                        scored = score_coin(coin, candles, book,
                                            funding_cache[coin], price)
                        cs = brain_state["coins"][coin]
                        cs["score"]        = scored["score"]
                        cs["raw_score"]    = scored.get("raw_score", scored["score"])
                        cs["state"]        = score_to_state(scored["score"])
                        cs["direction"]    = scored["direction"]
                        cs["s_candle"]     = scored.get("s_candle",   0)
                        cs["s_velocity"]   = scored.get("s_velocity", 0)
                        cs["s_volume"]     = scored.get("s_volume",   0)
                        cs["s_book"]       = scored.get("s_book",     0)
                        cs["s_liq"]        = scored.get("s_liq",      0)
                        cs["s_spread"]     = scored.get("s_spread",   0)
                        cs["s_breakout"]   = scored.get("s_breakout", 0)
                        cs["candle_dir"]   = scored.get("candle_dir",    "NEUTRAL")
                        cs["candle_accel"] = scored.get("candle_accel",  False)
                        cs["velocity_2m"]  = scored.get("velocity_2m",   0.0)
                        cs["vol_surge"]    = scored.get("vol_surge",      1.0)
                        cs["book_pressure"]= scored.get("book_pressure",  0.5)
                        cs["book_dir"]     = scored.get("book_dir",       "NEUTRAL")
                        cs["spread_pct"]   = scored.get("spread_pct",     0.0)
                        cs["breakout"]     = scored.get("breakout",       False)
                        cs["liq_usd"]            = scored.get("liq_usd",            0.0)
                        cs["liq_dir"]            = scored.get("liq_dir",            "NEUTRAL")
                        cs["funding"]            = scored.get("funding",             0.0)
                        cs["funding_mult"]       = scored.get("funding_mult",        1.0)
                        cs["price_change"]       = scored.get("price_change",        0.0)
                        cs["chop_score"]         = scored.get("chop_score",          0)
                        cs["price_position_15m"] = scored.get("price_position_15m",  0.5)
                        cs["liq_wall_dir"]       = scored.get("liq_wall_dir",        "NEUTRAL")
                        cs["liq_wall_strength"]  = scored.get("liq_wall_strength",   0.0)
                        cs["reversal_score"]     = scored.get("reversal_score",      0)
                        cs["reversal_dir"]       = scored.get("reversal_dir",        "NEUTRAL")

                        cs["history"].append(scored["score"])
                        cs["history"] = cs["history"][-60:]

                        # ── Confidence window ─────────────────────────────
                        cs["score_history"].append(scored["score"])
                        cs["score_history"] = cs["score_history"][-30:]  # 5 min
                        cs["dir_history"].append(scored["direction"])
                        cs["dir_history"] = cs["dir_history"][-10:]

                        # 5-minute min and average (only real scores)
                        sh = cs["score_history"]
                        cs["score_5m_min"] = min(sh) if sh else 0
                        cs["score_5m_avg"] = int(sum(sh) / len(sh)) if sh else 0

                        # Direction consistent = same for last 3+ readings
                        dh = cs["dir_history"]
                        if len(dh) >= 3 and len(set(dh[-3:])) == 1 and dh[-1] != "NEUTRAL":
                            cs["dir_consistent"] = True
                        else:
                            cs["dir_consistent"] = False

                        # Mark successful fetch timestamp
                        cs["last_real_ts"]   = time.time()
                        cs["fetch_failures"] = 0

                except Exception:
                    # Track consecutive failures
                    brain_state["coins"][coin]["fetch_failures"] += 1
                    pass

            brain_state["last_update"] = datetime.now().strftime("%H:%M:%S")

            # Write JSON for scanner
            try:
                export = {
                    "last_update":        brain_state["last_update"],
                    "scan_count":         brain_state["scan_count"],
                    "learned_thresholds": dict(_learned_thresholds),
                    "coins": {
                        coin: {
                            "score":          brain_state["coins"][coin]["score"],
                            "score_5m_min":   brain_state["coins"][coin]["score_5m_min"],
                            "score_5m_avg":   brain_state["coins"][coin]["score_5m_avg"],
                            "state":          brain_state["coins"][coin]["state"],
                            "direction":      brain_state["coins"][coin]["direction"],
                            "dir_consistent": brain_state["coins"][coin]["dir_consistent"],
                            "last_real_ts":   brain_state["coins"][coin]["last_real_ts"],
                            "fetch_failures": brain_state["coins"][coin]["fetch_failures"],
                            "price":          brain_state["coins"][coin]["price"],
                            "vol_surge":      brain_state["coins"][coin]["vol_surge"],
                            "funding":        brain_state["coins"][coin]["funding"],
                            "velocity_2m":    brain_state["coins"][coin]["velocity_2m"],
                            "book_pressure":  brain_state["coins"][coin]["book_pressure"],
                            "liq_usd":            brain_state["coins"][coin]["liq_usd"],
                            "liq_dir":            brain_state["coins"][coin]["liq_dir"],
                            "breakout":           brain_state["coins"][coin]["breakout"],
                            "chop_score":         brain_state["coins"][coin]["chop_score"],
                            "price_position_15m": brain_state["coins"][coin]["price_position_15m"],
                            "liq_wall_dir":       brain_state["coins"][coin]["liq_wall_dir"],
                            "liq_wall_strength":  brain_state["coins"][coin]["liq_wall_strength"],
                            "reversal_score":     brain_state["coins"][coin]["reversal_score"],
                            "reversal_dir":       brain_state["coins"][coin]["reversal_dir"],
                        }
                        for coin in COINS
                    }
                }
                with open(BRAIN_FILE, "w") as f:
                    json.dump(export, f)
            except Exception:
                pass

            await asyncio.sleep(UPDATE_INTERVAL)

# ─── DISPLAY ──────────────────────────────────────────────────────────────────

STATE_COLORS = {
    "HAUNTING": "bright_cyan",
    "STALKING": "bright_yellow",
    "DRIFTING": "yellow",
    "DORMANT":  "bright_black",
}
STATE_ICONS = {
    "HAUNTING": "◈",
    "STALKING": "◇",
    "DRIFTING": "·",
    "DORMANT":  "○",
}
DIR_COLORS  = {"UP": "bright_green", "DOWN": "bright_red", "NEUTRAL": "bright_black"}
DIR_SYMBOLS = {"UP": "▲",            "DOWN": "▼",          "NEUTRAL": "─"}



def _build_color_cell(label: str, c1: str, d1: str, c2: str, d2: str, c3: str, d3: str) -> Text:
    """Build a color swatch cell with colored squares."""
    t = Text()
    t.append(f"{label} ", style="bright_black")
    t.append("█ ", style=c1)
    t.append(f"{d1}  ", style="bright_black")
    if d2:
        t.append("█ ", style=c2)
        t.append(d2, style="bright_black")
    if d3:
        t.append("  █ ", style=c3)
        t.append(d3, style="bright_black")
    return t

def render_screen() -> Layout:
    now   = datetime.now().strftime("%H:%M:%S")
    scans = brain_state["scan_count"]
    last  = brain_state["last_update"]
    coins_data = brain_state["coins"]

    states    = [coins_data[c]["state"] for c in COINS]
    haunting  = states.count("HAUNTING")
    stalking  = states.count("STALKING")
    drifting  = states.count("DRIFTING")
    dormant   = states.count("DORMANT")
    avg_score = sum(coins_data[c]["score"] for c in COINS) // 4

    if haunting >= 2:
        regime, rc = "◈  PRIME HUNTING GROUNDS  ◈", "bold bright_cyan"
    elif haunting >= 1 or stalking >= 2:
        regime, rc = "◇  ACTIVE — SELECTIVE FIRE  ◇", "bold bright_yellow"
    elif stalking >= 1 or drifting >= 2:
        regime, rc = "·  LOW ACTIVITY — WAIT  ·", "bold yellow"
    else:
        regime, rc = "○  ALL QUIET — STAND BY  ○", "bold bright_black"

    bar_color = ("bright_cyan"   if haunting >= 2 else
                 "bright_yellow" if (haunting >= 1 or stalking >= 2) else
                 "yellow"        if (stalking >= 1 or drifting >= 2) else
                 "bright_black")

    bar_w  = 50
    filled = int(avg_score / 100 * bar_w)
    bar    = "█" * filled + "░" * (bar_w - filled)

    layout = Layout()
    layout.split_column(
        Layout(name="header",  size=3),
        Layout(name="regime",  size=6),
        Layout(name="bubbles", size=32),
        Layout(name="scores",  size=6),
        Layout(name="legend",  size=12),
    )

    # Header
    ht = Text(justify="center")
    ht.append("\n  ░▒▓  ", style="bright_black")
    ht.append("G H O S T   B R A I N  v2", style="bold bright_cyan")
    ht.append("  ▓▒░  ", style="bright_black")
    ht.append(f"[ {now} ]  scan #{scans}  updated {last}\n", style="bright_black")
    layout["header"].update(Panel(ht, border_style="bright_black", box=rbox.MINIMAL))

    # Regime
    rt = Text(justify="center")
    rt.append(f"\n{regime}\n", style=rc)
    rt.append(f"{bar}  {avg_score}/100\n", style=bar_color)
    st = Text(justify="center")
    st.append(f"◈ {haunting} HAUNTING  ", style="bright_cyan")
    st.append(f"◇ {stalking} STALKING  ", style="bright_yellow")
    st.append(f"· {drifting} DRIFTING  ", style="yellow")
    st.append(f"○ {dormant} DORMANT",     style="bright_black")
    rt.append_text(st)
    layout["regime"].update(Panel(rt, border_style="bright_black",
                                   title="[bright_black]MARKET REGIME[/]",
                                   box=rbox.SQUARE))

    # Coin bubbles
    bubble_layout = Layout()
    bubble_layout.split_row(*[Layout(name=coin) for coin in COINS])

    for coin in COINS:
        cs    = coins_data[coin]
        score = cs["score"]
        state = cs["state"]
        sc    = STATE_COLORS.get(state, "white")
        dc    = DIR_COLORS.get(cs["direction"], "white")
        ds    = DIR_SYMBOLS.get(cs["direction"], "─")
        ico   = STATE_ICONS.get(state, "○")

        price = cs["price"]
        if price >= 1000:   price_str = f"${price:,.0f}"
        elif price >= 1:    price_str = f"${price:,.3f}"
        else:               price_str = f"${price:.4f}"

        pc     = cs.get("price_change", 0)
        pc_col = "bright_green" if pc >= 0 else "bright_red"
        pc_str = f"{'+' if pc >= 0 else ''}{pc:.2f}%"

        fill_w    = 16
        filled_b  = int(score / 100 * fill_w)
        fill_char = "█" if state == "HAUNTING" else "▓" if state == "STALKING" else "░" if state == "DRIFTING" else "·"
        fill_bar  = fill_char * filled_b + "·" * (fill_w - filled_b)

        history = cs.get("history", [])
        spark_chars = " ▁▂▃▄▅▆▇█"
        if history:
            recent = history[-16:] if len(history) >= 16 else history
            padded = [0] * (16 - len(recent)) + recent
            spark  = "".join(spark_chars[min(8, int(v / 100 * 8))] for v in padded)
        else:
            spark = "·" * 16

        vol      = cs.get("vol_surge",    0)
        book     = cs.get("book_pressure", 0.5)
        vel      = cs.get("velocity_2m",   0)
        liq      = cs.get("liq_usd",       0)
        liq_dir  = cs.get("liq_dir",       "NEUTRAL")
        bkout    = cs.get("breakout",      False)
        accel    = cs.get("candle_accel",  False)
        fmult    = cs.get("funding_mult",  1.0)
        funding  = cs.get("funding",       0.0)
        spread   = cs.get("spread_pct",    0.0)
        cdir     = cs.get("candle_dir",    "NEUTRAL")
        book_dir = cs.get("book_dir",      "NEUTRAL")

        # Score breakdown values
        sc_cndle = cs.get("s_candle",   0)
        sc_vel   = cs.get("s_velocity", 0)
        sc_vol   = cs.get("s_volume",   0)
        sc_book  = cs.get("s_book",     0)
        sc_liq   = cs.get("s_liq",      0)
        sc_sprd  = cs.get("s_spread",   0)
        sc_bkout = cs.get("s_breakout", 0)

        # Color rules
        vol_c    = "bright_cyan"  if vol >= 2.0 else "cyan" if vol >= 1.3 else "bright_black"
        book_c   = "bright_green" if book > 0.60 else "bright_red" if book < 0.40 else "bright_black"
        vel_c    = "bright_green" if vel > 0.01 else "bright_red" if vel < -0.01 else "bright_black"
        liq_c    = ("bright_green" if liq_dir == "UP" else
                    "bright_red"   if liq_dir == "DOWN" else
                    "bright_black" if liq == 0 else "cyan")
        fnd_c    = "bright_green" if funding < -0.0001 else "bright_red" if funding > 0.0001 else "bright_black"
        sprd_c   = "bright_cyan"  if spread < 0.002 else "yellow" if spread < 0.005 else "bright_red"
        bkout_c  = "bold bright_cyan"   if bkout else "bright_black"
        accel_c  = "bold bright_yellow" if accel else "bright_black"
        mult_c   = "bright_green" if fmult > 1 else "bright_red" if fmult < 1 else "bright_black"

        def mini_bar(val, max_val, width=6):
            filled = int(val / max_val * width) if max_val > 0 else 0
            return "█" * filled + "·" * (width - filled)

        content = Text(justify="center")

        # Header
        content.append(f"\n{ico} ", style=sc)
        content.append(f"{coin}\n", style=f"bold {sc}")
        content.append(f"{state}\n\n", style=sc)

        # Fill bar + score
        content.append(f"{fill_bar}\n", style=sc)
        content.append(f"{score:3d}", style=f"bold {sc}")
        content.append(" / 100\n", style="bright_black")

        # Direction
        content.append(f"{ds} {cs['direction']}\n\n", style=dc)

        # Price
        content.append(f"{price_str}\n", style="bold white")
        content.append(f"{pc_str}\n", style=pc_col)

        # Divider
        content.append("─" * 18 + "\n", style="bright_black")

        # All metrics — always visible
        content.append("CNDLE ", style="bright_black")
        content.append(f"{cdir:<7}", style=DIR_COLORS.get(cdir, "bright_black"))
        content.append(f"{mini_bar(sc_cndle,25)} {sc_cndle:2d}/25\n", style=sc)

        content.append("VEL   ", style="bright_black")
        content.append(f"{vel:+.3f}%  ", style=vel_c)
        content.append(f"{mini_bar(sc_vel,20)} {sc_vel:2d}/20\n", style=sc)

        content.append("VOL   ", style="bright_black")
        content.append(f"{vol:.1f}x     ", style=vol_c)
        content.append(f"{mini_bar(sc_vol,15)} {sc_vol:2d}/15\n", style=sc)

        content.append("BOOK  ", style="bright_black")
        book_dir_s = "▲" if book_dir == "UP" else "▼" if book_dir == "DOWN" else "─"
        content.append(f"{book:.2f}{book_dir_s}   ", style=book_c)
        content.append(f"{mini_bar(sc_book,15)} {sc_book:2d}/15\n", style=sc)

        liq_str = f"${liq/1000:.0f}k" if liq >= 1000 else "$0  "
        content.append("LIQ   ", style="bright_black")
        content.append(f"{liq_str:<7}", style=liq_c)
        content.append(f"{mini_bar(sc_liq,10)} {sc_liq:2d}/10\n", style=sc)

        content.append("SPRD  ", style="bright_black")
        content.append(f"{spread:.4f}% ", style=sprd_c)
        content.append(f"{mini_bar(sc_sprd,8)}  {sc_sprd:2d}/8\n", style=sc)

        bkout_s = "YES" if bkout else "NO "
        content.append("BKOUT ", style="bright_black")
        content.append(f"{bkout_s}    ", style=bkout_c)
        content.append(f"{mini_bar(sc_bkout,7)}  {sc_bkout:2d}/7\n", style=sc)

        # Divider
        content.append("─" * 18 + "\n", style="bright_black")

        # Funding
        fund_str = f"{funding*100:+.5f}%"
        content.append("FND   ", style="bright_black")
        content.append(f"{fund_str}\n", style=fnd_c)

        # Funding multiplier
        content.append("MULT  ", style="bright_black")
        content.append(f"x{fmult:.2f}", style=mult_c)
        if accel:
            content.append("  ↑ACCEL", style=accel_c)
        content.append("\n", style="white")

        # Reversal signals
        chop      = cs.get("chop_score", 0)
        pos_15m   = cs.get("price_position_15m", 0.5)
        rev_score = cs.get("reversal_score", 0)
        rev_dir   = cs.get("reversal_dir", "NEUTRAL")
        wall_dir  = cs.get("liq_wall_dir", "NEUTRAL")

        if rev_score >= 40:
            rev_sym = "▼" if rev_dir == "DOWN" else "▲" if rev_dir == "UP" else "─"
            content.append(f"◉ REV {rev_sym}{rev_dir} {rev_score}/100\n", style="bold bright_magenta")

        content.append("CHOP  ", style="bright_black")
        chop_c = "bright_magenta" if chop >= 8 else "magenta" if chop >= 6 else "bright_black"
        content.append(f"{chop}/14\n", style=chop_c)

        content.append("POS   ", style="bright_black")
        pos_bar = "█" * int(pos_15m * 10) + "·" * (10 - int(pos_15m * 10))
        pos_c   = "bright_red" if pos_15m >= 0.80 else "bright_green" if pos_15m <= 0.20 else "bright_black"
        content.append(f"{pos_bar} {pos_15m:.2f}\n", style=pos_c)

        if wall_dir != "NEUTRAL":
            wall_sym_c = "bright_green" if wall_dir == "UP" else "bright_red"
            content.append(f"WALL  {wall_dir}\n", style=wall_sym_c)

        # Sparkline
        content.append(f"\n{spark}\n", style=sc)

        bubble_layout[coin].update(
            Panel(content, border_style=sc,
                  title=f"[{sc}]{coin}[/]",
                  box=rbox.SQUARE, padding=(0, 1))
        )

    layout["bubbles"].update(bubble_layout)

    # Score breakdown bar chart
    score_table = Table.grid(padding=(0, 1))
    score_table.add_column(width=6)
    score_table.add_column(width=14)
    score_table.add_column(width=14)
    score_table.add_column(width=14)
    score_table.add_column(width=14)

    score_table.add_row(
        Text("", style="white"),
        *[Text(coin, style=f"bold {STATE_COLORS.get(coins_data[coin]['state'],'white')}",
               justify="center") for coin in COINS]
    )

    breakdown_items = [
        ("CNDLE", "s_candle",   25),
        ("VEL",   "s_velocity", 20),
        ("VOL",   "s_volume",   15),
        ("BOOK",  "s_book",     15),
        ("LIQ",   "s_liq",      10),
        ("SPRD",  "s_spread",    8),
        ("BKOUT", "s_breakout",  7),
    ]

    for label, key, max_pts in breakdown_items:
        row = [Text(f"{label}", style="bright_black")]
        for coin in COINS:
            val = coins_data[coin].get(key, 0)
            pct = val / max_pts if max_pts > 0 else 0
            bar_len = 10
            filled_s = int(pct * bar_len)
            bar_s = "█" * filled_s + "·" * (bar_len - filled_s)
            col = "bright_cyan" if pct >= 0.8 else "yellow" if pct >= 0.5 else "bright_black"
            t = Text(f"{bar_s} {val:2d}/{max_pts}", style=col, justify="center")
            row.append(t)
        score_table.add_row(*row)

    layout["scores"].update(Panel(score_table,
                                   title="[bright_black]SCORE BREAKDOWN[/]",
                                   border_style="bright_black",
                                   box=rbox.SQUARE, padding=(0, 1)))

    # Key
    key_table = Table.grid(padding=(0, 3))
    key_table.add_column(width=34)
    key_table.add_column(width=34)
    key_table.add_column(width=44)

    key_table.add_row(
        Text("── STATES ──────────────────────────", style="bright_black"),
        Text("── SIGNAL COLORS ───────────────────", style="bright_black"),
        Text("── SCORE BARS (per signal) ──────────", style="bright_black"),
    )

    def color_row(label, c1, desc1, c2, desc2, score_label, score_txt):
        """Build a row with colored squares instead of text color names."""
        t = Text()
        t.append(f"{label}  ", style="bright_black")
        t.append("█ ", style=c1); t.append(f"{desc1}  ", style="bright_black")
        t.append("█ ", style=c2); t.append(desc2, style="bright_black")
        return t

    key_table.add_row(
        Text("◈ HAUNTING  ≥70  fire freely", style="bright_cyan"),
        _build_color_cell("CNDLE", "bright_green", "▲ UP", "bright_red", "▼ DOWN", "bright_black", "─ NEUTRAL"),
        Text("CNDLE  candle dir + acceleration /25", style="bright_black"),
    )
    key_table.add_row(
        Text("◇ STALKING  ≥45  fire selectively", style="bright_yellow"),
        _build_color_cell("VEL  ", "bright_cyan", "rising", "bright_red", "falling", "bright_black", ""),
        Text("VEL    price velocity last 2min  /20", style="bright_black"),
    )
    key_table.add_row(
        Text("· DRIFTING  ≥25  very selective", style="yellow"),
        _build_color_cell("VOL  ", "bright_cyan", "2x+ surge", "bright_black", "normal", "bright_black", ""),
        Text("VOL    2-candle volume surge      /15", style="bright_black"),
    )
    key_table.add_row(
        Text("○ DORMANT   <25  scanner skips", style="bright_black"),
        _build_color_cell("BOOK ", "bright_green", "buyers", "bright_red", "sellers", "bright_black", ""),
        Text("BOOK   bid/ask book imbalance    /15", style="bright_black"),
    )
    key_table.add_row(
        Text("▲ UP   ▼ DOWN   ─ NEUTRAL", style="bright_black"),
        _build_color_cell("LIQ  ", "bright_cyan", "short squeeze↑", "bright_red", "long liq↓", "bright_black", ""),
        Text("LIQ    forced liquidation $USD   /10", style="bright_black"),
    )
    key_table.add_row(
        Text("◈ BKOUT YES = 30min level broken", style="bright_cyan"),
        _build_color_cell("SPRD ", "bright_cyan", "tight=real", "bright_red", "wide=fake", "bright_black", ""),
        Text("SPRD   spread tight=real move     /8", style="bright_black"),
    )
    key_table.add_row(
        Text("↑ ACCEL = candles getting bigger", style="bright_yellow"),
        _build_color_cell("FND  ", "bright_red", "longs crowded", "cyan", "shorts crowded", "bright_black", ""),
        Text("BKOUT  30min high/low broken      /7", style="bright_black"),
    )
    key_table.add_row(
        Text("MULT x1.15=squeeze tailwind", style="bright_green"),
        _build_color_cell("MULT ", "bright_green", "x1.15 tailwind", "bright_red", "x0.90 headwind", "bright_black", ""),
        Text("FND    funding rate multiplier (not scored)", style="bright_black"),
    )
    key_table.add_row(
        Text("MULT x0.90=squeeze headwind", style="bright_red"),
        _build_color_cell("CHOP ", "bright_magenta", "8+ choppy", "magenta", "6-7 moderate", "bright_black", ""),
        Text("MULT   x1.15 tailwind  x0.90 headwind", style="bright_black"),
    )
    key_table.add_row(
        Text("── REVERSAL SIGNALS ─────────────────", style="bright_black"),
        Text("── REVERSAL COLORS ──────────────────", style="bright_black"),
        Text("── REVERSAL SCORING ─────────────────", style="bright_black"),
    )
    key_table.add_row(
        Text("◉ REV = reversal setup detected", style="bright_magenta"),
        Text("CHOP   magenta=8+  dim=low chop", style="bright_black"),
        Text("CHOP   6+ flips in 15 candles   /14", style="bright_black"),
    )
    key_table.add_row(
        Text("CHOP  = direction flips last 15min", style="bright_black"),
        Text("POS    red=top  green=bottom range", style="bright_black"),
        Text("POS    price in 15min range 0-1.0", style="bright_black"),
    )
    key_table.add_row(
        Text("POS   = price in 15min high/low range", style="bright_black"),
        Text("WALL   green=bid wall  red=ask wall", style="bright_black"),
        Text("WALL   liq wall = natural reversal pt", style="bright_black"),
    )
    key_table.add_row(
        Text("WALL  = liquidity wall = reversal zone", style="bright_black"),
        Text("◉ REV  magenta = active reversal setup", style="bright_magenta"),
        Text("REV 40+ = buy cheap reversal longshot", style="bright_black"),
    )

    layout["legend"].update(Panel(key_table,
                                   title="[bright_black]KEY[/]",
                                   border_style="bright_black",
                                   box=rbox.SQUARE, padding=(0, 1)))
    return layout


async def display_loop():
    with Live(render_screen(), console=console,
              refresh_per_second=1, screen=True) as live:
        while True:
            live.update(render_screen())
            await asyncio.sleep(DISPLAY_REFRESH)


async def main():
    console.print("\n[bright_cyan]  Ghost Brain v2 initializing...[/]")
    console.print("[bright_black]  Fetching initial data for all coins...[/]\n")

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        for coin in COINS:
            try:
                price = get_chainlink_price(coin)
                candles = await fetch_candles(session, coin, limit=35)
                book    = await fetch_orderbook(session, coin)
                # Chainlink not available on Windows → use last candle close
                if price <= 0 and candles:
                    price = candles[-1]["close"]
                if price > 0:
                    brain_state["coins"][coin]["price"] = price
                if candles and price > 0:
                    scored = score_coin(coin, candles, book, 0.0, price)
                    cs = brain_state["coins"][coin]
                    cs["score"]     = scored["score"]
                    cs["state"]     = score_to_state(scored["score"])
                    cs["direction"] = scored["direction"]
                    cs["history"].append(scored["score"])
                    for k in ["s_candle","s_velocity","s_volume","s_book",
                              "s_liq","s_spread","s_breakout","candle_dir",
                              "candle_accel","velocity_2m","vol_surge",
                              "book_pressure","spread_pct","breakout",
                              "liq_usd","liq_dir","funding","funding_mult","price_change"]:
                        if k in scored:
                            cs[k] = scored[k]
                console.print(f"  [bright_black]{coin}[/] "
                               f"[{STATE_COLORS.get(brain_state['coins'][coin]['state'],'white')}]"
                               f"{brain_state['coins'][coin]['state']}[/] "
                               f"score=[bright_cyan]{brain_state['coins'][coin]['score']}[/] "
                               f"dir={brain_state['coins'][coin]['direction']}")
            except Exception as e:
                console.print(f"  [red]{coin} error: {e}[/]")

    brain_state["scan_count"]  = 1
    brain_state["last_update"] = datetime.now().strftime("%H:%M:%S")
    brain_state["last_full"]   = time.time()

    console.print("\n[bright_cyan]  Ghost Brain v2 online.[/]\n")
    await asyncio.sleep(1)

    try:
        import websockets
        await asyncio.gather(
            brain_loop(),
            display_loop(),
            liquidation_feed(),
        )
    except ImportError:
        console.print("[yellow]  websockets not installed — running without liquidation feed[/]")
        console.print("ht_black]  Install with: pip install websockets --break-system-packages[/]\n")
        await asyncio.gather(
            brain_loop(),
            display_loop(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bright_black]  Ghost Brain offline.[/]\n")
