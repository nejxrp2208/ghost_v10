"""
Crypto Ghost Scanner 2 — RAW Data Collector (no filters)
=========================================================
Fires on the cheap side (<= $0.03) of every 5-min Up/Down market
for all 5 coins, across the ENTIRE market window (0–300s).

Purpose: measure raw cheap-side win rate before any filters apply.
Compare with Scanner 1 (which has all filters) to quantify filter value.

NO filters:
  - No trend threshold
  - No certainty (MIN_NO_PRICE) check
  - No direction bias
  - No hour filter
  - No oracle gate
  - No brain state
  Fires on whichever side has ask <= RAW_MAX_ENTRY (default $0.03)

Writes tier=6, strategy='raw' to crypto_ghost_PAPER.db.
Resolver closes trades automatically (same resolver as Scanner 1).

PM2: pm2 start scanner2
"""

import asyncio
import aiohttp
import json
import time
import sqlite3
import os
import sys
from datetime import datetime, timezone
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
    print("ERROR: py-clob-client-v2 not installed.")
    sys.exit(1)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
GAMMA_API      = "https://gamma-api.polymarket.com"
CLOB_API       = "https://clob.polymarket.com"
BINANCE_REST   = "https://api.binance.com/api/v3"

PAPER_TRADE        = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true", "1", "yes")
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "20"))
DAILY_LOSS_LIMIT   = float(os.getenv("DAILY_LOSS_LIMIT", "999999.0"))

# Only config that matters: cheap side price ceiling
RAW_MAX_ENTRY  = 0.03    # fire on any ask <= $0.03
RAW_SIZE_USDC  = float(os.getenv("T2_SIZE_USDC", "1.0"))   # use small size for raw collection
RAW_SCAN_INTERVAL = 2.0  # faster scan — catch markets entering the window
RAW_WINDOW_SECS   = 300  # full 5-min window

RAW_COINS = {"BTC", "ETH", "BNB", "SOL", "XRP"}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(SCRIPT_DIR,
                "crypto_ghost_PAPER.db" if PAPER_TRADE else "crypto_ghost.db")

