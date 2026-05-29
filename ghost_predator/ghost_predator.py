#!/usr/bin/env python3
"""
GHOST PREDATOR — independent last-window latency snipe (BTC/ETH up-down).

Thesis (proven on 3 days of data, see snipe_backtest.py):
  In the final seconds of a 5-min up/down market the underlying has usually
  already decided the outcome. The side that's leading at T-10s goes on to win
  ~95% of the time — yet Polymarket often still offers it well under its true
  probability. We compute the leader from OUR OWN Binance feed and snipe PM's
  stale quote before it reprices. Independent: no copying, no feed-race.

  Backtest edge:  T-5s  ~95% win  +60% ROI   |   T-10s  ~91% win  +36% ROI

Speed: built to run on a low-latency VPS. Paper mode simulates fills; live
mode fires FOK at the ask (capped, no slippage walk-up).

Run:  python3 ghost_predator.py        (+ python3 resolver.py in another tab)
"""
import os, sys, time, json, sqlite3, asyncio
from collections import deque
from datetime import datetime, timezone, timedelta

try:
    import aiohttp
except ImportError:
    sys.exit("needs aiohttp:  pip install aiohttp")

SD = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(SD, "ghost_predator.db")

# ── config ─────────────────────────────────────────────────────────────────
def load_env():
    p = os.path.join(SD, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if "#" in v: v = v[:v.index("#")]
                os.environ.setdefault(k.strip(), v.strip())
load_env()
def envf(k, d): return float(os.getenv(k, d))
def envi(k, d): return int(os.getenv(k, d))

PAPER           = os.getenv("PAPER_TRADE", "true").lower() == "true"
START_BAL       = envf("STARTING_BALANCE", "1000")
COINS           = [c.strip().upper() for c in os.getenv("COINS", "BTC,ETH").split(",") if c.strip()]
# Which market durations to trade. Backtest validated 5m ONLY — others differ.
DUR_MAP         = {"5m":300, "15m":900, "30m":1800, "1h":3600, "4h":14400, "1d":86400}
DURATIONS       = {d.strip() for d in os.getenv("DURATIONS", "5m").split(",") if d.strip()}
SNIPE_WINDOW    = envi("SNIPE_WINDOW_SECS", "12")   # snipe window for 5m (and default)
SNIPE_WINDOW_15M= envi("SNIPE_WINDOW_15M", str(SNIPE_WINDOW))  # wider window just for 15m markets
def snipe_window_for(dur):                          # per-duration snipe window (15m can run wider)
    return SNIPE_WINDOW_15M if dur == "15m" else SNIPE_WINDOW
MIN_FILL_SECS   = envi("MIN_FILL_SECS", "2")        # don't fire if less time than this remains
MIN_MOVE_BPS    = envf("MIN_MOVE_BPS", "3.0")       # leader must be ahead by >= this (0.03%) to be "decisive"
BTC_MIN_MOVE_BPS = envf("BTC_MIN_MOVE_BPS", str(MIN_MOVE_BPS))  # per-coin override; default = global MIN_MOVE_BPS
ETH_MIN_MOVE_BPS = envf("ETH_MIN_MOVE_BPS", str(MIN_MOVE_BPS))  # ETH rabi večji premik (data: 2.0 reže borderline losse)
ETH_SKIP_15M    = os.getenv("ETH_SKIP_15M", "false").lower() == "true"  # skip ETH 15m (data: 54% WR = coinflip, Chainlink disagree)
MIN_ASK         = envf("MIN_ASK", "0.02")
MAX_ASK         = envf("MAX_ASK", "0.90")           # only snipe if ask <= this (profit room)
BASE_SIZE       = envf("BASE_SIZE", "5.0")
MAX_PER_MARKET  = envf("MAX_PER_MARKET", "10.0")
REQUIRE_FILL    = os.getenv("REQUIRE_FILL", "true").lower() == "true"  # only fire/record when resting depth >= stake (skip unfillable; paper mirrors live FOK). false = record everything + tag would_fill
DYNAMIC_SIZE    = os.getenv("DYNAMIC_SIZE", "true").lower() == "true"  # 2026-05-23: size each order to depth at the ask = min(BASE_SIZE, depth) for ~100% fill. Captures thin high-win books at small size; supersedes the REQUIRE_FILL gate. BASE_SIZE becomes the CAP.
MIN_STAKE       = envf("MIN_STAKE", "1.0")                             # Polymarket min order; if depth-matched size is below this, the book is truly unfillable -> skip
FILL_FLOOR      = envf("FILL_FLOOR", "0.0")                            # safety floor on blended walk-fill entry price. 0 = disabled. Set e.g. 0.50 to prevent a stale WSS book from producing a sub-floor blended avg (observed: best_ask=0.65 passed gate but stale WSS levels dragged blended to 0.46)
WALK_FILL       = os.getenv("WALK_FILL", "true").lower() == "true"     # 2026-05-25: WALK THE BOOK to fill the FULL BASE_SIZE ($30) across the ask ladder within MAX_ASK, recording the BLENDED avg entry (realistic multi-level FOK). Fire ONLY if the in-band book can supply the full stake ("$30 or skip") — thin books that can't are skipped because a live FOK for $30 would miss them anyway, and their small wins don't survive live slippage/fees. Supersedes DYNAMIC_SIZE's shrink-to-best-ask. false = old depth-matched behavior.
BOOK_POLL_MS    = envi("BOOK_POLL_MS", "300")       # ask-refresh cadence for in-window markets
USE_WSS_BOOK    = os.getenv("USE_WSS_BOOK", "true").lower() == "true"  # real-time book via WSS (REST fallback). flip false = exact old REST behavior
DISCOVERY_SECS  = envi("DISCOVERY_SECS", "20")
PRICE_MAX_AGE   = envf("PRICE_MAX_AGE", "3.0")      # don't trade if Binance price is older than this (stale feed)
# ── account-survival guardrails (so a bad run can never drain the account) ──
DAILY_LOSS_LIMIT = envf("DAILY_LOSS_LIMIT", "150")  # halt for the day if today's PnL <= -this
MAX_LOSS_STREAK  = envi("MAX_LOSS_STREAK", "5")     # halt if this many losses in a row (edge broke)
MIN_BALANCE      = envf("MIN_BALANCE", "0")         # halt if balance < this (0 -> floor = one stake)
TG_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT         = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BINANCE_SYM = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
               "XRP": "XRPUSDT", "BNB": "BNBUSDT"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
CLOB_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"  # public market channel (book + deltas)
BINANCE_REST = "https://api.binance.com"

# ── shared state ─────────────────────────────────────────────────────────────
prices  = {}     # COIN -> latest Binance price
price_ts = {}    # COIN -> time.time() when that price arrived (stale-feed guard)
markets = {}     # token_id -> dict(coin, side, cond, slug, open_price, close_ts)
sniped  = set()  # token_ids already acted on
thin_skip = set()  # token_ids skipped this window for insufficient depth (dedupes the skip log)
HALT    = {"reason": None}  # set by snipe_loop guardrails; surfaced to dashboard
price_hist = {}  # COIN -> deque of recent prices (for the dashboard charts)
book_ms = {"last": None}  # last measured CLOB /book round-trip in ms (shown on dashboard)
books   = {}              # token_id -> {ask_price: size_shares} — real-time asks from the WSS feed
book_conn_ts = {"last": 0}  # time.time() of last WSS message (connection-liveness gate)
book_src = {"last": "—"}  # "wss" or "rest": which path served the last ask (shown on dashboard)
WSS_FRESH_SECS = 3.0      # if no WSS message in this long, treat the feed as dead -> REST fallback
def ts_str(): return datetime.now().strftime("%H:%M:%S")
def now(): return time.time()
def et_window(close_ts, dur_secs):
    """Market window in ET, e.g. '6:40-6:45 ET' (EDT = UTC-4)."""
    try:
        s = datetime.fromtimestamp(close_ts - dur_secs, timezone.utc) - timedelta(hours=4)
        e = datetime.fromtimestamp(close_ts, timezone.utc) - timedelta(hours=4)
        return f"{s.strftime('%-I:%M')}-{e.strftime('%-I:%M')} ET"
    except Exception:
        return ""

# ── db ─────────────────────────────────────────────────────────────────────
def db():
    c = sqlite3.connect(DB, timeout=10); c.execute("PRAGMA journal_mode=WAL"); return c
def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS positions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, coin TEXT, outcome TEXT,
        token_id TEXT, condition_id TEXT, slug TEXT, whale TEXT,
        entry_price REAL, size_usdc REAL, shares REAL, close_ts INTEGER,
        status TEXT DEFAULT 'open', pnl REAL DEFAULT 0, resolved_at TEXT, mode TEXT,
        open_price REAL, dec_price REAL, move_bps REAL, secs_left INTEGER,
        pm_confirmed INTEGER DEFAULT 0,
        ask_shares REAL, ask_usdc REAL, would_fill INTEGER, cum_usdc REAL)""")
    # migrate older DBs that predate the depth columns
    cols = [r[1] for r in c.execute("PRAGMA table_info(positions)").fetchall()]
    for col, typ in (("ask_shares", "REAL"), ("ask_usdc", "REAL"), ("would_fill", "INTEGER"), ("cum_usdc", "REAL"),
                     ("pre_window_move_bps", "REAL"), ("price_trend", "REAL")):
        if col not in cols:
            c.execute(f"ALTER TABLE positions ADD COLUMN {col} {typ}")
    c.commit(); c.close()
def market_exposure(cond):
    c = db(); r = c.execute("SELECT COALESCE(SUM(size_usdc),0) FROM positions WHERE condition_id=?",(cond,)).fetchone()[0]; c.close()
    return r or 0.0

def halt_reason():
    """Account-survival guardrails. Returns a halt string, or None if clear to trade.
    Prevents a bad run from draining the account (drained = orders can't fill = bot dead)."""
    c = db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_pnl = c.execute("SELECT COALESCE(SUM(pnl),0) FROM positions "
                          "WHERE status IN('won','lost') AND substr(ts,1,10)=?", (today,)).fetchone()[0]
    total_pnl = c.execute("SELECT COALESCE(SUM(pnl),0) FROM positions").fetchone()[0]
    streak_rows = c.execute("SELECT status FROM positions WHERE status IN('won','lost') "
                            "ORDER BY id DESC LIMIT ?", (MAX_LOSS_STREAK,)).fetchall()
    c.close()
    balance = START_BAL + total_pnl
    floor = MIN_BALANCE if MIN_BALANCE > 0 else BASE_SIZE
    if today_pnl <= -DAILY_LOSS_LIMIT:
        return f"DAILY LOSS LIMIT (today ${today_pnl:.2f} <= -${DAILY_LOSS_LIMIT:.0f})"
    if balance < floor:
        return f"BALANCE FLOOR (${balance:.2f} < ${floor:.2f} — can't fund a bet)"
    if (len(streak_rows) >= MAX_LOSS_STREAK and
            all(r[0] == "lost" for r in streak_rows)):
        return f"LOSS STREAK ({MAX_LOSS_STREAK} in a row — edge may be broken)"
    return None

# ── http ───────────────────────────────────────────────────────────────────
async def jget(session, url, timeout=8):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        return None
    return None

def _tg_send(msg):
    try:
        import urllib.request, urllib.parse
        u=f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        d=urllib.parse.urlencode({"chat_id":TG_CHAT,"text":msg}).encode()
        urllib.request.urlopen(urllib.request.Request(u,data=d),timeout=8)
    except Exception: pass

def tg(msg):
    """Fire-and-forget Telegram alert. Runs the blocking HTTP POST in a thread so it
    NEVER stalls the asyncio event loop (critical for a latency bot — a slow chat
    message must not delay the next snipe)."""
    if not (TG_TOKEN and TG_CHAT): return
    try:
        asyncio.get_running_loop().create_task(asyncio.to_thread(_tg_send, msg))
    except RuntimeError:
        _tg_send(msg)   # no running loop (shouldn't happen here) — fall back to sync

# ── live CLOB client (lazy) ──────────────────────────────────────────────────
_client = None
presigned = {}    # LIVE only: token -> pre-signed FOK order, built during the snipe window so fire() only POSTs it (shaves the ~30-50ms EIP-712 signing off the fire path). Unused in paper.
def live_client():
    global _client
    if _client is None:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.constants import POLYGON
        from py_clob_client_v2.clob_types import ApiCreds
        _client = ClobClient(CLOB, key=os.getenv("PRIVATE_KEY"), chain_id=POLYGON,
                             creds=ApiCreds(api_key=os.getenv("CLOB_API_KEY"),
                                            api_secret=os.getenv("CLOB_SECRET"),
                                            api_passphrase=os.getenv("CLOB_PASSPHRASE")),
                             signature_type=3, funder=os.getenv("POLYMARKET_PROXY_ADDRESS"))
    return _client

# ── 1) Binance price feed (WSS) ──────────────────────────────────────────────
async def binance_feed():
    streams = "/".join(f"{BINANCE_SYM[c].lower()}@trade" for c in COINS if c in BINANCE_SYM)
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    rev = {v: k for k, v in BINANCE_SYM.items()}
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(url, heartbeat=20) as ws:
                    print(f"[{ts_str()}] binance feed connected: {streams}")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(msg.data).get("data", {})
                            sym = d.get("s"); p = d.get("p")
                            if sym in rev and p:
                                prices[rev[sym]] = float(p)
                                price_ts[rev[sym]] = now()
        except Exception as e:
            print(f"[{ts_str()}] binance feed reconnect: {e}")
            await asyncio.sleep(2)

# ── 1b) Polymarket order-book feed (WSS) ─────────────────────────────────────
# Real-time asks via the CLOB public "market" channel, so best_ask() reads the
# book from memory (microseconds) instead of a ~25ms REST round-trip — and, more
# importantly, it never MISSES depth that appears+vanishes between 50ms polls.
# Pure fast-path: if this feed lags or drops, best_ask() falls back to REST, so
# it can only improve fills, never break them. Kill switch: USE_WSS_BOOK=false.
def wss_best_ask(token):
    """Best ask for `token` from the in-memory WSS book. Returns:
      (price, size) — a live ask from the real-time book
      (None, 0.0)   — WSS HAS this token's book and it's SWEPT (no ask). AUTHORITATIVE:
                      the caller must NOT REST-fallback — the book really is empty.
      None          — no usable WSS data (feed stalled, or no snapshot yet for this
                      token) -> caller should REST-fallback.
    The (None,0.0) vs None split is what makes a tightened (10ms) snipe loop safe:
    the normal last-second 'swept book' is answered straight from the WSS feed, so we
    don't hammer REST 100x/s during every market's final seconds. REST only fires when
    WSS genuinely lacks data (brief reconnect / pre-snapshot)."""
    if now() - book_conn_ts["last"] > WSS_FRESH_SECS:
        return None                        # feed stalled/disconnected -> REST
    if token not in books:
        return None                        # no snapshot for this token yet -> REST
    asks = books[token]                    # present (possibly empty = swept)
    if not asks:
        return (None, 0.0)                 # WSS says SWEPT -> authoritative, no REST
    bp = min(asks)                         # best (lowest) ask price
    sz = asks[bp]                          # shares resting at it (book levels pre-aggregated)
    if sz <= 0:
        return (None, 0.0)
    return (bp, sz)

async def book_feed():
    """Maintain `books` from the CLOB market channel. Subscribes to every tracked
    token; reconnects (resubscribes) when new tokens appear. Defensive parsing:
    handles book snapshots, price_change deltas (BOTH 'price_changes' and legacy
    'changes' arrays), and single-or-list message envelopes."""
    if not USE_WSS_BOOK:
        return
    while True:
        toks = sorted(markets.keys())
        if not toks:
            await asyncio.sleep(1); continue
        subscribed = set(toks)
        # prune books for tokens no longer tracked (closed windows)
        for t in [t for t in books if t not in markets]:
            books.pop(t, None)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(CLOB_WSS, heartbeat=10) as ws:
                    await ws.send_json({"assets_ids": toks, "type": "market"})
                    print(f"[{ts_str()}] book feed (WSS) connected: {len(toks)} tokens")
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        book_conn_ts["last"] = now()
                        try:
                            data = json.loads(msg.data)
                        except Exception:
                            continue
                        for ev in (data if isinstance(data, list) else [data]):
                            if not isinstance(ev, dict):
                                continue
                            et = ev.get("event_type")
                            if et == "book":
                                aid = ev.get("asset_id")
                                d = {}
                                for a in ev.get("asks") or []:
                                    try:
                                        p = float(a["price"]); sz = float(a["size"])
                                        if sz > 0: d[p] = sz
                                    except Exception:
                                        pass
                                if aid: books[aid] = d
                            elif et == "price_change":
                                for ch in (ev.get("price_changes") or ev.get("changes") or []):
                                    if str(ch.get("side")).upper() != "SELL":
                                        continue          # only the ask side matters (we buy)
                                    aid = ch.get("asset_id") or ev.get("asset_id")
                                    if not aid:
                                        continue
                                    try:
                                        p = float(ch["price"]); sz = float(ch["size"])
                                    except Exception:
                                        continue
                                    d = books.setdefault(aid, {})
                                    if sz <= 0: d.pop(p, None)
                                    else:       d[p] = sz
                        # a new window appeared — reconnect to add it to the subscription
                        if set(markets.keys()) - subscribed:
                            break
        except Exception as e:
            print(f"[{ts_str()}] book feed reconnect: {e}")
        await asyncio.sleep(1)

# ── 2) market discovery + open-price backfill ───────────────────────────────
async def binance_open(session, coin, start_ts):
    sym = BINANCE_SYM.get(coin)
    d = await jget(session, f"{BINANCE_REST}/api/v3/klines?symbol={sym}&interval=5m"
                            f"&startTime={start_ts*1000}&limit=1")
    if d and len(d) and len(d[0]) > 1:
        return float(d[0][1])     # candle open
    return None

async def discovery():
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                # 2026-05-22: targeted discovery. Previously we pulled the "soonest
                # 100 markets by endDate" and filtered — but at busy moments all 100
                # close within ~5 min, so BTC/ETH 15m windows fell off the list and a
                # restart left the bot tracking only 5m. Now we query each (coin,dur)
                # window DIRECTLY by its exact, predictable slug. Slugs are
                # "{coin}-updown-{dur}-{startTs}" with startTs aligned to the duration
                # — so this always finds every window with full lead time, regardless
                # of how many other markets are closing.
                t = int(now())          # int: slug startTs must be an integer
                for coin in COINS:
                    cl = coin.lower()
                    for dur_label in DURATIONS:
                        dur_secs = DUR_MAP.get(dur_label)
                        if not dur_secs:
                            continue
                        cur_start = (t // dur_secs) * dur_secs
                        for k in range(3):                       # current + next 2 windows
                            start = cur_start + k * dur_secs
                            close_ts = start + dur_secs          # CORRECT close per duration
                            if close_ts <= now() + 1:
                                continue
                            slug = f"{cl}-updown-{dur_label}-{start}"
                            if any(mm.get("slug") == slug for mm in markets.values()):
                                continue                          # already tracked
                            mk = await jget(s, f"{GAMMA}/markets?slug={slug}")
                            if not mk:
                                continue                          # window not created yet
                            m = mk[0]
                            toks = m.get("clobTokenIds"); outs = m.get("outcomes")
                            if isinstance(toks, str): toks = json.loads(toks)
                            if isinstance(outs, str): outs = json.loads(outs)
                            if not toks or not outs:
                                continue
                            cond = m.get("conditionId")
                            open_px = await binance_open(s, coin, start)
                            for tok, out in zip(toks, outs):
                                markets[tok] = {"coin": coin, "side": out, "cond": cond,
                                                "slug": slug, "open_price": open_px,
                                                "close_ts": close_ts, "dur": dur_label}
                # prune closed markets from state (and the sniped set, so it can't grow unbounded)
                for tok in [t for t, mm in markets.items() if mm["close_ts"] < now() - 5]:
                    markets.pop(tok, None)
                    sniped.discard(tok)
                    thin_skip.discard(tok)
                # self-heal: retry open-price for any tracked market still missing it
                # (transient klines failures on restart bursts otherwise leave it untradeable)
                for tok, mm in list(markets.items()):
                    if mm.get("open_price") is None:
                        ds = DUR_MAP.get(mm.get("dur","5m"), 300)
                        op2 = await binance_open(s, mm["coin"], mm["close_ts"] - ds)
                        if op2:
                            mm["open_price"] = op2
            except Exception as e:
                print(f"[{ts_str()}] discovery err: {e}")
            await asyncio.sleep(DISCOVERY_SECS)

# ── 3) ask refresh for markets in their snipe window ─────────────────────────
async def best_ask(session, token):
    """Returns (best_ask_price, shares_offered_at_that_price). The size lets us
    estimate whether our stake would ACTUALLY fill live (FOK at the ask only
    consumes liquidity at the best price).

    Fast path: read the real-time WSS book (microseconds, never misses sub-poll
    depth). Fallback: a REST /book round-trip if the feed has no fresh data for
    this token — so a feed hiccup can never block a fire."""
    if USE_WSS_BOOK:
        wa = wss_best_ask(token)
        if wa is not None:
            book_ms["last"] = 0; book_src["last"] = "wss"   # served from memory — no round-trip
            return wa
    _t0 = time.perf_counter()
    b = await jget(session, f"{CLOB}/book?token_id={token}", timeout=2)
    book_ms["last"] = round((time.perf_counter() - _t0) * 1000)   # real round-trip to PM
    book_src["last"] = "rest"
    if not b or not isinstance(b, dict): return None, 0.0
    try:
        asks = [(float(a["price"]), float(a.get("size", 0)))
                for a in (b.get("asks") or []) if float(a.get("size", 0)) > 0]
        if not asks: return None, 0.0
        bp = min(p for p, _ in asks)                 # best (lowest) ask price
        sz = sum(s for p, s in asks if p == bp)      # shares offered AT that price
        return bp, sz
    except Exception:
        return None, 0.0

def walk_book_fill(book, target_usdc, max_ask):
    """Realistic multi-level FOK fill: walk the ask ladder cheapest-first, taking only
    levels priced <= max_ask, accumulating up to target_usdc. A $30 order lifts the best
    ask, then the next level, etc., filling at a BLENDED price. Returns
    (blended_avg_price, total_shares, total_cost_usdc, fully_filled). fully_filled is True
    iff the in-band book (<= max_ask) holds at least the full target — i.e. a live FOK for
    the full stake would clear. `book` is the {price: size} ask dict for the token."""
    if not book:
        return (None, 0.0, 0.0, False)
    cost = 0.0; shares = 0.0
    for p in sorted(book):
        if p > max_ask:
            break
        take = min(p * book[p], target_usdc - cost)   # USDC taken from this level
        if take <= 0:
            break
        cost += take; shares += take / p
        if cost >= target_usdc - 1e-9:
            break
    if shares <= 0:
        return (None, 0.0, 0.0, False)
    return (round(cost / shares, 6), round(shares, 4), round(cost, 2),
            cost >= target_usdc - 1e-6)

def _wick_snapshot(coin, op, leading_up, dur):
    """Snapshot price state just BEFORE the snipe window opened.
    Returns (pre_window_move_bps, price_trend).
    pre_window_move_bps: move at ~T-25s (5m) or ~T-40s (15m) — captures the wick height.
    price_trend: bps/5s relative to leader direction. Positive = moving away from open (good).
                 Negative = reverting toward open (wick warning)."""
    hist = list(price_hist.get(coin, []))
    lookback = 8 if dur == "5m" else 10   # entries back = ~seconds back (1 entry/s from state_writer)
    if not op or len(hist) < 2:
        return (None, None)
    pre_px = hist[-(lookback+1)] if len(hist) > lookback else hist[0]
    pre_move = round((pre_px - op) / op * 1e4, 1)
    if len(hist) >= 6:
        trend_raw = (hist[-1] - hist[-6]) / op * 1e4   # bps change over last ~5s
        p_trend = round(trend_raw if leading_up else -trend_raw, 2)  # positive = away from open
    else:
        p_trend = None
    return (pre_move, p_trend)

# ── 4) snipe loop ────────────────────────────────────────────────────────────
async def snipe_loop():
    async with aiohttp.ClientSession() as s:
        prev_halt = None
        while True:
            t = now()
            # ── account-survival guardrails: stop trading if tripped ──
            reason = halt_reason()
            HALT["reason"] = reason
            if reason:
                if reason != prev_halt:
                    print(f"[{ts_str()}] *** TRADING HALTED — {reason} ***"); tg(f"GHOST PREDATOR HALTED: {reason}")
                    prev_halt = reason
                await asyncio.sleep(max(1.0, BOOK_POLL_MS/1000)); continue
            if prev_halt:
                print(f"[{ts_str()}] guardrail cleared — trading resumed"); prev_halt = None
            for tok, m in list(markets.items()):
                left = m["close_ts"] - t
                win = snipe_window_for(m.get("dur", "5m"))         # 5m=12s, 15m=30s
                if left > win or left < MIN_FILL_SECS:             # only act inside window
                    continue
                if tok in sniped: continue
                coin = m["coin"]; op = m["open_price"]; cur = prices.get(coin)
                if op is None or cur is None: continue
                if now() - price_ts.get(coin, 0) > PRICE_MAX_AGE: continue   # stale feed — don't trade blind
                if ETH_SKIP_15M and coin == "ETH" and m.get("dur") == "15m": continue  # ETH 15m: 54% WR coinflip (Chainlink disagree)
                move_bps = (cur - op) / op * 1e4          # signed bps
                # is THIS token the leading side?
                leading_up = cur >= op
                is_up = m["side"].upper() in ("UP", "YES")
                token_is_leader = (leading_up and is_up) or ((not leading_up) and (not is_up))
                if not token_is_leader: continue
                coin_min_move = ETH_MIN_MOVE_BPS if coin == "ETH" else BTC_MIN_MOVE_BPS
                if abs(move_bps) < coin_min_move: continue  # per-coin move threshold
                # refresh ask (+ how many shares are offered at it)
                ask, ask_sz = await best_ask(s, tok)
                if ask is None or not (MIN_ASK <= ask <= MAX_ASK): continue
                exp = market_exposure(m["cond"])
                if exp >= MAX_PER_MARKET: continue
                target = min(BASE_SIZE, MAX_PER_MARKET - exp)
                # cumulative in-band ask depth (<= MAX_ASK) = the walk-the-book fill ceiling
                cum_usdc = round(sum(p * s for p, s in books.get(tok, {}).items() if p <= MAX_ASK), 2)
                if WALK_FILL:
                    # ── walk the ladder to fill the FULL $30 within MAX_ASK, at a blended price ──
                    eff_ask, walk_sh, walk_cost, fully = walk_book_fill(books.get(tok, {}), target, MAX_ASK)
                    if eff_ask is None:
                        continue                                  # nothing resting in-band
                    if FILL_FLOOR > 0 and eff_ask < FILL_FLOOR:
                        if tok not in thin_skip:
                            print(f"[{ts_str()}] [skip-floor] {coin} {m['side']} {m.get('dur','5m')} "
                                  f"blended {eff_ask:.3f} < FILL_FLOOR {FILL_FLOOR:.2f} — stale WSS levels, not fired")
                            thin_skip.add(tok)
                        continue
                    if (not fully) or walk_cost < MIN_STAKE:
                        # in-band book can't supply the full stake -> a live FOK for $30 would miss.
                        # Skip the dust: thin fills don't survive live slippage/fees anyway.
                        if tok not in thin_skip:
                            print(f"[{ts_str()}] [skip-thin] {coin} {m['side']} {m.get('dur','5m')} "
                                  f"best {ask:.3f} fills only ${walk_cost:.0f}/${target:.0f} <= {MAX_ASK} — not fired")
                            thin_skip.add(tok)
                        continue
                    sniped.add(tok)
                    pre_move, p_trend = _wick_snapshot(coin, op, leading_up, m.get("dur","5m"))
                    await fire(s, tok, m, eff_ask, walk_cost, move_bps, int(left), cur, walk_sh, cum_usdc, pre_move, p_trend)
                    continue
                # ── legacy sizing (WALK_FILL=false): depth-matched best-ask fill ──
                size = target
                depth_usdc = ask_sz * ask                        # USDC actually resting at the best ask
                if DYNAMIC_SIZE:
                    # size to the book: never ask for more than the depth holds, so the
                    # FOK fills IN FULL. BASE_SIZE is the cap.
                    size = min(size, depth_usdc)
                elif REQUIRE_FILL and depth_usdc < size:
                    if tok not in thin_skip:
                        print(f"[{ts_str()}] [skip-thin] {coin} {m['side']} {m.get('dur','5m')} "
                              f"ask {ask:.3f} depth ${depth_usdc:.0f} < ${size:.0f} — not fired (would miss live)")
                        thin_skip.add(tok)
                    continue
                if size < MIN_STAKE:                             # below PM min order -> truly unfillable
                    if tok not in thin_skip:
                        print(f"[{ts_str()}] [skip-thin] {coin} {m['side']} {m.get('dur','5m')} "
                              f"depth ${depth_usdc:.0f} < ${MIN_STAKE:.0f} min — not fired")
                        thin_skip.add(tok)
                    continue
                sniped.add(tok)
                pre_move, p_trend = _wick_snapshot(coin, op, leading_up, m.get("dur","5m"))
                await fire(s, tok, m, ask, size, move_bps, int(left), cur, ask_sz, cum_usdc, pre_move, p_trend)
            await asyncio.sleep(BOOK_POLL_MS / 1000)

async def fire(session, tok, m, ask, size, move_bps, left, cur, ask_sz=0.0, cum_usdc=0.0, pre_window_move_bps=None, price_trend=None):
    coin, side, cond, slug = m["coin"], m["side"], m["cond"], m["slug"]
    mode = "PAPER" if PAPER else "LIVE"
    fill_price, shares = ask, round(size/ask, 4)
    # depth-based fill estimate: USDC actually resting at the ask, and whether
    # our stake would clear it. Paper = estimate; live overwrites with real result.
    ask_usdc = round(ask_sz * ask, 2)
    # would_fill: does resting depth cover the (possibly depth-capped) stake? Compare
    # UNROUNDED with an epsilon so dynamic-sized orders (size == depth) aren't flipped
    # to 0 by sub-cent rounding — under DYNAMIC_SIZE every recorded order fills in full.
    would_fill = 1 if (ask_sz * ask) + 1e-6 >= size else 0
    if not PAPER:
        try:
            from py_clob_client_v2.clob_types import MarketOrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import BUY
            cl = live_client()
            # walk-the-book live FOK: spend BASE_SIZE ($30), fill any level <= MAX_ASK (else kill).
            # Prefer the order PRE-SIGNED in the snipe window (presign_loop) — then we only POST,
            # skipping the ~30-50ms EIP-712 signing. Fall back to signing inline if none is cached.
            order = presigned.pop(tok, None)
            if order is None:
                amt = BASE_SIZE if WALK_FILL else size
                cap = MAX_ASK if WALK_FILL else ask
                args = MarketOrderArgs(token_id=tok, amount=amt, side=BUY, price=cap,
                                       order_type=OrderType.FOK)
                order = cl.create_market_order(args)
            resp = cl.post_order(order, OrderType.FOK)
            taking = float(resp.get("takingAmount") or 0); making = float(resp.get("makingAmount") or 0)
            if making < 0.01:
                print(f"[{ts_str()}] [miss] {coin} {side} FOK didn't fill | raw resp: {resp}"); return   # 2026-05-27: log raw resp — v2 changed response shape; first live order must confirm takingAmount/makingAmount keys
            shares=round(taking,4); fill_price=round(making/taking,6); size=making
            would_fill = 1   # FOK actually filled — real result
        except Exception as e:
            print(f"[{ts_str()}] [live-err] {coin} {side}: {e}"); return
    c = db()
    cur_db = c.execute("""INSERT INTO positions(ts,coin,outcome,token_id,condition_id,slug,whale,
                 entry_price,size_usdc,shares,close_ts,status,mode,open_price,dec_price,
                 move_bps,secs_left,ask_shares,ask_usdc,would_fill,cum_usdc,
                 pre_window_move_bps,price_trend)
                 VALUES(?,?,?,?,?,?, 'SNIPE', ?,?,?,?, 'open', ?,?,?,?,?,?,?,?,?,?,?)""",
              (datetime.now(timezone.utc).isoformat(), coin, side, tok, cond, slug,
               fill_price, size, shares, m["close_ts"], mode, m["open_price"], cur,
               round(move_bps,1), left, round(ask_sz,2), ask_usdc, would_fill, cum_usdc,
               pre_window_move_bps, price_trend))
    pid = cur_db.lastrowid
    c.commit(); c.close()
    dur = m.get("dur", "5m")
    print(f"[{ts_str()}] SNIPE #{pid} {mode} {coin} {side} {dur} @ {fill_price:.3f} "
          f"${size:.2f} ({shares:.1f}sh) | {move_bps:+.1f}bps | {left}s left | "
          f"depth ${ask_usdc:.0f} (cum<={MAX_ASK} ${cum_usdc:.0f}) {'FILL' if would_fill else 'THIN~miss'}")
    # ── rich Telegram FIRE alert ──
    up = side.upper() in ("UP", "YES")
    mult = (1/fill_price) if fill_price else 0
    profit = size*(mult-1) if fill_price else 0
    implied = fill_price*100
    tag = "PAPER " if PAPER else ""
    tg(f"🎯 {tag}FIRE #{pid} — {dur} snipe\n"
       f"{'🟢' if up else '🔴'} {coin} {side.upper()} @ ${fill_price:.4f}\n\n"
       f"💰 Bought {shares:.1f} shares for ${size:.2f}\n"
       f"📊 Depth @ ask: ${ask_usdc:.0f} — {'would fill' if would_fill else 'TOO THIN, live miss likely'}\n"
       f"📈 If WIN: pays +${profit:.2f} ({mult:.1f}× return)\n"
       f"📉 If LOSE: -${size:.2f}\n"
       f"🎲 Market-implied {side.upper()} prob: {implied:.1f}% (other side: {100-implied:.1f}%)\n\n"
       f"⏰ Fired {datetime.now(timezone.utc):%H:%M:%S} UTC\n"
       f"📋 {coin} Up or Down · {dur} · {et_window(m['close_ts'], DUR_MAP.get(dur,300))} · {left}s to close")

# ── status ───────────────────────────────────────────────────────────────────
async def status_loop():
    while True:
        await asyncio.sleep(30)
        c = db()
        o = c.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
        tot, won, pnl = c.execute("SELECT COUNT(*), COALESCE(SUM(status='won'),0), COALESCE(SUM(pnl),0) FROM positions").fetchone()
        c.close()
        inwin = sum(1 for m in markets.values() if 0 < m["close_ts"]-now() <= snipe_window_for(m.get("dur","5m")))
        px = " ".join(f"{c}={prices.get(c,0):.0f}" for c in COINS)
        print(f"[{ts_str()}] tracking {len(markets)} mkts | {inwin} in-window | "
              f"open {o} | total {tot} won {won} pnl ${pnl:+.2f} | {px}")

STATE_FILE = os.path.join(SD, "state.json")
async def state_writer():
    """Dump live hunt state every 1s for the dashboard to read.
    Groups by market, finds the leading token, and (for markets near close)
    fetches the leading side's live ask so the dashboard cards show real edge."""
    async with aiohttp.ClientSession() as sess:
        while True:
            try:
                t = now()
                bs = {}   # slug -> {coin, close_ts, open, tokens:{UP:tok,DOWN:tok}}
                for tok, m in markets.items():
                    d = bs.setdefault(m["slug"], {"coin": m["coin"], "close_ts": m["close_ts"],
                                                  "open": m["open_price"], "dur": m.get("dur","5m"),
                                                  "tokens": {}})
                    d["tokens"][m["side"].upper()] = tok
                hunt = []
                for slug, d in bs.items():
                    left = d["close_ts"] - t
                    if left < -2 or left > 960:      # cover 15m windows (900s) so their cards populate too
                        continue
                    cur, op = prices.get(d["coin"]), d["open"]
                    if op and cur:
                        move = (cur - op) / op * 1e4
                        leader = "UP" if cur >= op else "DOWN"
                    else:
                        move, leader = 0.0, "?"
                    ask = None
                    if left <= 60 and leader in ("UP", "DOWN"):   # fetch ask as it approaches
                        ltok = d["tokens"].get(leader)
                        if ltok:
                            ask, _ = await best_ask(sess, ltok)
                    _dl = d.get("dur","5m")
                    hunt.append({"coin": d["coin"], "leader": leader, "dur": _dl,
                                 "window": et_window(d["close_ts"], DUR_MAP.get(_dl,300)),
                                 "move_bps": round(move, 1), "pct": round(move/100, 3),
                                 "secs_left": int(left), "price": cur, "ask": ask,
                                 "in_window": MIN_FILL_SECS <= left <= snipe_window_for(_dl),
                                 "decisive": abs(move) >= MIN_MOVE_BPS})
                hunt.sort(key=lambda x: x["secs_left"])
                # ── price history + per-coin chart data (price-to-beat + countdown) ──
                for cc in COINS:
                    pv = prices.get(cc)
                    if pv:
                        price_hist.setdefault(cc, deque(maxlen=90)).append(round(pv, 2))
                charts = {}
                for cc in COINS:
                    for dl in sorted({d.get("dur","5m") for d in bs.values() if d["coin"]==cc}):
                        cms = [d for d in bs.values() if d["coin"]==cc and d.get("dur","5m")==dl]
                        if not cms:
                            continue
                        d = min(cms, key=lambda x: x["close_ts"])   # imminent (coin,dur) market
                        charts[f"{cc}_{dl}"] = {"open": d["open"], "coin": cc, "dur": dl,
                                                "window": et_window(d["close_ts"], DUR_MAP.get(dl,300)),
                                                "secs_left": int(d["close_ts"] - t),
                                                "now": prices.get(cc),
                                                "hist": list(price_hist.get(cc, []))}
                payload = {"ts": t, "mode": "PAPER" if PAPER else "LIVE",
                           "bankroll": START_BAL, "prices": prices, "tracking": len(hunt),
                           "halt": HALT["reason"], "charts": charts,
                           "cfg": {"window": SNIPE_WINDOW, "window_15m": SNIPE_WINDOW_15M, "min_move": MIN_MOVE_BPS,
                                   "max_ask": MAX_ASK, "min_fill": MIN_FILL_SECS,
                                   "base_size": BASE_SIZE, "max_mkt": MAX_PER_MARKET,
                                   "book_poll": BOOK_POLL_MS, "book_ms": book_ms["last"],
                                   "book_feed": ("wss" if USE_WSS_BOOK else "rest"), "book_src": book_src["last"],
                                   "wss_live": (now() - book_conn_ts["last"] <= WSS_FRESH_SECS),
                                   "daily_loss_limit": DAILY_LOSS_LIMIT,
                                   "max_loss_streak": MAX_LOSS_STREAK},
                           "hunt": hunt}
                tmp = STATE_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(payload, f)
                os.replace(tmp, STATE_FILE)
            except Exception:
                pass
            await asyncio.sleep(1)

def banner():
    print("="*66)
    print(f"  GHOST PREDATOR  —  LATENCY SNIPE  —  {'PAPER' if PAPER else '*** LIVE ***'}")
    print(f"  coins {COINS} | snipe window 5m={SNIPE_WINDOW}s 15m={SNIPE_WINDOW_15M}s (fill cutoff {MIN_FILL_SECS}s)")
    print(f"  leader must move >={MIN_MOVE_BPS}bps | ask {MIN_ASK}-{MAX_ASK} | "
          f"${BASE_SIZE}/snipe cap ${MAX_PER_MARKET}/mkt")
    print(f"  bankroll ${START_BAL:.0f} | book {'WSS realtime (REST fallback)' if USE_WSS_BOOK else f'REST {BOOK_POLL_MS}ms'} | db {os.path.basename(DB)}")
    print("="*66)

# ── order-path pre-warm (LIVE only) ─────────────────────────────────────────
async def prewarm_loop():
    """Keep the order client's TLS connection HOT during snipe windows, so a live
    FOK fires on a ~10ms warm connection instead of paying the ~27ms cold-TLS
    handshake (measured: warm ~10-15ms vs cold ~40ms to the CLOB). Network RTT is
    already ~1.9ms (Cloudflare edge) and NOT the bottleneck — this handshake is the
    only order-latency lever left. No-op in paper.
    NOTE: verify get_server_time() exists in your py_clob_client_v2 when you go live;
    it's wrapped safe — a wrong method name just makes this a harmless no-op."""
    if PAPER:
        return
    print(f"[{ts_str()}] order pre-warm active (live) — keeping CLOB TLS hot near close")
    while True:
        try:
            t = now()
            if any(0 < (m["close_ts"] - t) <= 75 for m in markets.values()):  # near any close
                await asyncio.to_thread(live_client().get_server_time)         # cheap GET → keeps the order TLS pool warm
        except Exception:
            pass
        await asyncio.sleep(5)

# ── order pre-signing (LIVE only) ───────────────────────────────────────────
async def presign_loop():
    """Pre-build & SIGN the $30 FOK during each market's snipe window, so fire() only POSTs it
    — shaving the ~30-50ms EIP-712 signing (the single slowest step on the fire path) off the
    moment that matters. The order is amount=BASE_SIZE, price=MAX_ASK (the walk-the-book cap:
    'spend $30, fill anything <= MAX_ASK'), so its params are CONSTANT per token and the
    signature does NOT go stale on ask drift — we just need the token to still be a fire
    candidate when it triggers. fire() pops the cached order; if absent it signs inline. No-op
    in paper. VERIFY when going live: that create_market_order() returns a reusable signed order
    and a few-seconds-old FOK salt/nonce is still accepted. Wrapped safe — any error falls back
    to inline signing in fire(), so it can never break a fire."""
    if PAPER:
        return
    print(f"[{ts_str()}] order pre-signing active (live) — signing $30 FOKs in the snipe window")
    while True:
        try:
            t = now()
            for tok, m in list(markets.items()):
                left = m["close_ts"] - t
                if left > snipe_window_for(m.get("dur", "5m")) or left < MIN_FILL_SECS:
                    continue                                   # only within the fire window
                if tok in presigned or tok in sniped:
                    continue
                from py_clob_client_v2.clob_types import MarketOrderArgs, OrderType
                from py_clob_client_v2.order_builder.constants import BUY
                cap = MAX_ASK if WALK_FILL else None
                args = MarketOrderArgs(token_id=tok, amount=BASE_SIZE, side=BUY,
                                       price=(cap if cap is not None else MAX_ASK),
                                       order_type=OrderType.FOK)
                presigned[tok] = await asyncio.to_thread(live_client().create_market_order, args)
                print(f"[{ts_str()}] [presign] {m['coin']} {m['side']} {m.get('dur','5m')} — $30 FOK @ <={MAX_ASK} ready")
            for tk in [k for k in presigned if k not in markets]:   # prune closed windows
                presigned.pop(tk, None)
        except Exception as e:
            print(f"[{ts_str()}] [presign-err] {e}")
        await asyncio.sleep(0.5)

async def main():
    init_db(); banner()
    await asyncio.gather(binance_feed(), book_feed(), discovery(), snipe_loop(),
                         status_loop(), state_writer(), prewarm_loop(), presign_loop())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[stopped]")
