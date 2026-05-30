#!/usr/bin/env python3
"""
Ghost Predator - DIVERGENCE COLLECTOR ("reversal radar").

In the final seconds (~T-11s) of every BTC/ETH 5m & 15m Up/Down window, snapshot:
  - Binance SPOT vs window open (which side is winning, and by how many bps)
  - Polymarket ODDS for Up & Down (best ask = what the market is pricing)

Then after the window closes, resolve the actual outcome from Binance and flag:
  - reversed : the side leading on spot at T-11 LOST (a final-seconds reversal)
  - diverged : Polymarket already priced the OTHER side as favorite at T-11
               (the whale's footprint: PM odds ripping away from spot before the flip)

Goal: measure how often these reversals fire, and whether the PM-vs-spot
divergence sees them coming. Standalone, read-only, its own SQLite db. No keys.

Usage:
    python3 divergence_scan.py          # run the collector loop (forever)
    python3 divergence_scan.py report   # print stats from collected data
"""
import os, sqlite3, json, urllib.request, time, sys
from datetime import datetime, timezone

SD    = os.path.dirname(os.path.abspath(__file__))
DB    = os.path.join(SD, "divergence.db")
COINS = {"btc": "BTCUSDT", "eth": "ETHUSDT"}
DURS  = {"5m": 300, "15m": 900}
SNAP_HI, SNAP_LO = 12, 3   # snapshot when secs_left in [SNAP_LO, SNAP_HI] (~T-11)


def get(url, t=12):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=t))


def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS divergence(
        window_ts INTEGER, coin TEXT, dur TEXT, snap_secs_left INTEGER,
        open_px REAL, spot_px REAL, spot_bps REAL,
        up_ask REAL, down_ask REAL, spot_leader TEXT, pm_leader TEXT, diverged INTEGER,
        actual TEXT, reversed INTEGER, status TEXT DEFAULT 'open',
        PRIMARY KEY(window_ts, coin, dur))""")
    return c


_tok = {}
def tokens(coin, dur, ws):
    slug = "%s-updown-%s-%d" % (coin, dur, ws)
    if slug in _tok: return _tok[slug]
    try:
        g = get("https://gamma-api.polymarket.com/markets?slug=%s" % slug)
    except Exception:
        return None
    m = g[0] if isinstance(g, list) and g else (g if isinstance(g, dict) else None)
    if not m: return None
    tl = m.get("clobTokenIds"); outs = m.get("outcomes")
    tl   = json.loads(tl)   if isinstance(tl, str)   else tl
    outs = json.loads(outs) if isinstance(outs, str) else outs
    if not tl or not outs or len(tl) < 2: return None
    d = {outs[i]: tl[i] for i in range(min(len(tl), len(outs)))}
    res = (d.get("Up"), d.get("Down"))
    if all(res): _tok[slug] = res; return res
    return None


def best_ask(token):
    try:
        bk = get("https://clob.polymarket.com/book?token_id=%s" % token, 8)
        asks = [float(a["price"]) for a in bk.get("asks", [])]
        return min(asks) if asks else None
    except Exception:
        return None


def spot(sym):
    try:
        return float(get("https://api.binance.com/api/v3/ticker/price?symbol=%s" % sym, 8)["price"])
    except Exception:
        return None


def kline_px(sym, ts, field):   # field 1=open, 4=close of the 1m candle at ts
    try:
        k = get("https://api.binance.com/api/v3/klines?symbol=%s&interval=1m&startTime=%d&limit=1"
                % (sym, ts * 1000), 8)
        return float(k[0][field]) if k else None
    except Exception:
        return None


def snap(c, coin, sym, dur, ws, secs_left):
    if c.execute("SELECT 1 FROM divergence WHERE window_ts=? AND coin=? AND dur=?",
                 (ws, coin, dur)).fetchone():
        return
    tk = tokens(coin, dur, ws)
    if not tk: return
    up_ask, dn_ask = best_ask(tk[0]), best_ask(tk[1])
    sp, op = spot(sym), kline_px(sym, ws, 1)
    if up_ask is None or dn_ask is None or not sp or not op: return
    spot_bps    = (sp - op) / op * 1e4
    spot_leader = "Up" if sp > op else "Down"
    pm_leader   = "Up" if up_ask > dn_ask else "Down"   # higher price = PM favorite
    diverged    = 1 if spot_leader != pm_leader else 0
    c.execute("""INSERT OR IGNORE INTO divergence(window_ts,coin,dur,snap_secs_left,open_px,
                 spot_px,spot_bps,up_ask,down_ask,spot_leader,pm_leader,diverged)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
              (ws, coin, dur, secs_left, op, sp, round(spot_bps, 2), up_ask, dn_ask,
               spot_leader, pm_leader, diverged))
    c.commit()
    tag = " *** DIVERGENCE: PM favors %s, spot favors %s ***" % (pm_leader, spot_leader) if diverged else ""
    print("[%s] %s %s %ds left | spot %+.1fbps(%s) | PM up=%.2f dn=%.2f(%s)%s" % (
        datetime.now(timezone.utc).strftime("%H:%M:%S"), coin, dur, secs_left,
        spot_bps, spot_leader, up_ask, dn_ask, pm_leader, tag), flush=True)


