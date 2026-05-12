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
_COIN_MAP = {
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ethereum"],
    "BNB": ["bnb", "binance coin"],
    "SOL": ["sol", "solana"],
    "XRP": ["xrp", "ripple"],
}

def parse_coin(question: str) -> Optional[str]:
    q = question.lower()
    for coin, kws in _COIN_MAP.items():
        if any(k in q for k in kws):
            return coin
    return None


def is_updown(question: str) -> bool:
    q = question.lower()
    return "up or down" in q or "up-or-down" in q


_SLUG_COINS = {
    "BTC": "btc", "ETH": "eth", "BNB": "bnb",
    "SOL": "sol", "XRP": "xrp",
}

def _build_slugs(coins: set) -> list:
    """Build slug list for upcoming 5-min markets. Same approach as scanner1."""
    now_ts = int(time.time())
    slugs  = []
    curr_5m = ((now_ts // 300) + 1) * 300
    prev_5m = curr_5m - 300
    for i in range(6):   # covers prev + next 4 windows (~25 min)
        end_ts = prev_5m + i * 300
        for coin in coins:
            slug_coin = _SLUG_COINS.get(coin)
            if slug_coin:
                slugs.append((coin, f"{slug_coin}-updown-5m-{end_ts}"))
    return slugs


async def fetch_markets(session: aiohttp.ClientSession) -> list:
    """Fetch active 5-min Up/Down markets via slug lookup (same method as scanner1)."""
    HEADERS = {"User-Agent": "Mozilla/5.0"}
    now     = time.time()
    slugs   = _build_slugs(S2_COINS)

    sem = asyncio.Semaphore(15)

    async def _try_slug(coin, slug):
        async with sem:
            try:
                async with session.get(f"{GAMMA_API}/markets",
                                       params={"slug": slug}, headers=HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=4)) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
                    items = data if isinstance(data, list) else []
                    return (coin, slug, items[0]) if items else None
            except Exception:
                return None

    results = await asyncio.gather(*[_try_slug(c, s) for c, s in slugs])

    markets = []
    for res in results:
        if not res:
            continue
        coin, slug, m = res
        end = m.get("endDate") or m.get("endDateIso", "")
        if not end:
            continue
        try:
            end_dt    = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            secs_left = end_dt.timestamp() - now
        except Exception:
            continue
        if secs_left < 0 or secs_left > S2_WINDOW_SECS:
            continue

        # Parse tokens
        tokens = m.get("tokens", []) or []
        cti    = m.get("clobTokenIds") or m.get("clob_token_ids") or []
        if isinstance(cti, str):
            try: cti = json.loads(cti)
            except Exception: cti = []
        outs = m.get("outcomes") or m.get("outcome_names") or []
        if isinstance(outs, str):
            try: outs = json.loads(outs)
            except Exception: outs = []
        if not tokens and cti:
            for i, tid in enumerate(cti):
                out = outs[i] if i < len(outs) else ("UP" if i == 0 else "DOWN")
                tokens.append({"token_id": str(tid), "outcome": str(out).upper()})
        if not tokens:
            continue

        markets.append({
            "question":   m.get("question", ""),
            "id":         m.get("id", "") or slug,
            "tokens":     tokens,
            "endDateIso": end,
            "_coin":      coin,
            "_secs_left": secs_left,
        })

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] S2 scan: {len(markets)} in window | slugs tried={len(slugs)}")
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
        if not markets:
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

        if fired:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] S2 total fired={self.trades_fired} | open={len(self.open_positions)}")

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
