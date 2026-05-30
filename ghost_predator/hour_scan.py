#!/usr/bin/env python3
"""
Ghost Predator - HOUR SCANNER.

For every 5m/15m window over a long lookback, measure whether the late-window
leader (price ~1 min before close vs window open) predicted the actual close,
tagged by UTC hour. Unbiased ground truth: tens of thousands of windows.

Stdlib only. Read-only. Writes to its OWN SQLite db (no risk to any trading db).

Usage:
    python3 hour_scan.py backfill 30     # fetch last 30 days, insert (idempotent)
    python3 hour_scan.py append          # fetch last ~2 days (for an hourly cron)
    python3 hour_scan.py report          # aggregate + print per-hour table
    python3 hour_scan.py                 # backfill 30 + report
"""
import os, sqlite3, json, urllib.request, time, sys
from datetime import datetime, timezone

SD       = os.path.dirname(os.path.abspath(__file__))
DB       = os.path.join(SD, "hour_scan.db")
SYMS     = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
MOVE_MIN = 2.0    # "real move" threshold (bps) for the filtered column


def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS hour_scan(
        window_ts INTEGER, coin TEXT, dur TEXT, hour INTEGER,
        move_bps REAL, abs_bps REAL, leader TEXT, actual TEXT, correct INTEGER,
        PRIMARY KEY(window_ts, coin, dur))""")
    return c


def fetch_1m(sym, start_ms, end_ms):
    out = []; s = start_ms
    while s < end_ms:
        url = ("https://api.binance.com/api/v3/klines?symbol=%s"
               "&interval=1m&startTime=%d&limit=1000" % (sym, s))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            k = json.load(urllib.request.urlopen(req, timeout=25))
        except Exception as e:
            print("  fetch err:", e); break
        if not k: break
        out.extend(k)
        if len(k) < 1000: break
        s = k[-1][0] + 60000
        time.sleep(0.05)
    return out


def scan_into_db(days):
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - int(days * 86400 * 1000)
    c = db()
    for coin, sym in SYMS.items():
        print("  fetching %s 1m klines (%g days)..." % (coin, days))
        kl = fetch_1m(sym, start_ms, now_ms)
        mm = {}
        for x in kl:
            mm[x[0] // 1000] = (float(x[1]), float(x[4]))   # sec -> (open, close)
        for dur, wlen in (("5m", 5), ("15m", 15)):
            span = wlen * 60
            for opents in [t for t in mm if t % span == 0]:
                decis_open = opents + (wlen - 2) * 60   # close ~1 min before end
                close_open = opents + (wlen - 1) * 60   # last candle close = window close
                if decis_open not in mm or close_open not in mm: continue
                open_px, decis_px, close_px = mm[opents][0], mm[decis_open][1], mm[close_open][1]
                if open_px <= 0 or close_px == open_px: continue
                move    = (decis_px - open_px) / open_px * 1e4
                leader  = "Up" if decis_px > open_px else ("Down" if decis_px < open_px else "Flat")
                if leader == "Flat": continue
                actual  = "Up" if close_px > open_px else "Down"
                correct = 1 if leader == actual else 0
                hour    = datetime.fromtimestamp(opents, timezone.utc).hour
                c.execute("INSERT OR IGNORE INTO hour_scan VALUES(?,?,?,?,?,?,?,?,?)",
                          (opents, coin, dur, hour, round(move, 3),
                           round(abs(move), 3), leader, actual, correct))
        c.commit()
    c.close()


def report():
    c   = db()
    tot = c.execute("SELECT COUNT(*) FROM hour_scan").fetchone()[0]
    if not tot:
        print("  no data yet"); c.close(); return
    ov  = c.execute("SELECT AVG(correct) FROM hour_scan").fetchone()[0]
    ov2 = c.execute("SELECT AVG(correct),COUNT(*) FROM hour_scan "
                    "WHERE abs_bps>=?", (MOVE_MIN,)).fetchone()
    print("HOUR SCAN - %d windows" % tot)
    print("overall acc %.1f%% | real moves (>=%.0fbps) %.1f%% (N=%d)"
          % (ov * 100, MOVE_MIN, (ov2[0] or 0) * 100, ov2[1]))
    for h in range(24):
        a = c.execute("SELECT COUNT(*),AVG(correct) FROM hour_scan WHERE hour=?", (h,)).fetchone()
        b = c.execute("SELECT COUNT(*),AVG(correct) FROM hour_scan "
                      "WHERE hour=? AND abs_bps>=?", (h, MOVE_MIN)).fetchone()
        if not a[0]: continue
        acc2 = (b[1] or 0) * 100
        print("  %02d:00  N=%-5d  acc_all=%4.1f%%   N>=2=%-5d  acc>=2=%4.1f%%  edge=%+.1f%%"
              % (h, a[0], (a[1] or 0) * 100, b[0], acc2, acc2 - 75.0))
    c.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "backfill":
        scan_into_db(float(sys.argv[2]) if len(sys.argv) > 2 else 30); report()
    elif cmd == "append":
        scan_into_db(2)
    elif cmd == "report":
        report()
    else:
        scan_into_db(30); report()
