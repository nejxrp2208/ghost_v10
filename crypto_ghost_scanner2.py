"""
Crypto Ghost Scanner 2 — Mean-Reversion Mid-Probability Strategy
================================================================
Companion to crypto_ghost_scanner.py (Scanner 1 — cheap longshot).

Strategy:
  When BTC/ETH/BNB deviates strongly in one direction over the last 1h,
  the 5-min market tends to mean-revert. Buy the "losing" side at mid-
  probability pricing (0.35–0.45) instead of the extreme longshot.

Signal:
  trend_1h < -S2_TREND_THRESHOLD  →  buy UP  (price fell → expect bounce)
  trend_1h > +S2_TREND_THRESHOLD  →  buy DOWN (price rose → expect pullback)

Entry zone:  ask 0.35–0.45  (vs Scanner 1's ≤$0.03)
Window:      last S2_WINDOW_SECS seconds of the 5-min market
Payout:      ~1.5:1  (vs Scanner 1's ~33:1)
Target WR:   >40%  (breakeven ~40%)

Shares crypto_ghost_PAPER.db, resolver, and redeemer with Scanner 1.
Trades written with tier=5 so they're distinguishable in the analyzer.

PM2:  pm2 start scanner2
"""

import asyncio
import aiohttp
import json
import time
import sqlite3
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.stdout.reconfigure(line_buffering=True)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
else:
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass

try:
    from dotenv import load_dotenv
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for ep in [os.path.join(script_dir, ".env"), os.path.join(script_dir, "..", ".env")]:
        if os.path.exists(ep):
            load_dotenv(ep, override=True)
            print(f"[ENV] Loaded: {ep}")
            break
except ImportError:
    pass

try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import OrderArgsV2 as OrderArgs, ApiCreds
    BUY = "BUY"
except ImportError:
    print("ERROR: py-clob-client-v2 not installed. Run: pip install py-clob-client-v2")
    sys.exit(1)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
GAMMA_API      = "https://gamma-api.polymarket.com"
CLOB_API       = "https://clob.polymarket.com"
BINANCE_REST   = "https://api.binance.com/api/v3"

PAPER_TRADE    = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true", "1", "yes")
PORTFOLIO_SIZE = float(os.getenv("PORTFOLIO_SIZE", "200.0"))

# ── Scanner 2 specific ──
S2_ENTRY_MIN       = float(os.getenv("S2_ENTRY_MIN",       "0.35"))   # min ask
S2_ENTRY_MAX       = float(os.getenv("S2_ENTRY_MAX",       "0.45"))   # max ask
S2_TREND_THRESHOLD = float(os.getenv("S2_TREND_THRESHOLD", "0.003"))  # 0.3% 1h dev triggers
S2_WINDOW_SECS     = int(os.getenv("S2_WINDOW_SECS",       "120"))    # last 2 minutes
S2_SIZE_USDC       = float(os.getenv("S2_SIZE_USDC",       "3.0"))
S2_MIN_LIQUIDITY   = float(os.getenv("S2_MIN_LIQUIDITY",   "10.0"))   # higher req for mid-prob
S2_COINS           = set(
    c.strip().upper() for c in
    os.getenv("S2_COINS", "BTC,ETH,BNB").split(",") if c.strip()
)
S2_SCAN_INTERVAL   = float(os.getenv("S2_SCAN_INTERVAL",   "5.0"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS",   "20"))
DAILY_LOSS_LIMIT   = float(os.getenv("DAILY_LOSS_LIMIT",   "999999.0"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(SCRIPT_DIR,
                "crypto_ghost_PAPER.db" if PAPER_TRADE else "crypto_ghost.db")

BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT",
    "SOL": "SOLUSDT", "XRP": "XRPUSDT",
}

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, tier INTEGER, coin TEXT,
            market_id TEXT, token_id TEXT, question TEXT,
            entry_price REAL, size_usdc REAL, order_id TEXT,
            status TEXT DEFAULT 'open', pnl REAL,
            closed_at TEXT, price_start REAL, price_end REAL,
            resolution_verified INTEGER DEFAULT 0, pm_result TEXT,
            outcome TEXT DEFAULT 'UP', trend_dev_1h REAL, secs_left REAL
        )
    """)
    conn.commit()
    conn.close()


def log_trade(coin, market_id, token_id, question, entry, size, order_id,
              outcome, trend_dev, secs_left):
    conn = sqlite3.connect(DB_PATH)
    for col in ["outcome TEXT DEFAULT 'UP'", "trend_dev_1h REAL", "secs_left REAL"]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass
    conn.execute("""
        INSERT INTO trades
          (ts, tier, coin, market_id, token_id, question,
           entry_price, size_usdc, order_id, outcome, trend_dev_1h, secs_left)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (datetime.now(timezone.utc).isoformat(), 5, coin,
          market_id, token_id, question[:120],
          entry, size, order_id, outcome, trend_dev, secs_left))
    conn.commit()
    conn.close()


def get_open_count():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
    conn.close()
    return n


def get_daily_loss():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts LIKE ? AND status IN ('won','lost')",
        (f"{today}%",)
    ).fetchone()[0]
    conn.close()
    return -min(pnl, 0)

