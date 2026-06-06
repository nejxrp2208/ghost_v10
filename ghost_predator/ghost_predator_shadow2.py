#!/usr/bin/env python3
"""
GHOST PREDATOR SHADOW 2 — Contra-Binance divergence collector (paper only).

Strategy (from divergence analysis, 2026-06-04):
  When PM prices one side at 0.75-0.81 AND Binance leader disagrees →
  PM has right 88% of the time (n=25 BTC 5m, confirmed).

  diverged=1 + PM ask 0.75-0.81  →  fire WITH PM (contra Binance)
  diverged=0 or PM ask outside band  →  skip

No .env needed — all config hardcoded.
No real money ever — PAPER = True hardcoded.
No Telegram. Separate DB: shadow_predator2.db.
"""
import os, sys, time, json, sqlite3, asyncio
from collections import deque
from datetime import datetime, timezone, timedelta

try:
    import aiohttp
except ImportError:
    sys.exit("needs aiohttp:  pip install aiohttp")

SD = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(SD, "shadow_predator2.db")

# ── config (hardcoded) ────────────────────────────────────────────────────────
PAPER           = True
START_BAL       = 1000.0
COINS           = ["BTC", "ETH", "XRP", "SOL"]
DUR_MAP         = {"5m":300, "15m":900, "30m":1800, "1h":3600, "4h":14400, "1d":86400}
DURATIONS       = {"5m"}            # 5m only — strongest divergence signal
SNIPE_WINDOW    = 28                # same timing as main predator
MIN_FILL_SECS   = 5
MIN_MOVE_BPS    = 1.0               # Binance must show decisive move
PM_FAV_MIN      = 0.75              # PM favorite ask lower bound
PM_FAV_MAX      = 0.81              # PM favorite ask upper bound
BASE_SIZE       = 20.0
MAX_PER_MARKET  = 20.0
MIN_STAKE       = 1.0
FILL_FLOOR      = 0.50
WALK_FILL       = True
BOOK_POLL_MS    = 100
USE_WSS_BOOK    = True
DISCOVERY_SECS  = 20
PRICE_MAX_AGE   = 3.0
TG_TOKEN        = ""
TG_CHAT         = ""

BINANCE_SYM = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
               "XRP": "XRPUSDT", "BNB": "BNBUSDT"}
GAMMA        = "https://gamma-api.polymarket.com"
CLOB         = "https://clob.polymarket.com"
CLOB_WSS     = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_REST = "https://api.binance.com"

