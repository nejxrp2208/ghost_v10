#!/usr/bin/env python3
"""
GHOST PREDATOR — LASTLAP: read-only last-minutes price-trace collector.

Watches every BTC/ETH/SOL/XRP up-down window from T-240s (5m) / T-480s (15m)
to the close and records, at sub-second cadence, how spot moves AGAINST the
price-to-beat (window open). After the close it stores the winner + summary
(leader flips, max excursion, end bps) so we can compare how price behaves
in windows the leader HOLDS vs windows it LOSES.

No trades, no writes to any other DB. Own DB: lastlap.db.

Run:    python3 lastlap.py            (collector — PM2: ghost-predator-lastlap)
Report: python3 lastlap.py report     (hold-rates per checkpoint, WIN vs LOSS profile)
"""
import os, sys, json, sqlite3, asyncio, time
from datetime import datetime, timezone

try:
    import aiohttp
except ImportError:
    sys.exit("needs aiohttp:  pip install aiohttp")

SD = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(SD, "lastlap.db")

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
def envf(k, d): return float(os.getenv(k) or d)
def envi(k, d): return int(os.getenv(k) or d)

COINS       = [c.strip().upper() for c in (os.getenv("LASTLAP_COINS") or "BTC,ETH,SOL,XRP").split(",") if c.strip()]
DURATIONS   = [d.strip() for d in (os.getenv("LASTLAP_DURATIONS") or "5m,15m").split(",") if d.strip()]
WATCH_5M    = envi("LASTLAP_WATCH_5M", "240")    # start watching a 5m window this many secs before close
WATCH_15M   = envi("LASTLAP_WATCH_15M", "480")   # start watching a 15m window this many secs before close
TICK_MS     = envi("LASTLAP_TICK_MS", "500")     # price sample cadence (500ms = 2 ticks/s)
ASK_SECS    = envi("LASTLAP_ASK_SECS", "10")     # PM best-ask sample cadence (REST, both sides)
DISCO_SECS  = envi("LASTLAP_DISCOVERY_SECS", "15")
PRICE_MAX_AGE = envf("PRICE_MAX_AGE", "3.0")

DUR_MAP     = {"5m": 300, "15m": 900}
BINANCE_SYM = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
               "XRP": "XRPUSDT", "BNB": "BNBUSDT"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
BINANCE_REST = "https://api.binance.com"

def watch_secs(dur): return WATCH_15M if dur == "15m" else WATCH_5M
def now(): return time.time()
def ts_str(): return datetime.now(timezone.utc).strftime("%H:%M:%S")

prices, price_ts = {}, {}
windows = {}          # slug -> {win_id, coin, dur, open_price, close_ts, tokens:{'UP':tok,'DOWN':tok}}
tick_buf = []         # buffered tick rows, flushed in batches