_SLUG_COINS = {"BTC": "btc", "ETH": "eth", "BNB": "bnb", "SOL": "sol", "XRP": "xrp"}
BINANCE_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT",
                   "SOL": "SOLUSDT", "XRP": "XRPUSDT"}

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
            outcome TEXT DEFAULT 'UP', trend_dev_1h REAL, secs_left REAL,
            strategy TEXT DEFAULT 'raw'
        )
    """)
    for col in ["outcome TEXT DEFAULT 'UP'", "trend_dev_1h REAL",
                "secs_left REAL", "strategy TEXT DEFAULT 'raw'"]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass
    conn.commit()
    conn.close()


def log_trade(coin, market_id, token_id, question, entry, size,
              order_id, outcome, secs_left):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO trades
          (ts, tier, coin, market_id, token_id, question,
           entry_price, size_usdc, order_id, outcome, secs_left, strategy)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (datetime.now(timezone.utc).isoformat(), 6, coin,
          market_id, token_id, question[:120],
          entry, size, order_id, outcome, secs_left, "raw"))
    conn.commit()
    conn.close()


def get_open_count():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open' AND tier=6").fetchone()[0]
    conn.close()
    return n


def get_daily_loss():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts LIKE ? AND tier=6 AND status IN ('won','lost')",
        (f"{today}%",)
    ).fetchone()[0]
    conn.close()
    return -min(pnl, 0)

# ─── BINANCE price (for logging only, not filtering) ──────────────────────────
_binance_cache: dict = {}

async def fetch_price(session: aiohttp.ClientSession, coin: str) -> Optional[float]:
    cached = _binance_cache.get(coin)
    if cached and time.time() - cached[0] < 30:
        return cached[1]
    sym = BINANCE_SYMBOLS.get(coin)
    if not sym:
        return None
    try:
        url = f"{BINANCE_REST}/ticker/price?symbol={sym}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as r:
            if r.status != 200:
                return None
            d = await r.json()
            price = float(d.get("price", 0))
            _binance_cache[coin] = (time.time(), price)
            return price
    except Exception:
        return None

# ─── MARKET DISCOVERY ─────────────────────────────────────────────────────────
def _build_slugs() -> list:
    """Build slugs for all 5-min markets that could currently be active."""
    now_ts  = int(time.time())
    slugs   = []
    # Cover a rolling window: 1 period back, 5 forward (~30 min)
    base    = ((now_ts // 300)) * 300 - 300
    for i in range(8):
        end_ts = base + i * 300
        for coin, slug_coin in _SLUG_COINS.items():
            if coin in RAW_COINS:
                slugs.append((coin, f"{slug_coin}-updown-5m-{end_ts}"))
    return slugs


async def fetch_markets(session: aiohttp.ClientSession) -> list:
    HEADERS = {"User-Agent": "Mozilla/5.0"}
    now     = time.time()
    slugs   = _build_slugs()
    sem     = asyncio.Semaphore(15)

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
        # Accept full 5-min window (0 to 300 seconds remaining)
        if secs_left < 2 or secs_left > RAW_WINDOW_SECS:
            continue

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

    return markets

# ─── ORDERBOOK ────────────────────────────────────────────────────────────────
async def fetch_best_ask(session: aiohttp.ClientSession,
                         token_id: str) -> Optional[float]:
    try:
        url = f"{CLOB_API}/book?token_id={token_id}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as r:
            if r.status != 200:
                return None
            book = await r.json()
            asks = sorted(book.get("asks") or [],
                          key=lambda x: float(x.get("price", 9999)))
            if not asks:
                return None
            price = float(asks[0].get("price", 0))
            return price if price > 0 else None
    except Exception:
        return None

# ─── SCANNER ENGINE ───────────────────────────────────────────────────────────
class RawScanner:
    def __init__(self):
        self.open_positions: set = set()   # token_ids currently open
        self.total_fired    = 0
        self.scan_count     = 0

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
        try:
            conn = sqlite3.connect(DB_PATH)
            still_open = {row[0] for row in conn.execute(
                "SELECT token_id FROM trades WHERE status='open' AND tier=6"
            ).fetchall()}
            conn.close()
            self.open_positions = still_open
        except Exception:
            pass

    async def fire(self, session, coin, market, token_id, ask, outcome, secs_left) -> bool:
        if token_id in self.open_positions:
            return False
        if get_open_count() >= MAX_OPEN_POSITIONS:
            return False
        if get_daily_loss() >= DAILY_LOSS_LIMIT:
            return False

        question = market.get("question", "")[:70]
        size     = RAW_SIZE_USDC
        shares   = max(round(size / ask, 4), 5.0)
        payout   = round(size / ask - size, 2)

        try:
            if PAPER_TRADE:
                order_id = f"RAW-{int(time.time()*1000)}"
            else:
                order_args = OrderArgs(token_id=token_id, price=ask, size=shares, side=BUY)
                resp       = self.client.create_and_post_order(order_args, order_type="FAK")
                order_id   = (resp.get("orderID") or resp.get("orderId") or
                              resp.get("id") or "") if resp else ""

            if order_id:
                self.open_positions.add(token_id)
                log_trade(coin, market.get("id", ""), token_id, question,
                          ask, size, order_id, outcome, secs_left)
                self.total_fired += 1
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] RAW FIRE | {coin} {outcome} @ ${ask:.3f} | "
                      f"{secs_left:.0f}s left | +${payout:.2f} | '{question[:45]}'")
                return True
        except Exception as e:
            print(f"  [RAW] fire error: {e}")
        return False

    async def scan(self, session: aiohttp.ClientSession):
        self.scan_count += 1
        self._sync_open_positions()
        markets = await fetch_markets(session)

        fired_this_scan = 0
        for m in markets:
            coin      = m["_coin"]
            secs_left = m["_secs_left"]
            tokens    = m.get("tokens") or []

            for tok in tokens:
                if not isinstance(tok, dict):
                    continue
                token_id = tok.get("token_id") or tok.get("id")
                outcome  = (tok.get("outcome", "") or "").upper()
                if not token_id or not outcome:
                    continue
                if token_id in self.open_positions:
                    continue

                ask = await fetch_best_ask(session, token_id)
                if ask is None:
                    continue
                if ask > RAW_MAX_ENTRY:
                    continue  # only cheap side

                ok = await self.fire(session, coin, m, token_id, ask, outcome, secs_left)
                if ok:
                    fired_this_scan += 1

        if self.scan_count % 30 == 0:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] RAW scan #{self.scan_count} | "
                  f"markets={len(markets)} | total_fired={self.total_fired} | "
                  f"open={len(self.open_positions)}")

    async def run(self):
        init_db()
        mode = "PAPER" if PAPER_TRADE else "LIVE"
        print(f"╔══════════════════════════════════════════════════════════╗")
        print(f"║   Ghost Scanner 2 — RAW Data Collector [{mode}]         ║")
        print(f"║   Entry: <= ${RAW_MAX_ENTRY:.3f} | Window: 0-{RAW_WINDOW_SECS}s | No filters  ║")
        print(f"║   Coins: {', '.join(sorted(RAW_COINS)):<44}  ║")
        print(f"║   tier=6  strategy='raw'  size=${RAW_SIZE_USDC:.2f}/trade           ║")
        print(f"╚══════════════════════════════════════════════════════════╝")

        connector = aiohttp.TCPConnector(limit=30, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            while True:
                try:
                    await self.scan(session)
                except Exception as e:
                    print(f"\n[RAW] Scan error: {e}")
                await asyncio.sleep(RAW_SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(RawScanner().run())
    except KeyboardInterrupt:
        print("\n[RAW] Stopped.")
