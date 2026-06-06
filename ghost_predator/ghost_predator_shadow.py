#!/usr/bin/env python3
"""
GHOST PREDATOR SHADOW COLLECTOR — data collection only, always paper.

Same snipe logic as ghost_predator.py but expanded scope:
  Coins:  BTC, ETH, XRP, SOL
  Durations: 5m + 15m
  Purpose: collect win-rate and entry-band data across all coins/durations
           to find where real edge exists before committing real money.

No .env needed — all config is hardcoded below.
No real money ever — PAPER = True is hardcoded.
No Telegram alerts.
Separate DB: shadow_predator.db (never touches ghost_predator.db).
"""
import os, sys, time, json, sqlite3, asyncio
from collections import deque
from datetime import datetime, timezone, timedelta

try:
    import aiohttp
except ImportError:
    sys.exit("needs aiohttp:  pip install aiohttp")

SD = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(SD, "shadow_predator.db")

# ── config (all hardcoded — no .env needed) ────────────────────────────────
PAPER           = True
START_BAL       = 1000.0
COINS           = ["BTC", "ETH", "XRP", "SOL"]
DUR_MAP         = {"5m":300, "15m":900, "30m":1800, "1h":3600, "4h":14400, "1d":86400}
DURATIONS       = {"5m", "15m"}
SNIPE_WINDOW    = 28        # 5m snipe window (seconds before close)
SNIPE_WINDOW_15M= 20        # 15m snipe window (20s — settled but less contested)
def snipe_window_for(dur):
    return SNIPE_WINDOW_15M if dur == "15m" else SNIPE_WINDOW
MIN_FILL_SECS   = 5
MIN_MOVE_BPS    = 1.0
BTC_MIN_MOVE_BPS = 1.0
ETH_MIN_MOVE_BPS = 1.0
XRP_MIN_MOVE_BPS = 1.0
SOL_MIN_MOVE_BPS = 1.0
ETH_SKIP_15M    = False     # collect 15m data for all coins
BTC_SKIP_15M    = False
BLOCKED_HOURS   = set()     # no hour blocks — collect all hours
MIN_ASK         = 0.65
MAX_ASK         = 0.78
BASE_SIZE       = 20.0
MAX_PER_MARKET  = 20.0
BTC_BASE_SIZE   = 20.0
ETH_BASE_SIZE   = 20.0
XRP_BASE_SIZE   = 20.0
SOL_BASE_SIZE   = 20.0
BTC_MAX_PER_MARKET = 20.0
ETH_MAX_PER_MARKET = 20.0
XRP_MAX_PER_MARKET = 20.0
SOL_MAX_PER_MARKET = 20.0
REQUIRE_FILL    = True
DYNAMIC_SIZE    = True
MIN_STAKE       = 1.0
FILL_FLOOR      = 0.50
WALK_FILL       = True
BOOK_POLL_MS    = 100       # slower poll — shadow doesn't need 10ms precision
USE_WSS_BOOK    = True
DISCOVERY_SECS  = 20
PRICE_MAX_AGE   = 3.0
DAILY_LOSS_LIMIT  = 999999  # disabled — paper only, no halting
MAX_LOSS_STREAK   = 999     # disabled
MIN_BALANCE       = 0.0
ROLLING_24H_LIMIT = 0.0
REGIME_GATE     = False     # always fire — collect all signals for data
REGIME_WINDOW   = 15
FIRSTMOVER_ENABLED = False
TG_TOKEN        = ""        # no Telegram
TG_CHAT         = ""

BINANCE_SYM = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
               "XRP": "XRPUSDT", "BNB": "BNBUSDT"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
CLOB_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_REST = "https://api.binance.com"

# ── shared state ──────────────────────────────────────────────────────────────
REGIME = {"gate": True, "edge": None, "n": 0, "paper_n": 0, "paper_edge": None}
FIRSTMOVER = {"signal": "WARMUP", "edge": None, "n": 0, "ts": 0}
prices  = {}
price_ts = {}
markets = {}
sniped  = set()
thin_skip = set()
HALT    = {"reason": None}
price_hist = {}
book_ms = {"last": None}
books   = {}
book_conn_ts = {"last": 0}
book_src = {"last": "-"}
WSS_FRESH_SECS = 3.0
def ts_str(): return datetime.now().strftime("%H:%M:%S")
def now(): return time.time()
def et_window(close_ts, dur_secs):
    try:
        s = datetime.fromtimestamp(close_ts - dur_secs, timezone.utc) - timedelta(hours=4)
        e = datetime.fromtimestamp(close_ts, timezone.utc) - timedelta(hours=4)
        return f"{s.strftime('%-I:%M')}-{e.strftime('%-I:%M')} ET"
    except Exception:
        return ""