def resolve(c):
    nowt = int(time.time())
    for ws, coin, dur, op, sl in c.execute(
            "SELECT window_ts,coin,dur,open_px,spot_leader FROM divergence WHERE status='open'").fetchall():
        span = DURS[dur]
        if ws + span > nowt - 75: continue          # wait until the close candle is final
        cl = kline_px(COINS[coin], ws + span - 60, 4)
        if not cl: continue
        actual = "Up" if cl > op else ("Down" if cl < op else "Flat")
        if actual == "Flat":
            c.execute("UPDATE divergence SET status='push' WHERE window_ts=? AND coin=? AND dur=?",
                      (ws, coin, dur)); continue
        rev = 1 if sl != actual else 0
        c.execute("UPDATE divergence SET status='done', actual=?, reversed=? WHERE window_ts=? AND coin=? AND dur=?",
                  (actual, rev, ws, coin, dur))
        if rev:
            print("[%s] RESOLVED reversal: %s %s ws=%d  spot-leader %s -> actual %s" % (
                datetime.now(timezone.utc).strftime("%H:%M:%S"), coin, dur, ws, sl, actual), flush=True)
    c.commit()


def report():
    c = db()
    done = c.execute("SELECT COUNT(*) FROM divergence WHERE status='done'").fetchone()[0]
    opn  = c.execute("SELECT COUNT(*) FROM divergence WHERE status='open'").fetchone()[0]
    print("\n==== DIVERGENCE RADAR ====")
    print("snapshots: %d done, %d awaiting resolution" % (done, opn))
    if not done:
        print("(no resolved windows yet - collector needs to run through some closes)")
        c.close(); return
    rev = c.execute("SELECT COUNT(*) FROM divergence WHERE status='done' AND reversed=1").fetchone()[0]
    print("overall final-seconds reversal rate: %.1f%%  (%d/%d)" % (rev / done * 100, rev, done))
    print("\n  does PM-vs-spot divergence PREDICT the reversal?")
    for d, lab in ((1, "PM DIVERGED from spot"), (0, "PM agreed with spot")):
        n = c.execute("SELECT COUNT(*) FROM divergence WHERE status='done' AND diverged=?", (d,)).fetchone()[0]
        r = c.execute("SELECT COUNT(*) FROM divergence WHERE status='done' AND diverged=? AND reversed=1",
                      (d,)).fetchone()[0]
        print("    %-22s  N=%-4d  reversed %.1f%%  (%d)" % (lab, n, (r / n * 100 if n else 0), r))
    print("\n  by coin/dur:")
    for row in c.execute("""SELECT coin,dur,COUNT(*),SUM(reversed),SUM(diverged)
                            FROM divergence WHERE status='done' GROUP BY coin,dur"""):
        co, du, n, rv, dv = row
        print("    %s %-3s  N=%-4d  reversed %.1f%%  diverged %.1f%%" % (
            co, du, n, (rv or 0) / n * 100, (dv or 0) / n * 100))
    c.close()


def loop():
    c = db()
    print("[%s] divergence collector live - snapshotting ~T-11s of every BTC/ETH 5m & 15m window"
          % datetime.now(timezone.utc).strftime("%H:%M:%S"), flush=True)
    i = 0
    while True:
        try:
            nowt = int(time.time())
            for coin, sym in COINS.items():
                for dur, span in DURS.items():
                    ws = nowt - nowt % span
                    secs_left = (ws + span) - nowt
                    if SNAP_LO <= secs_left <= SNAP_HI:
                        snap(c, coin, sym, dur, ws, secs_left)
            i += 1
            if i % 10 == 0:           # resolve every ~20s
                resolve(c)
        except Exception as e:
            print("loop err:", e, flush=True)
        time.sleep(2)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    else:
        loop()
