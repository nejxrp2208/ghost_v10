#!/usr/bin/env python3
"""
GHOST PREDATOR — First-mover edge detector.

Replaces "grind a fixed rule" logic with a rolling realized-edge measurement.
Reads divergence radar (divergence.db); on the last N=20 resolved windows
where the favorite (PM higher-ask side) was priced in [MIN_FAV_ASK, MAX_FAV_ASK]:

    favorite      = side with the higher Polymarket ask (up vs down)
    won           = (actual close == favorite side)
    WR            = wins / N
    avg_price     = mean(favorite ask) over the N
    rolling_edge  = WR - avg_price

Signal:
    rolling_edge >= +8%  → GO     (fresh mispricing, fire real money)
    rolling_edge <  0%   → OUT    (decayed, paper-watch)
    in between           → WAIT   (marginal, paper-watch)
    not enough data      → WARMUP (default = OK to fire, falls back to existing gates)

Writes firstmover_state.json every POLL_SECS. Ghost predator reads this file
in its own async loop and routes fires to shadow when signal != GO/WARMUP.

Standalone, read-only on divergence.db. Stdlib only. PM2-managed.

Usage:
    python3 firstmover.py            # forever loop (PM2 process)
    python3 firstmover.py report     # one-shot status print
"""
import os, sqlite3, json, time, sys
from datetime import datetime, timezone

SD         = os.path.dirname(os.path.abspath(__file__))
DIV_DB     = os.path.join(SD, "divergence.db")
STATE_FILE = os.path.join(SD, "firstmover_state.json")


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

WINDOW        = int(os.getenv("FIRSTMOVER_WINDOW", "20"))
MIN_FAV_ASK   = float(os.getenv("FIRSTMOVER_MIN_FAV_ASK", "0.60"))
MAX_FAV_ASK   = float(os.getenv("FIRSTMOVER_MAX_FAV_ASK", "0.85"))
GO_THRESHOLD  = float(os.getenv("FIRSTMOVER_GO", "0.08"))     # WR-price ≥ +8% → GO
OUT_THRESHOLD = float(os.getenv("FIRSTMOVER_OUT", "0.00"))    # WR-price < 0% → OUT
POLL_SECS     = int(os.getenv("FIRSTMOVER_POLL_SECS", "60"))


def compute_signal():
    """Read last WINDOW resolved divergence windows where favorite ask is in band.
    Return dict: signal, wr, avg_price, rolling_edge, n, window."""
    try:
        c = sqlite3.connect(f"file:{DIV_DB}?mode=ro", uri=True)
    except Exception as e:
        return {"signal": "WARMUP", "wr": None, "avg_price": None,
                "rolling_edge": None, "n": 0, "window": WINDOW,
                "error": f"db open failed: {e}"}
    try:
        rows = c.execute("""
            SELECT actual, up_ask, down_ask, pm_leader
            FROM divergence
            WHERE status='done'
              AND MAX(up_ask, down_ask) >= ?
              AND MAX(up_ask, down_ask) <= ?
            ORDER BY window_ts DESC LIMIT ?
        """, (MIN_FAV_ASK, MAX_FAV_ASK, WINDOW)).fetchall()
    except Exception as e:
        c.close()
        return {"signal": "WARMUP", "wr": None, "avg_price": None,
                "rolling_edge": None, "n": 0, "window": WINDOW,
                "error": f"query failed: {e}"}
    c.close()

    if len(rows) < WINDOW:
        return {"signal": "WARMUP", "wr": None, "avg_price": None,
                "rolling_edge": None, "n": len(rows), "window": WINDOW}

    wins      = sum(1 for r in rows if r[0] == r[3])    # actual matched PM favorite
    avg_price = sum(max(r[1], r[2]) for r in rows) / len(rows)
    wr        = wins / len(rows)
    edge      = round(wr - avg_price, 4)

    if edge >= GO_THRESHOLD:
        sig = "GO"
    elif edge < OUT_THRESHOLD:
        sig = "OUT"
    else:
        sig = "WAIT"

    return {"signal": sig, "wr": round(wr, 4), "avg_price": round(avg_price, 4),
            "rolling_edge": edge, "n": len(rows), "window": WINDOW}


def write_state(s):
    s["ts"]             = time.time()
    s["ts_iso"]         = datetime.now(timezone.utc).isoformat()
    s["go_threshold"]   = GO_THRESHOLD
    s["out_threshold"]  = OUT_THRESHOLD
    s["min_fav_ask"]    = MIN_FAV_ASK
    s["max_fav_ask"]    = MAX_FAV_ASK
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(s, f)
    os.replace(tmp, STATE_FILE)


def report():
    s = compute_signal()
    print(f"\n==== FIRST-MOVER DETECTOR ====")
    print(f"window:    last {s['window']} resolved windows (favorite ask {MIN_FAV_ASK}-{MAX_FAV_ASK})")
    print(f"data:      n={s['n']}/{s['window']}")
    if s.get("error"):
        print(f"error:     {s['error']}")
    if s["wr"] is not None:
        print(f"WR:        {s['wr']*100:.1f}%")
        print(f"avg fav:   {s['avg_price']:.3f}")
        print(f"edge:      {s['rolling_edge']*100:+.1f}%")
    print(f"\nSIGNAL:    {s['signal']}")
    if s["signal"] == "GO":
        print("           → fresh mispricing, real bets allowed")
    elif s["signal"] == "OUT":
        print("           → edge decayed, paper-watch only")
    elif s["signal"] == "WAIT":
        print("           → marginal edge, paper-watch only")
    else:
        print("           → not enough data yet, defers to other gates")
    print()


def loop():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] firstmover live — "
          f"window={WINDOW}, band={MIN_FAV_ASK}-{MAX_FAV_ASK}, "
          f"GO>={GO_THRESHOLD*100:.0f}%, OUT<{OUT_THRESHOLD*100:.0f}%, "
          f"poll={POLL_SECS}s", flush=True)
    last_sig = None
    while True:
        try:
            s = compute_signal()
            write_state(s)
            if s["signal"] != last_sig:
                edge = s.get("rolling_edge")
                edge_s = f"{edge*100:+.1f}%" if edge is not None else "n/a"
                print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                      f"signal: {last_sig} → {s['signal']} "
                      f"(edge {edge_s} on n={s['n']})", flush=True)
                last_sig = s["signal"]
        except Exception as e:
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] err: {e}", flush=True)
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    else:
        loop()