# ── shared state ──────────────────────────────────────────────────────────────
prices      = {}
price_ts    = {}
markets     = {}        # token_id -> {coin, side, cond, slug, open_price, close_ts, dur}
sniped      = set()     # slugs already fired this window
thin_skip   = set()
price_hist  = {}
book_ms     = {"last": None}
books       = {}
book_conn_ts = {"last": 0}
book_src    = {"last": "-"}
WSS_FRESH_SECS = 3.0
def ts_str(): return datetime.now().strftime("%H:%M:%S")
def now():    return time.time()

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
        ask_shares REAL, ask_usdc REAL, would_fill INTEGER, cum_usdc REAL,
        pre_window_move_bps REAL, price_trend REAL,
        pm_fav_ask REAL, binance_leader TEXT)""")
    c.commit(); c.close()

def market_exposure(cond):
    c = db()
    r = c.execute("SELECT COALESCE(SUM(size_usdc),0) FROM positions WHERE condition_id=?", (cond,)).fetchone()[0]
    c.close(); return r or 0.0

# ── http ──────────────────────────────────────────────────────────────────────
async def jget(session, url, timeout=8):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        return None
    return None

def tg(msg): pass

# ── Binance price feed ────────────────────────────────────────────────────────
async def binance_feed():
    streams = "/".join(f"{BINANCE_SYM[c].lower()}@trade" for c in COINS if c in BINANCE_SYM)
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    rev = {v: k for k, v in BINANCE_SYM.items()}
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(url, heartbeat=20) as ws:
                    print(f"[{ts_str()}] binance feed connected")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(msg.data).get("data", {})
                            sym = d.get("s"); p = d.get("p")
                            if sym in rev and p:
                                prices[rev[sym]] = float(p)
                                price_ts[rev[sym]] = now()
        except Exception as e:
            print(f"[{ts_str()}] binance reconnect: {e}")
            await asyncio.sleep(2)

# ── Polymarket book feed (WSS) ────────────────────────────────────────────────
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
                    print(f"[{ts_str()}] book feed connected: {len(toks)} tokens")
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

# ── ask ───────────────────────────────────────────────────────────────────────
async def best_ask(session, token):
    if USE_WSS_BOOK:
        wa = wss_best_ask(token)
        if wa is not None:
            return wa
    b = await jget(session, f"{CLOB}/book?token_id={token}", timeout=2)
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

# ── snipe loop — CONTRA BINANCE ───────────────────────────────────────────────
async def snipe_loop():
    async with aiohttp.ClientSession() as s:
        while True:
            t = now()
            # Group tokens by slug → need both Up and Down asks
            by_slug = {}
            for tok, m in list(markets.items()):
                by_slug.setdefault(m["slug"], {})[m["side"].upper()] = (tok, m)

            for slug, sides in by_slug.items():
                if "UP" not in sides or "DOWN" not in sides:
                    continue
                up_tok, up_m = sides["UP"]
                dn_tok, dn_m = sides["DOWN"]
                m = up_m

                left = m["close_ts"] - t
                if left > SNIPE_WINDOW or left < MIN_FILL_SECS:
                    continue
                if slug in sniped:
                    continue

                coin = m["coin"]; op = m["open_price"]; cur = prices.get(coin)
                if op is None or cur is None:
                    continue
                if now() - price_ts.get(coin, 0) > PRICE_MAX_AGE:
                    continue

                # Binance leader
                move_bps = (cur - op) / op * 1e4
                if abs(move_bps) < MIN_MOVE_BPS:
                    continue
                binance_up = cur >= op

                # Get asks for both sides
                up_ask, up_sz = await best_ask(s, up_tok)
                dn_ask, dn_sz = await best_ask(s, dn_tok)
                if up_ask is None or dn_ask is None:
                    continue

                # PM favorite = side with HIGHER ask (PM thinks more likely to win)
                if up_ask >= dn_ask:
                    pm_fav_side = "UP"; pm_fav_tok = up_tok
                    pm_fav_ask = up_ask; pm_fav_sz = up_sz
                else:
                    pm_fav_side = "DOWN"; pm_fav_tok = dn_tok
                    pm_fav_ask = dn_ask; pm_fav_sz = dn_sz

                # Divergence: PM favorite ≠ Binance leader
                pm_fav_up = (pm_fav_side == "UP")
                if pm_fav_up == binance_up:
                    continue    # PM agrees with Binance → skip

                # PM favorite ask must be in 0.75-0.81
                if not (PM_FAV_MIN <= pm_fav_ask <= PM_FAV_MAX):
                    continue

                # Exposure check
                exp = market_exposure(m["cond"])
                if exp >= MAX_PER_MARKET:
                    continue
                target = min(BASE_SIZE, MAX_PER_MARKET - exp)

                # Walk fill on PM favorite token
                cum_usdc = round(sum(p * sz for p, sz in books.get(pm_fav_tok, {}).items() if p <= PM_FAV_MAX), 2)
                eff_ask, walk_sh, walk_cost, fully = walk_book_fill(books.get(pm_fav_tok, {}), target, PM_FAV_MAX)
                if eff_ask is None:
                    continue
                if FILL_FLOOR > 0 and eff_ask < FILL_FLOOR:
                    continue
                if (not fully) or walk_cost < MIN_STAKE:
                    if slug not in thin_skip:
                        print(f"[{ts_str()}] [skip-thin] {coin} {pm_fav_side} fills only ${walk_cost:.0f}/${target:.0f}")
                        thin_skip.add(slug)
                    continue

                sniped.add(slug)
                binance_leader = "UP" if binance_up else "DOWN"
                await fire(s, pm_fav_tok, m, eff_ask, walk_cost, move_bps, int(left),
                           cur, walk_sh, cum_usdc, pm_fav_ask, pm_fav_side, binance_leader)

            await asyncio.sleep(BOOK_POLL_MS / 1000)

async def fire(session, tok, m, ask, size, move_bps, left, cur,
               ask_sz=0.0, cum_usdc=0.0, pm_fav_ask=0.0, pm_fav_side="UP", binance_leader="UP"):
    coin, cond, slug = m["coin"], m["cond"], m["slug"]
    fill_price = ask; shares = round(size / ask, 4)
    ask_usdc = round(ask_sz * ask, 2)
    would_fill = 1 if ask_sz * ask + 1e-6 >= size else 0
    c = db()
    cur_db = c.execute("""INSERT INTO positions(ts,coin,outcome,token_id,condition_id,slug,whale,
                 entry_price,size_usdc,shares,close_ts,status,mode,open_price,dec_price,
                 move_bps,secs_left,ask_shares,ask_usdc,would_fill,cum_usdc,
                 pre_window_move_bps,price_trend,pm_fav_ask,binance_leader)
                 VALUES(?,?,?,?,?,?,'SHADOW2',?,?,?,?,'open','PAPER',?,?,?,?,?,?,?,?,?,?,?,?)""",
              (datetime.now(timezone.utc).isoformat(), coin, pm_fav_side, tok, cond, slug,
               fill_price, size, shares, m["close_ts"], m["open_price"], cur,
               round(move_bps,1), left, round(ask_sz,2), ask_usdc, would_fill, cum_usdc,
               None, None, round(pm_fav_ask,4), binance_leader))
    pid = cur_db.lastrowid
    c.commit(); c.close()
    dur = m.get("dur","5m")
    print(f"[{ts_str()}] SHADOW2 #{pid} {coin} PM:{pm_fav_side} @ {fill_price:.3f} "
          f"${size:.2f} | BIN:{binance_leader} DIVERGED | {move_bps:+.1f}bps | {left}s")

# ── status ────────────────────────────────────────────────────────────────────
async def status_loop():
    while True:
        await asyncio.sleep(60)
        c = db()
        o = c.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
        tot, won, pnl = c.execute("SELECT COUNT(*), COALESCE(SUM(status='won'),0), COALESCE(SUM(pnl),0) FROM positions").fetchone()
        c.close()
        px = " ".join(f"{cc}={prices.get(cc,0):.2f}" for cc in COINS)
        print(f"[{ts_str()}] tracking {len(markets)} | open {o} | total {tot} won {won} pnl ${pnl:+.2f} | {px}")

def banner():
    print("="*66)
    print(f"  GHOST PREDATOR SHADOW 2 — CONTRA-BINANCE DIVERGENCE COLLECTOR")
    print(f"  coins {COINS} | 5m window={SNIPE_WINDOW}s | PM ask {PM_FAV_MIN}-{PM_FAV_MAX}")
    print(f"  strategy: fire WITH PM when PM diverges from Binance | db {os.path.basename(DB)}")
    print("="*66)

async def paper_resolver_loop():
    """Resolve positions using Polymarket on-chain TOKEN WINNER — same logic as
    main resolver.py. 100% accurate (settles on what PM actually paid out).
    Uses public CLOB API — no credentials, no env needed."""
    CLOSE_BUFFER = 20
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                t_now = int(now())
                c = db()
                rows = c.execute("""SELECT id, coin, outcome, token_id, condition_id,
                                           size_usdc, shares, close_ts, status
                                    FROM positions WHERE status='open'""").fetchall()
                c.close()
                for pid, coin, side, token, cond, sz, shares, close_ts, status in rows:
                    if close_ts and t_now < close_ts + CLOSE_BUFFER:
                        continue
                    m = await jget(s, f"{CLOB}/markets/{cond}", timeout=12)
                    if not m or not m.get("tokens"):
                        continue
                    toks = m["tokens"]
                    ids = {t.get("token_id") for t in toks}
                    if ids and token not in ids:
                        continue
                    w = next((t for t in toks if t.get("winner")), None)
                    if w is None:
                        continue
                    wtok = w.get("token_id")
                    won = (wtok == token)
                    pnl = round(shares - sz, 2) if won else round(-sz, 2)
                    new_status = "won" if won else "lost"
                    c = db()
                    c.execute("UPDATE positions SET status=?, pnl=?, resolved_at=?, pm_confirmed=1 WHERE id=?",
                              (new_status, pnl, datetime.now(timezone.utc).isoformat(), pid))
                    c.commit(); c.close()
                    print(f"[{ts_str()}] RESOLVE2 #{pid} {coin} {side} {'WIN' if won else 'LOSS'} PM-confirmed | pnl={pnl:+.2f}")
            except Exception as ex:
                print(f"[{ts_str()}] [resolve-err] {ex}")
            await asyncio.sleep(20)

async def main():
    init_db(); banner()
    await asyncio.gather(binance_feed(), book_feed(), discovery(), snipe_loop(),
                         status_loop(), paper_resolver_loop())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[stopped]")