def db():
    c = sqlite3.connect(DB, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS windows(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE, coin TEXT, dur TEXT,
        close_ts INTEGER, open_price REAL,
        winner TEXT, close_price REAL, end_bps REAL,
        flips INTEGER, max_abs_bps REAL, ticks INTEGER,
        resolved_at TEXT, created TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ticks(
        win_id INTEGER, secs_left REAL, price REAL, move_bps REAL, leader TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ticks_win ON ticks(win_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS asks(
        win_id INTEGER, secs_left INTEGER, ask_up REAL, ask_dn REAL)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_asks_win ON asks(win_id)")
    c.commit(); c.close()

# ── http / feeds (same patterns as ghost_predator.py) ────────────────────────
async def jget(session, url, timeout=8):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        return None
    return None

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

async def binance_open(session, coin, start_ts):
    sym = BINANCE_SYM.get(coin)
    d = await jget(session, f"{BINANCE_REST}/api/v3/klines?symbol={sym}&interval=5m"
                            f"&startTime={start_ts*1000}&limit=1")
    if d and len(d) and len(d[0]) > 1:
        return float(d[0][1])
    return None

async def discovery():
    """Find windows by their predictable slug early enough to catch the full
    watch span (T-240/T-480). Registers each window in the DB immediately so
    ticks can reference its win_id."""
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                t = int(now())
                for coin in COINS:
                    cl = coin.lower()
                    for dur in DURATIONS:
                        ds = DUR_MAP.get(dur)
                        if not ds: continue
                        cur_start = (t // ds) * ds
                        for k in range(3):                    # current + next 2 windows
                            start = cur_start + k * ds
                            close_ts = start + ds
                            if close_ts <= t + 1: continue
                            slug = f"{cl}-updown-{dur}-{start}"
                            if slug in windows: continue
                            mk = await jget(s, f"{GAMMA}/markets?slug={slug}")
                            if not mk: continue               # window not created yet
                            m = mk[0]
                            toks = m.get("clobTokenIds"); outs = m.get("outcomes")
                            if isinstance(toks, str): toks = json.loads(toks)
                            if isinstance(outs, str): outs = json.loads(outs)
                            if not toks or not outs: continue
                            open_px = await binance_open(s, coin, start)
                            c = db()
                            c.execute("""INSERT OR IGNORE INTO windows(slug,coin,dur,close_ts,open_price,created)
                                         VALUES(?,?,?,?,?,?)""",
                                      (slug, coin, dur, close_ts, open_px,
                                       datetime.now(timezone.utc).isoformat()))
                            wid = c.execute("SELECT id FROM windows WHERE slug=?", (slug,)).fetchone()[0]
                            c.commit(); c.close()
                            windows[slug] = {"win_id": wid, "coin": coin, "dur": dur,
                                             "open_price": open_px, "close_ts": close_ts,
                                             "tokens": {str(o).upper(): tk for tk, o in zip(toks, outs)}}
                # self-heal missing open prices (transient klines failure)
                for w in list(windows.values()):
                    if w["open_price"] is None:
                        ds = DUR_MAP.get(w["dur"], 300)
                        op = await binance_open(s, w["coin"], w["close_ts"] - ds)
                        if op:
                            w["open_price"] = op
                            c = db(); c.execute("UPDATE windows SET open_price=? WHERE id=?",
                                                (op, w["win_id"])); c.commit(); c.close()
            except Exception as e:
                print(f"[{ts_str()}] discovery err: {e}")
            await asyncio.sleep(DISCO_SECS)

# ── tick sampler (the core) ──────────────────────────────────────────────────
async def tick_loop():
    last_flush = now()
    while True:
        t = now()
        for w in list(windows.values()):
            left = w["close_ts"] - t
            if left < 0 or left > watch_secs(w["dur"]): continue
            op = w["open_price"]; cur = prices.get(w["coin"])
            if op is None or cur is None: continue
            if t - price_ts.get(w["coin"], 0) > PRICE_MAX_AGE: continue   # stale feed — no fake ticks
            bps = (cur - op) / op * 1e4
            tick_buf.append((w["win_id"], round(left, 2), cur, round(bps, 3),
                             "UP" if cur >= op else "DOWN"))
        if tick_buf and (len(tick_buf) >= 100 or t - last_flush >= 2.0):
            c = db()
            c.executemany("INSERT INTO ticks(win_id,secs_left,price,move_bps,leader) VALUES(?,?,?,?,?)",
                          tick_buf)
            c.commit(); c.close()
            tick_buf.clear(); last_flush = t
        await asyncio.sleep(TICK_MS / 1000)

async def ask_loop():
    """Sample PM best ask of BOTH sides every ASK_SECS while in the watch span —
    shows how PM's pricing tracks (or lags) the spot trajectory."""
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                t = now()
                for w in list(windows.values()):
                    left = w["close_ts"] - t
                    if left < 2 or left > watch_secs(w["dur"]): continue
                    a = {}
                    for side, tok in w["tokens"].items():
                        b = await jget(s, f"{CLOB}/book?token_id={tok}", timeout=3)
                        try:
                            asks = [float(x["price"]) for x in (b.get("asks") or [])
                                    if float(x.get("size", 0)) > 0]
                            a[side] = min(asks) if asks else None
                        except Exception:
                            a[side] = None
                    c = db()
                    c.execute("INSERT INTO asks(win_id,secs_left,ask_up,ask_dn) VALUES(?,?,?,?)",
                              (w["win_id"], int(left), a.get("UP"), a.get("DOWN")))
                    c.commit(); c.close()
            except Exception as e:
                print(f"[{ts_str()}] ask err: {e}")
            await asyncio.sleep(ASK_SECS)

# ── finalize: winner + per-window summary ─────────────────────────────────────
async def finalize_loop():
    while True:
        try:
            t = now()
            for slug, w in list(windows.items()):
                if w["close_ts"] > t - 3: continue          # give the last ticks time to land
                c = db()
                rows = c.execute("""SELECT secs_left, price, move_bps, leader FROM ticks
                                    WHERE win_id=? ORDER BY secs_left DESC""", (w["win_id"],)).fetchall()
                flips, max_abs, prev = 0, 0.0, None
                for _, _, bps, ld in rows:
                    if prev is not None and ld != prev: flips += 1
                    prev = ld
                    max_abs = max(max_abs, abs(bps))
                if rows:
                    close_px, end_bps = rows[-1][1], rows[-1][2]   # tick nearest the close
                    winner = "Up" if close_px >= w["open_price"] else "Down"
                else:
                    close_px = end_bps = winner = None             # discovered too late — no trace
                c.execute("""UPDATE windows SET winner=?, close_price=?, end_bps=?,
                             flips=?, max_abs_bps=?, ticks=?, resolved_at=? WHERE id=?""",
                          (winner, close_px, end_bps, flips, round(max_abs, 3), len(rows),
                           datetime.now(timezone.utc).isoformat(), w["win_id"]))
                c.commit(); c.close()
                if rows:
                    print(f"[{ts_str()}] [lap-done] {slug} winner={winner} end={end_bps:+.1f}bps "
                          f"flips={flips} max={max_abs:.1f}bps ticks={len(rows)}")
                windows.pop(slug, None)
        except Exception as e:
            print(f"[{ts_str()}] finalize err: {e}")
        await asyncio.sleep(5)

async def status_loop():
    while True:
        await asyncio.sleep(30)
        t = now()
        inw = sum(1 for w in windows.values() if 0 < w["close_ts"] - t <= watch_secs(w["dur"]))
        c = db()
        nw, nt = c.execute("SELECT (SELECT COUNT(*) FROM windows), (SELECT COUNT(*) FROM ticks)").fetchone()
        c.close()
        px = " ".join(f"{k}={prices.get(k,0):.4f}".rstrip("0").rstrip(".") if prices.get(k,0) < 10
                      else f"{k}={prices.get(k,0):.0f}" for k in COINS)
        print(f"[{ts_str()}] watching {inw}/{len(windows)} windows | db: {nw} windows {nt} ticks | {px}")

def banner():
    print("=" * 66)
    print("  GHOST PREDATOR — LASTLAP price-trace collector (read-only)")
    print(f"  coins {COINS} | durs {DURATIONS} | watch 5m=T-{WATCH_5M}s 15m=T-{WATCH_15M}s")
    print(f"  tick every {TICK_MS}ms | PM asks every {ASK_SECS}s | db {os.path.basename(DB)}")
    print("=" * 66)

async def main():
    init_db(); banner()
    await asyncio.gather(binance_feed(), discovery(), tick_loop(), ask_loop(),
                         finalize_loop(), status_loop())

# ── report ────────────────────────────────────────────────────────────────────
CHECKPOINTS = {"5m": [240, 180, 120, 90, 60, 30, 15, 5],
               "15m": [480, 360, 240, 180, 120, 60, 30, 15, 5]}

def report():
    c = db()
    wins = c.execute("""SELECT id, coin, dur, winner, end_bps, flips, max_abs_bps, ticks
                        FROM windows WHERE winner IS NOT NULL AND ticks > 0""").fetchall()
    if not wins:
        print("no resolved windows yet"); return
    print(f"LASTLAP report — {len(wins)} resolved windows\n")

    def leader_at(wid, cp):
        r = c.execute("""SELECT leader, move_bps FROM ticks WHERE win_id=?
                         ORDER BY ABS(secs_left - ?) LIMIT 1""", (wid, cp)).fetchone()
        return r if r else (None, None)

    # 1) hold-rate per checkpoint, per coin x dur
    print("P(leader at T-x wins) — per coin x dur")
    for dur in DURATIONS:
        cps = CHECKPOINTS.get(dur, [])
        for coin in COINS:
            grp = [w for w in wins if w[1] == coin and w[2] == dur]
            if len(grp) < 5: continue
            cells = []
            for cp in cps:
                n = h = 0
                for w in grp:
                    ld, _ = leader_at(w[0], cp)
                    if ld is None: continue
                    n += 1
                    if (ld == "UP") == (w[3] == "Up"): h += 1
                cells.append(f"T-{cp}:{(h/n*100):4.0f}%" if n else f"T-{cp}:   -")
            print(f"  {coin} {dur:<4} n={len(grp):<5} " + "  ".join(cells))
    # 2) WIN vs LOSS profile (from the T-30 leader's perspective)
    print("\nWIN vs LOSS profile (leader at T-30 holds or falls)")
    for dur in DURATIONS:
        hold, fall = [], []
        for w in wins:
            if w[2] != dur: continue
            ld, bps = leader_at(w[0], 30)
            if ld is None: continue
            (hold if (ld == "UP") == (w[3] == "Up") else fall).append((w, abs(bps or 0)))
        for name, grp in (("HOLD", hold), ("FALL", fall)):
            if not grp: continue
            n = len(grp)
            af = sum(w[5] for w, _ in grp) / n
            am = sum(w[6] for w, _ in grp) / n
            ab = sum(b for _, b in grp) / n
            ae = sum(abs(w[4] or 0) for w, _ in grp) / n
            print(f"  {dur:<4} {name}: n={n:<5} avg flips={af:4.1f}  avg max|bps|={am:5.1f}  "
                  f"avg |bps@T-30|={ab:4.1f}  avg |end bps|={ae:4.1f}")
    # 3) hold rate by |bps| bucket at T-60
    print("\nP(hold) by |move_bps| at T-60")
    buckets = [(0, 1), (1, 2), (2, 3), (3, 5), (5, 8), (8, 999)]
    for dur in DURATIONS:
        cells = []
        for lo, hi in buckets:
            n = h = 0
            for w in wins:
                if w[2] != dur: continue
                ld, bps = leader_at(w[0], 60)
                if ld is None or bps is None: continue
                if not (lo <= abs(bps) < hi): continue
                n += 1
                if (ld == "UP") == (w[3] == "Up"): h += 1
            cells.append(f"{lo}-{hi if hi < 999 else '+'}: {(h/n*100):3.0f}% (n={n})" if n else f"{lo}-{hi if hi < 999 else '+'}: -")
        print(f"  {dur:<4} " + "  ".join(cells))
    c.close()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("\n[stopped]")