# ─── BINANCE — 1H DEVIATION ───────────────────────────────────────────────────
_binance_cache: dict = {}   # coin → (ts, dev_1h, price)
_BINANCE_TTL = 30           # seconds

async def fetch_dev_1h(session: aiohttp.ClientSession, coin: str) -> Optional[float]:
    """Return (current - 1h_open) / 1h_open for coin. Cached 30s."""
    cached = _binance_cache.get(coin)
    if cached and time.time() - cached[0] < _BINANCE_TTL:
        return cached[1]

    sym = BINANCE_SYMBOLS.get(coin)
    if not sym:
        return None
    try:
        url = f"{BINANCE_REST}/klines?symbol={sym}&interval=1h&limit=2"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data:
                return None
            candle = data[-1]              # current (incomplete) candle
            open_1h  = float(candle[1])
            close    = float(candle[4])
            dev      = (close - open_1h) / open_1h
            _binance_cache[coin] = (time.time(), dev, close)
            return dev
    except Exception:
        return None

# ─── POLYMARKET — MARKET DISCOVERY ───────────────────────────────────────────
def parse_coin(question: str) -> Optional[str]:
    q = question.lower()
    mapping = {
        "BTC": ["btc", "bitcoin"],
        "ETH": ["eth", "ethereum"],
        "BNB": ["bnb", "binance coin"],
        "SOL": ["sol", "solana"],
        "XRP": ["xrp", "ripple"],
    }
    for coin, kws in mapping.items():
        if any(k in q for k in kws):
            return coin
    return None


def is_5min_updown(question: str) -> bool:
    import re
    q = question.lower()
    if "up or down" not in q and "up-or-down" not in q:
        return False
    # 5-min markets match time-range pattern like "11:55PM-12:00AM"
    return bool(re.search(r'\d{1,2}:\d{2}[ap]m', q))


async def fetch_markets(session: aiohttp.ClientSession) -> list:
    """Fetch active 5-min Up/Down markets from Gamma. Returns list of market dicts."""
    markets = []
    try:
        url = (f"{GAMMA_API}/markets?active=true&closed=false"
               f"&tag=crypto&limit=200&order=end_date_iso&ascending=true")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return markets
            data = await r.json()
            raw = data if isinstance(data, list) else data.get("markets", [])
    except Exception:
        return markets

    now = datetime.now(timezone.utc)
    for m in raw:
        q    = m.get("question", "")
        coin = parse_coin(q)
        if not coin or coin not in S2_COINS:
            continue
        if not is_5min_updown(q):
            continue

        end_iso = m.get("endDateIso") or m.get("end_date_iso") or ""
        if not end_iso:
            continue
        try:
            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            secs_left = (end_dt - now).total_seconds()
        except Exception:
            continue

        if secs_left < 0 or secs_left > S2_WINDOW_SECS:
            continue   # not in our window yet

        m["_coin"]      = coin
        m["_secs_left"] = secs_left
        markets.append(m)

    return markets

# ─── POLYMARKET — ORDERBOOK ───────────────────────────────────────────────────
async def fetch_orderbook(session: aiohttp.ClientSession,
                          token_id: str) -> Optional[dict]:
    try:
        url = f"{CLOB_API}/book?token_id={token_id}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception:
        return None


def best_ask_info(book: dict) -> tuple[Optional[float], float]:
    """Return (best_ask_price, liquidity_at_ask)."""
    asks = book.get("asks") or []
    if not asks:
        return None, 0.0
    asks_sorted = sorted(asks, key=lambda x: float(x.get("price", 9999)))
    best = asks_sorted[0]
    price = float(best.get("price", 0))
    size  = float(best.get("size", 0))
    return (price if price > 0 else None), price * size