# ── db ────────────────────────────────────────────────────────────────────────
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
    c.execute("""CREATE TABLE IF NOT EXISTS shadow_positions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, coin TEXT, outcome TEXT,
        token_id TEXT, condition_id TEXT, slug TEXT,
        entry_price REAL, close_ts INTEGER,
        status TEXT DEFAULT 'open', resolved_at TEXT,
        open_price REAL, dec_price REAL, move_bps REAL, secs_left INTEGER)""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(positions)").fetchall()]
    for col, typ in (("ask_shares", "REAL"), ("ask_usdc", "REAL"), ("would_fill", "INTEGER"),
                     ("cum_usdc", "REAL"), ("pre_window_move_bps", "REAL"), ("price_trend", "REAL")):
        if col not in cols:
            c.execute(f"ALTER TABLE positions ADD COLUMN {col} {typ}")
    c.commit(); c.close()
def market_exposure(cond):
    c = db(); r = c.execute("SELECT COALESCE(SUM(size_usdc),0) FROM positions WHERE condition_id=?",(cond,)).fetchone()[0]; c.close()
    return r or 0.0

def halt_reason():
    return None   # shadow never halts

# ── http ──────────────────────────────────────────────────────────────────────
async def jget(session, url, timeout=8):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        return None
    return None

def tg(msg): pass   # no Telegram in shadow

# ── Binance price feed (WSS) ──────────────────────────────────────────────────
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

# ── Polymarket order-book feed (WSS) ─────────────────────────────────────────
def wss_best_ask(token):
    if now() - book_conn_ts["last"] > WSS_FRESH_SECS:
        return None
    if token not in books:
        return None
    asks = books[token]
    if not asks:
        return (None, 0.0)
    bp = min(asks); sz = asks[bp]
    if sz <= 0:
        return (None, 0.0)
    return (bp, sz)

async def book_feed():
    if not USE_WSS_BOOK:
        return
    while True:
        toks = sorted(markets.keys())
        if not toks:
            await asyncio.sleep(1); continue
        subscribed = set(toks)
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
                                aid = ev.get("asset_id"); d = {}
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
                                        continue
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
                        if set(markets.keys()) - subscribed:
                            break
        except Exception as e:
            print(f"[{ts_str()}] book feed reconnect: {e}")
        await asyncio.sleep(1)

# ── market discovery ──────────────────────────────────────────────────────────
async def binance_open(session, coin, start_ts):
    sym = BINANCE_SYM.get(coin)
    d = await jget(session, f"{BINANCE_REST}/api/v3/klines?symbol={sym}&interval=5m"
                            f"&startTime={start_ts*1000}&limit=1")
    if d and len(d) and len(d[0]) > 1:
        return float(d[0][1])
    return None

async def discovery():
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                t = int(now())
                for coin in COINS:
                    cl = coin.lower()
                    for dur_label in DURATIONS:
                        dur_secs = DUR_MAP.get(dur_label)
                        if not dur_secs:
                            continue
                        cur_start = (t // dur_secs) * dur_secs
                        for k in range(3):
                            start = cur_start + k * dur_secs
                            close_ts = start + dur_secs
                            if close_ts <= now() + 1:
                                continue
                            slug = f"{cl}-updown-{dur_label}-{start}"
                            if any(mm.get("slug") == slug for mm in markets.values()):
                                continue
                            mk = await jget(s, f"{GAMMA}/markets?slug={slug}")
                            if not mk:
                                continue
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
                for tok in [t for t, mm in markets.items() if mm["close_ts"] < now() - 5]:
                    markets.pop(tok, None)
                    sniped.discard(tok)
                    thin_skip.discard(tok)
                for tok, mm in list(markets.items()):
                    if mm.get("open_price") is None:
                        ds = DUR_MAP.get(mm.get("dur","5m"), 300)
                        op2 = await binance_open(s, mm["coin"], mm["close_ts"] - ds)
                        if op2:
                            mm["open_price"] = op2
            except Exception as e:
                print(f"[{ts_str()}] discovery err: {e}")
            await asyncio.sleep(DISCOVERY_SECS)

# ── ask refresh ───────────────────────────────────────────────────────────────
async def best_ask(session, token):
    if USE_WSS_BOOK:
        wa = wss_best_ask(token)
        if wa is not None:
            book_ms["last"] = 0; book_src["last"] = "wss"
            return wa
    _t0 = time.perf_counter()
    b = await jget(session, f"{CLOB}/book?token_id={token}", timeout=2)
    book_ms["last"] = round((time.perf_counter() - _t0) * 1000)
    book_src["last"] = "rest"
    if not b or not isinstance(b, dict): return None, 0.0
    try:
        asks = [(float(a["price"]), float(a.get("size", 0)))
                for a in (b.get("asks") or []) if float(a.get("size", 0)) > 0]
        if not asks: return None, 0.0
        bp = min(p for p, _ in asks)
        sz = sum(s for p, s in asks if p == bp)
        return bp, sz
    except Exception:
        return None, 0.0

def walk_book_fill(book, target_usdc, max_ask):
    if not book:
        return (None, 0.0, 0.0, False)
    cost = 0.0; shares = 0.0
    for p in sorted(book):
        if p > max_ask:
            break
        take = min(p * book[p], target_usdc - cost)
        if take <= 0:
            break
        cost += take; shares += take / p
        if cost >= target_usdc - 1e-9:
            break
    if shares <= 0:
        return (None, 0.0, 0.0, False)
    return (round(cost / shares, 6), round(shares, 4), round(cost, 2),
            cost >= target_usdc - 1e-6)

async def regime_loop():
    REGIME["gate"] = True   # always ON — shadow collects all data
    return

async def shadow_fire(tok, m, ask, move_bps, left, cur): pass

async def shadow_resolver_loop():
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                t_now = int(now())
                c = db()
                rows = c.execute("""SELECT id, coin, outcome, close_ts, open_price
                                    FROM shadow_positions
                                    WHERE status='open' AND close_ts < ?""",
                                 (t_now - 30,)).fetchall()
                c.close()
                for pid, coin, side, close_ts, open_px in rows:
                    sym = BINANCE_SYM.get(coin)
                    if not sym or not open_px:
                        continue
                    d = await jget(s, f"{BINANCE_REST}/api/v3/klines?symbol={sym}&interval=1m"
                                      f"&startTime={(close_ts-60)*1000}&limit=1")
                    if not d or not d[0]:
                        continue
                    close_px = float(d[0][4])
                    is_up = side.upper() in ("UP", "YES")
                    won = (close_px >= open_px) if is_up else (close_px < open_px)
                    status = 'won' if won else 'lost'
                    c = db()
                    c.execute("UPDATE shadow_positions SET status=?,resolved_at=? WHERE id=?",
                              (status, datetime.now(timezone.utc).isoformat(), pid))
                    c.commit(); c.close()
            except Exception as ex:
                print(f"[{ts_str()}] [shadow-resolve-err] {ex}")
            await asyncio.sleep(30)

async def firstmover_reader(): return

def _coin_min_move(coin):
    return {"BTC": BTC_MIN_MOVE_BPS, "ETH": ETH_MIN_MOVE_BPS,
            "XRP": XRP_MIN_MOVE_BPS, "SOL": SOL_MIN_MOVE_BPS}.get(coin, MIN_MOVE_BPS)

def _coin_base_size(coin):
    return {"BTC": BTC_BASE_SIZE, "ETH": ETH_BASE_SIZE,
            "XRP": XRP_BASE_SIZE, "SOL": SOL_BASE_SIZE}.get(coin, BASE_SIZE)

def _coin_max_market(coin):
    return {"BTC": BTC_MAX_PER_MARKET, "ETH": ETH_MAX_PER_MARKET,
            "XRP": XRP_MAX_PER_MARKET, "SOL": SOL_MAX_PER_MARKET}.get(coin, MAX_PER_MARKET)

def _wick_snapshot(coin, op, leading_up, dur):
    hist = list(price_hist.get(coin, []))
    lookback = 8 if dur == "5m" else 10
    if not op or len(hist) < 2:
        return (None, None)
    pre_px = hist[-(lookback+1)] if len(hist) > lookback else hist[0]
    pre_move = round((pre_px - op) / op * 1e4, 1)
    if len(hist) >= 6:
        trend_raw = (hist[-1] - hist[-6]) / op * 1e4
        p_trend = round(trend_raw if leading_up else -trend_raw, 2)
    else:
        p_trend = None
    return (pre_move, p_trend)

# ── snipe loop ────────────────────────────────────────────────────────────────
async def snipe_loop():
    async with aiohttp.ClientSession() as s:
        while True:
            t = now()
            HALT["reason"] = None
            for tok, m in list(markets.items()):
                left = m["close_ts"] - t
                win = snipe_window_for(m.get("dur", "5m"))
                if left > win or left < MIN_FILL_SECS:
                    continue
                if tok in sniped: continue
                coin = m["coin"]; op = m["open_price"]; cur = prices.get(coin)
                if op is None or cur is None: continue
                if now() - price_ts.get(coin, 0) > PRICE_MAX_AGE: continue
                move_bps = (cur - op) / op * 1e4
                leading_up = cur >= op
                is_up = m["side"].upper() in ("UP", "YES")
                token_is_leader = (leading_up and is_up) or ((not leading_up) and (not is_up))
                if not token_is_leader: continue
                if abs(move_bps) < _coin_min_move(coin): continue
                ask, ask_sz = await best_ask(s, tok)
                if ask is None or not (MIN_ASK <= ask <= MAX_ASK): continue
                coin_base = _coin_base_size(coin)
                coin_max  = _coin_max_market(coin)
                exp = market_exposure(m["cond"])
                if exp >= coin_max: continue
                target = min(coin_base, coin_max - exp)
                cum_usdc = round(sum(p * sz for p, sz in books.get(tok, {}).items() if p <= MAX_ASK), 2)
                if WALK_FILL:
                    eff_ask, walk_sh, walk_cost, fully = walk_book_fill(books.get(tok, {}), target, MAX_ASK)
                    if eff_ask is None: continue
                    if FILL_FLOOR > 0 and eff_ask < FILL_FLOOR:
                        if tok not in thin_skip:
                            print(f"[{ts_str()}] [skip-floor] {coin} {m['side']} {m.get('dur','5m')} blended {eff_ask:.3f} < {FILL_FLOOR:.2f}")
                            thin_skip.add(tok)
                        continue
                    if (not fully) or walk_cost < MIN_STAKE:
                        if tok not in thin_skip:
                            print(f"[{ts_str()}] [skip-thin] {coin} {m['side']} {m.get('dur','5m')} fills only ${walk_cost:.0f}/${target:.0f}")
                            thin_skip.add(tok)
                        continue
                    sniped.add(tok)
                    pre_move, p_trend = _wick_snapshot(coin, op, leading_up, m.get("dur","5m"))
                    await fire(s, tok, m, eff_ask, walk_cost, move_bps, int(left), cur, walk_sh, cum_usdc, pre_move, p_trend)
                    continue
                size = target
                depth_usdc = ask_sz * ask
                if DYNAMIC_SIZE:
                    size = min(size, depth_usdc)
                elif REQUIRE_FILL and depth_usdc < size:
                    if tok not in thin_skip:
                        thin_skip.add(tok)
                    continue
                if size < MIN_STAKE:
                    if tok not in thin_skip:
                        thin_skip.add(tok)
                    continue
                sniped.add(tok)
                pre_move, p_trend = _wick_snapshot(coin, op, leading_up, m.get("dur","5m"))
                await fire(s, tok, m, ask, size, move_bps, int(left), cur, ask_sz, cum_usdc, pre_move, p_trend)
            await asyncio.sleep(BOOK_POLL_MS / 1000)

async def fire(session, tok, m, ask, size, move_bps, left, cur, ask_sz=0.0, cum_usdc=0.0, pre_window_move_bps=None, price_trend=None):
    coin, side, cond, slug = m["coin"], m["side"], m["cond"], m["slug"]
    fill_price, shares = ask, round(size/ask, 4)
    ask_usdc = round(ask_sz * ask, 2)
    would_fill = 1 if (ask_sz * ask) + 1e-6 >= size else 0
    c = db()
    cur_db = c.execute("""INSERT INTO positions(ts,coin,outcome,token_id,condition_id,slug,whale,
                 entry_price,size_usdc,shares,close_ts,status,mode,open_price,dec_price,
                 move_bps,secs_left,ask_shares,ask_usdc,would_fill,cum_usdc,
                 pre_window_move_bps,price_trend)
                 VALUES(?,?,?,?,?,?, 'SHADOW', ?,?,?,?, 'open', 'PAPER', ?,?,?,?,?,?,?,?,?,?)""",
              (datetime.now(timezone.utc).isoformat(), coin, side, tok, cond, slug,
               fill_price, size, shares, m["close_ts"], m["open_price"], cur,
               round(move_bps,1), left, round(ask_sz,2), ask_usdc, would_fill, cum_usdc,
               pre_window_move_bps, price_trend))
    pid = cur_db.lastrowid
    c.commit(); c.close()
    dur = m.get("dur", "5m")
    print(f"[{ts_str()}] SHADOW #{pid} {coin} {side} {dur} @ {fill_price:.3f} "
          f"${size:.2f} ({shares:.1f}sh) | {move_bps:+.1f}bps | {left}s left")

async def status_loop():
    while True:
        await asyncio.sleep(60)
        c = db()
        o = c.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
        tot, won, pnl = c.execute("SELECT COUNT(*), COALESCE(SUM(status='won'),0), COALESCE(SUM(pnl),0) FROM positions").fetchone()
        c.close()
        px = " ".join(f"{cc}={prices.get(cc,0):.2f}" for cc in COINS)
        print(f"[{ts_str()}] tracking {len(markets)} mkts | open {o} | total {tot} won {won} pnl ${pnl:+.2f} | {px}")

STATE_FILE = os.path.join(SD, "shadow_state.json")
async def state_writer():
    while True:
        try:
            t = now()
            for cc in COINS:
                pv = prices.get(cc)
                if pv:
                    price_hist.setdefault(cc, deque(maxlen=90)).append(round(pv, 2))
            payload = {"ts": t, "mode": "SHADOW", "tracking": len(markets), "prices": prices}
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, STATE_FILE)
        except Exception:
            pass
        await asyncio.sleep(5)

def banner():
    print("="*66)
    print(f"  GHOST PREDATOR SHADOW COLLECTOR — PAPER ONLY — DATA COLLECTION")
    print(f"  coins {COINS} | 5m window={SNIPE_WINDOW}s | 15m window={SNIPE_WINDOW_15M}s | min_fill={MIN_FILL_SECS}s")
    print(f"  ask {MIN_ASK}-{MAX_ASK} | move >={MIN_MOVE_BPS}bps | ${BASE_SIZE}/snipe | db {os.path.basename(DB)}")
    print("="*66)

async def prewarm_loop(): return
async def presign_loop(): return

async def paper_resolver_loop():
    """Resolve positions using Binance close price — same logic as main ghost_predator
    shadow_resolver_loop but operates on the main positions table."""
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                t_now = int(now())
                c = db()
                rows = c.execute("""SELECT id, coin, outcome, close_ts, open_price
                                    FROM positions
                                    WHERE status='open' AND close_ts < ?""",
                                 (t_now - 30,)).fetchall()
                c.close()
                for pid, coin, side, close_ts, open_px in rows:
                    sym = BINANCE_SYM.get(coin)
                    if not sym or not open_px:
                        continue
                    d = await jget(s, f"{BINANCE_REST}/api/v3/klines?symbol={sym}&interval=1m"
                                      f"&startTime={(close_ts-60)*1000}&limit=1")
                    if not d or not d[0]:
                        continue
                    close_px = float(d[0][4])
                    is_up = side.upper() in ("UP", "YES")
                    won = (close_px >= open_px) if is_up else (close_px < open_px)
                    status = 'won' if won else 'lost'
                    pnl = round((20.0 / open_px) * (1 - open_px), 4) if won else -20.0
                    c = db()
                    c.execute("UPDATE positions SET status=?, pnl=?, resolved_at=? WHERE id=?",
                              (status, pnl, datetime.now(timezone.utc).isoformat(), pid))
                    c.commit(); c.close()
                    print(f"[{ts_str()}] RESOLVE #{pid} {coin} {side} {'WIN' if won else 'LOSS'} | open={open_px:.4f} close={close_px:.4f} pnl={pnl:+.2f}")
            except Exception as ex:
                print(f"[{ts_str()}] [resolve-err] {ex}")
            await asyncio.sleep(30)

async def main():
    init_db(); banner()
    await asyncio.gather(binance_feed(), book_feed(), discovery(), snipe_loop(),
                         status_loop(), state_writer(), regime_loop(),
                         shadow_resolver_loop(), firstmover_reader(),
                         paper_resolver_loop())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[stopped]")