# ─── SCANNER ENGINE ───────────────────────────────────────────────────────────
class Scanner2:
    def __init__(self):
        self.open_positions: dict = {}   # token_id → position info
        self.trades_fired   = 0

        if PAPER_TRADE:
            self.client = None
        else:
            creds = ApiCreds(
                api_key        = os.getenv("API_KEY", ""),
                api_secret     = os.getenv("API_SECRET", ""),
                api_passphrase = os.getenv("API_PASSPHRASE", ""),
            )
            self.client = ClobClient(
                host       = "https://clob.polymarket.com",
                chain_id   = 137,
                key        = PRIVATE_KEY,
                creds      = creds,
                funder     = FUNDER_ADDRESS,
                signature_type = 2,
            )

    def _sync_open_positions(self):
        """Remove positions the resolver already closed."""
        try:
            conn = sqlite3.connect(DB_PATH)
            still_open = {row[0] for row in conn.execute(
                "SELECT token_id FROM trades WHERE status='open' AND tier=5"
            ).fetchall()}
            conn.close()
            to_remove = [tid for tid in self.open_positions if tid not in still_open]
            for tid in to_remove:
                del self.open_positions[tid]
        except Exception:
            pass

    async def fire(self, session: aiohttp.ClientSession, coin: str,
                   market: dict, token_id: str, ask: float,
                   outcome: str, dev_1h: float, secs_left: float) -> bool:
        if token_id in self.open_positions:
            return False
        if get_open_count() >= MAX_OPEN_POSITIONS:
            return False
        if get_daily_loss() >= DAILY_LOSS_LIMIT:
            return False

        question = market.get("question", "")[:70]
        size     = S2_SIZE_USDC
        shares   = max(round(size / ask, 4), 5.0)
        payout   = round(size / ask - size, 2)

        try:
            if PAPER_TRADE:
                order_id = f"S2-PAPER-{int(time.time()*1000)}"
            else:
                order_args = OrderArgs(token_id=token_id, price=ask, size=shares, side=BUY)
                resp       = self.client.create_and_post_order(order_args, order_type="FAK")
                order_id   = (resp.get("orderID") or resp.get("orderId") or
                              resp.get("id") or "") if resp else ""

            if order_id:
                self.open_positions[token_id] = {
                    "coin": coin, "question": question,
                    "entry": ask, "size": size, "ts": time.time(),
                }
                log_trade(coin, market.get("id", ""), token_id, question,
                          ask, size, order_id, outcome, dev_1h, secs_left)
                self.trades_fired += 1
                ts       = datetime.now().strftime("%H:%M:%S")
                mode_tag = "[PAPER]" if PAPER_TRADE else "[LIVE] "
                print(f"[{ts}] {mode_tag} S2 MEAN-REV | "
                      f"{coin} {outcome} @ ${ask:.3f} | "
                      f"dev1h={dev_1h:+.3%} | {secs_left:.0f}s left | "
                      f"Size:${size} +${payout:.2f} | '{question[:40]}'")
                return True
        except Exception as e:
            print(f"  [S2] Trade error: {e}")
        return False

    async def scan(self, session: aiohttp.ClientSession):
        self._sync_open_positions()
        markets = await fetch_markets(session)
        ts = datetime.now().strftime("%H:%M:%S")
        if not markets:
            print(f"[{ts}] S2 scan: 0 in window | fired=0 | open={len(self.open_positions)} | total={self.trades_fired}")
            return

        fired = 0
        for m in markets:
            coin      = m["_coin"]
            secs_left = m["_secs_left"]
            tokens    = m.get("tokens") or []
            if not tokens:
                continue

            # ── Trend signal ──────────────────────────────────────────────────
            dev_1h = await fetch_dev_1h(session, coin)
            if dev_1h is None:
                continue
            if abs(dev_1h) < S2_TREND_THRESHOLD:
                continue   # not enough deviation to signal mean reversion

            want_up = (dev_1h < 0)   # price fell → buy UP for bounce
            target_outcome = "UP" if want_up else "DOWN"

            # ── Find matching token ───────────────────────────────────────────
            up_aliases   = ("UP", "YES", "HIGHER", "ABOVE")
            down_aliases = ("DOWN", "NO", "LOWER", "BELOW")
            target_token_id = None
            for tok in tokens:
                if not isinstance(tok, dict):
                    continue
                out = (tok.get("outcome", "") or "").upper()
                tid = tok.get("token_id") or tok.get("id")
                if not tid:
                    continue
                if want_up and out in up_aliases:
                    target_token_id = tid
                    break
                if not want_up and out in down_aliases:
                    target_token_id = tid
                    break

            if not target_token_id or target_token_id in self.open_positions:
                continue

            # ── Orderbook check ───────────────────────────────────────────────
            book = await fetch_orderbook(session, target_token_id)
            if not book:
                continue
            ask, liq = best_ask_info(book)
            if ask is None:
                continue
            if not (S2_ENTRY_MIN <= ask <= S2_ENTRY_MAX):
                continue   # not in mid-probability zone
            if liq < S2_MIN_LIQUIDITY:
                continue   # not enough liquidity

            # ── Fire ─────────────────────────────────────────────────────────
            ok = await self.fire(session, coin, m, target_token_id,
                                 ask, target_outcome, dev_1h, secs_left)
            if ok:
                fired += 1

        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] S2 scan: {len(markets)} in window | fired={fired} | "
              f"open={len(self.open_positions)} | total={self.trades_fired}")

    async def run(self):
        init_db()
        mode = "PAPER" if PAPER_TRADE else "LIVE"
        print(f"╔══════════════════════════════════════════════════════╗")
        print(f"║   Ghost Scanner 2 — Mean-Reversion [{mode}]         ║")
        print(f"║   Entry: ${S2_ENTRY_MIN:.2f}–${S2_ENTRY_MAX:.2f}  "
              f"Threshold: {S2_TREND_THRESHOLD:.1%}  Window: {S2_WINDOW_SECS}s  ║")
        print(f"╚══════════════════════════════════════════════════════╝")

        conn_kwargs = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=conn_kwargs) as session:
            while True:
                try:
                    await self.scan(session)
                except Exception as e:
                    print(f"\n[S2] Scan error: {e}")
                await asyncio.sleep(S2_SCAN_INTERVAL)


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(Scanner2().run())
    except KeyboardInterrupt:
        print("\n[S2] Stopped.")
