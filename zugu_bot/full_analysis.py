#!/usr/bin/env python3
"""
full_analysis.py  —  Comprehensive trade performance analysis for zugubotv3
Combines DB data + velocity/volume CSV into a full report.
"""

import sqlite3, csv, sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

DB_PATH   = "/root/zugubotv3/crypto_ghost_PAPER.db"
CSV_PATH  = "/root/zugubotv3/velocity_volume_analysis.csv"
OUT_PATH  = "/root/zugubotv3/full_analysis_report.txt"

# ── Load data ─────────────────────────────────────────────────────────────────

def load_data():
    # Load CSV (has velocity + volume)
    csv_rows = {}
    try:
        with open(CSV_PATH, newline="") as f:
            for r in csv.DictReader(f):
                csv_rows[int(r["id"])] = r
    except FileNotFoundError:
        print("CSV not found — run velocity_volume_analysis.py first")
        sys.exit(1)

    # Load DB
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT id, ts, coin, entry_price, pnl, pm_result, outcome,
               price_start, price_end, size_usdc
        FROM trades
        WHERE pm_result IN ('won','lost')
        ORDER BY ts
    """)
    trades = []
    for row in cur.fetchall():
        d = dict(row)
        d["ts_dt"] = datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        cv = csv_rows.get(d["id"], {})
        # velocity
        for k in ["vel_10s_pct","vel_30s_pct","vel_20_10_pct","abs_vel_10s"]:
            val = cv.get(k, "")
            d[k] = float(val) if val != "" else None
        # volume
        for k in ["vol_5m","vol_15m","vol_1h"]:
            val = cv.get(k, "")
            d[k] = float(val) if val != "" else None
        trades.append(d)
    con.close()
    return trades

# ── Formatting helpers ─────────────────────────────────────────────────────────

SEP  = "─" * 70
SEP2 = "═" * 70

def hdr(title):
    return f"\n{SEP2}\n  {title}\n{SEP2}"

def row_fmt(label, n, wr, avg_pnl, total_pnl, extra=""):
    return f"  {label:<22} {n:>5}   {wr:>6.1f}%   {avg_pnl:>+7.3f}   {total_pnl:>+8.2f}  {extra}"

def tbl_header():
    return f"  {'Category':<22} {'N':>5}   {'WinRate':>7}   {'AvgPnL':>7}   {'TotalPnL':>8}"

def tbl_sep():
    return f"  {SEP}"

def stats(grp):
    if not grp: return 0, 0.0, 0.0, 0.0
    n   = len(grp)
    wins = sum(1 for r in grp if r["pm_result"] == "won")
    wr  = wins / n * 100
    pnls = [r["pnl"] for r in grp]
    return n, wr, sum(pnls)/n, sum(pnls)

def bucket_analysis(title, rows, key_fn, bucket_defs, out):
    out.append(f"\n  [{title}]")
    out.append(tbl_header())
    out.append(tbl_sep())
    for label, cond in bucket_defs:
        grp = [r for r in rows if cond(r)]
        if not grp: continue
        n, wr, avg, tot = stats(grp)
        out.append(row_fmt(label, n, wr, avg, tot))

def percentile_cuts(rows, key, n=3):
    vals = sorted(r[key] for r in rows if r.get(key) is not None)
    if not vals: return []
    return [vals[int(len(vals)*i/n)] for i in range(1, n)]

# ── Session classifier ─────────────────────────────────────────────────────────

def session(dt_utc):
    h = dt_utc.hour
    if  0 <= h <  8: return "Asian    (00-08 UTC)"
    if  8 <= h < 13: return "European (08-13 UTC)"
    if 13 <= h < 17: return "NY open  (13-17 UTC)"
    if 17 <= h < 22: return "NY main  (17-22 UTC)"
    return "Late/Over(22-00 UTC)"

SESSION_ORDER = [
    "Asian    (00-08 UTC)",
    "European (08-13 UTC)",
    "NY open  (13-17 UTC)",
    "NY main  (17-22 UTC)",
    "Late/Over(22-00 UTC)",
]

# ── Drawdown + streaks ────────────────────────────────────────────────────────

def compute_drawdown(trades):
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum += t["pnl"]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd

def compute_streaks(trades):
    best_win = cur_win = 0
    worst_loss = cur_loss = 0
    for t in trades:
        if t["pm_result"] == "won":
            cur_win  += 1
            cur_loss = 0
            best_win = max(best_win, cur_win)
        else:
            cur_loss += 1
            cur_win  = 0
            worst_loss = max(worst_loss, cur_loss)
    return best_win, worst_loss

# ── Main report ───────────────────────────────────────────────────────────────

def build_report(trades):
    out = []
    out.append(SEP2)
    out.append(f"  FULL TRADE ANALYSIS  —  zugubotv3  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    out.append(SEP2)

    total_pnl = sum(t["pnl"] for t in trades)
    n, wr, avg, tot = stats(trades)
    win_streak, loss_streak = compute_streaks(trades)
    max_dd = compute_drawdown(trades)
    first  = trades[0]["ts_dt"]
    last   = trades[-1]["ts_dt"]
    days   = (last - first).days + 1

    out.append(f"\n  Period : {first.strftime('%Y-%m-%d %H:%M')} → {last.strftime('%Y-%m-%d %H:%M')} UTC  ({days} days)")
    out.append(f"  Trades : {n}  (won: {sum(1 for t in trades if t['pm_result']=='won')}  /  lost: {sum(1 for t in trades if t['pm_result']=='lost')})")
    out.append(f"  Win rate   : {wr:.1f}%")
    out.append(f"  Avg PnL/trade : {avg:+.3f} USDC")
    out.append(f"  Total PnL  : {tot:+.2f} USDC")
    out.append(f"  Max drawdown   : -{max_dd:.2f} USDC")
    out.append(f"  Best win streak  : {win_streak}")
    out.append(f"  Worst loss streak: {loss_streak}")
    avg_per_day = tot / days if days else 0
    out.append(f"  Avg PnL/day  : {avg_per_day:+.2f} USDC")
    out.append(f"  Projected/month: {avg_per_day*30:+.2f} USDC")

    # ── By coin ──────────────────────────────────────────────────────────────
    out.append(hdr("1. BY COIN"))
    out.append(tbl_header())
    out.append(tbl_sep())
    for coin in ["BTC","ETH"]:
        grp = [t for t in trades if t["coin"] == coin]
        n2, wr2, avg2, tot2 = stats(grp)
        out.append(row_fmt(coin, n2, wr2, avg2, tot2))

    # ── By direction ─────────────────────────────────────────────────────────
    out.append(hdr("2. BY BET DIRECTION"))
    out.append(tbl_header())
    out.append(tbl_sep())
    for direction in ["UP","DOWN"]:
        grp = [t for t in trades if t["outcome"] == direction]
        n2, wr2, avg2, tot2 = stats(grp)
        out.append(row_fmt(direction, n2, wr2, avg2, tot2))

    # ── By hour ──────────────────────────────────────────────────────────────
    out.append(hdr("3. BY HOUR (UTC)"))
    out.append(tbl_header())
    out.append(tbl_sep())
    for h in range(24):
        grp = [t for t in trades if t["ts_dt"].hour == h]
        if not grp: continue
        n2, wr2, avg2, tot2 = stats(grp)
        out.append(row_fmt(f"{h:02d}:00–{h:02d}:59", n2, wr2, avg2, tot2))

    # ── By session ───────────────────────────────────────────────────────────
    out.append(hdr("4. BY TRADING SESSION"))
    out.append(tbl_header())
    out.append(tbl_sep())
    for sess in SESSION_ORDER:
        grp = [t for t in trades if session(t["ts_dt"]) == sess]
        if not grp: continue
        n2, wr2, avg2, tot2 = stats(grp)
        out.append(row_fmt(sess, n2, wr2, avg2, tot2))

    # ── By day of week ───────────────────────────────────────────────────────
    out.append(hdr("5. BY DAY OF WEEK"))
    out.append(tbl_header())
    out.append(tbl_sep())
    days_names = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    for i, name in enumerate(days_names):
        grp = [t for t in trades if t["ts_dt"].weekday() == i]
        if not grp: continue
        n2, wr2, avg2, tot2 = stats(grp)
        out.append(row_fmt(name, n2, wr2, avg2, tot2))

    # ── By entry price (PM odds) ─────────────────────────────────────────────
    out.append(hdr("6. BY ENTRY PRICE (PM odds)"))
    out.append(tbl_header())
    out.append(tbl_sep())
    ep_buckets = [
        ("0.30–0.40", lambda t: 0.30 <= t["entry_price"] < 0.40),
        ("0.40–0.50", lambda t: 0.40 <= t["entry_price"] < 0.50),
        ("0.50–0.55", lambda t: 0.50 <= t["entry_price"] < 0.55),
        ("0.55–0.60", lambda t: 0.55 <= t["entry_price"] < 0.60),
        ("0.60–0.65", lambda t: 0.60 <= t["entry_price"] < 0.65),
        ("0.65–0.70", lambda t: 0.65 <= t["entry_price"] < 0.70),
        ("0.70–0.75", lambda t: 0.70 <= t["entry_price"] < 0.75),
    ]
    for label, cond in ep_buckets:
        grp = [t for t in trades if cond(t)]
        if not grp: continue
        n2, wr2, avg2, tot2 = stats(grp)
        out.append(row_fmt(label, n2, wr2, avg2, tot2))

    # ── By velocity 10s ──────────────────────────────────────────────────────
    out.append(hdr("7. BY VELOCITY |10s| (Binance % move in 10s)"))
    out.append(tbl_header())
    out.append(tbl_sep())
    v_rows = [t for t in trades if t["vel_10s_pct"] is not None]
    vel_buckets = [
        ("0–0.003%",    lambda t: abs(t["vel_10s_pct"]) < 0.003),
        ("0.003–0.005%",lambda t: 0.003 <= abs(t["vel_10s_pct"]) < 0.005),
        ("0.005–0.010%",lambda t: 0.005 <= abs(t["vel_10s_pct"]) < 0.010),
        ("0.010–0.020%",lambda t: 0.010 <= abs(t["vel_10s_pct"]) < 0.020),
        ("0.020–0.035%",lambda t: 0.020 <= abs(t["vel_10s_pct"]) < 0.035),
        ("0.035–0.050%",lambda t: 0.035 <= abs(t["vel_10s_pct"]) < 0.050),
        (">0.050%",     lambda t: abs(t["vel_10s_pct"]) >= 0.050),
    ]
    for label, cond in vel_buckets:
        grp = [t for t in v_rows if cond(t)]
        if not grp: continue
        n2, wr2, avg2, tot2 = stats(grp)
        out.append(row_fmt(label, n2, wr2, avg2, tot2))

    # ── By velocity 30s ──────────────────────────────────────────────────────
    out.append(hdr("8. BY VELOCITY |30s| (Binance % move in 30s)"))
    out.append(tbl_header())
    out.append(tbl_sep())
    vel30_buckets = [
        ("0–0.003%",    lambda t: abs(t["vel_30s_pct"]) < 0.003 if t["vel_30s_pct"] is not None else False),
        ("0.003–0.005%",lambda t: 0.003 <= abs(t["vel_30s_pct"]) < 0.005 if t["vel_30s_pct"] is not None else False),
        ("0.005–0.010%",lambda t: 0.005 <= abs(t["vel_30s_pct"]) < 0.010 if t["vel_30s_pct"] is not None else False),
        ("0.010–0.020%",lambda t: 0.010 <= abs(t["vel_30s_pct"]) < 0.020 if t["vel_30s_pct"] is not None else False),
        ("0.020–0.035%",lambda t: 0.020 <= abs(t["vel_30s_pct"]) < 0.035 if t["vel_30s_pct"] is not None else False),
        ("0.035–0.050%",lambda t: 0.035 <= abs(t["vel_30s_pct"]) < 0.050 if t["vel_30s_pct"] is not None else False),
        (">0.050%",     lambda t: abs(t["vel_30s_pct"]) >= 0.050 if t["vel_30s_pct"] is not None else False),
    ]
    for label, cond in vel30_buckets:
        grp = [t for t in trades if cond(t)]
        if not grp: continue
        n2, wr2, avg2, tot2 = stats(grp)
        out.append(row_fmt(label, n2, wr2, avg2, tot2))

    # ── Velocity direction vs bet ─────────────────────────────────────────────
    out.append(hdr("9. VELOCITY DIRECTION vs BET DIRECTION"))
    out.append(tbl_header())
    out.append(tbl_sep())
    v_rows = [t for t in trades if t["vel_10s_pct"] is not None]
    aligned = [t for t in v_rows if (t["outcome"]=="UP"   and t["vel_10s_pct"] > 0)
                                  or (t["outcome"]=="DOWN" and t["vel_10s_pct"] < 0)]
    against = [t for t in v_rows if not ((t["outcome"]=="UP"   and t["vel_10s_pct"] > 0)
                                       or (t["outcome"]=="DOWN" and t["vel_10s_pct"] < 0))]
    n2, wr2, avg2, tot2 = stats(aligned)
    out.append(row_fmt("Aligned (vel=bet dir)", n2, wr2, avg2, tot2))
    n2, wr2, avg2, tot2 = stats(against)
    out.append(row_fmt("Against (vel≠bet dir)", n2, wr2, avg2, tot2))

    # ── By volume (terciles) ──────────────────────────────────────────────────
    for vol_key, vol_label in [("vol_5m","5min"), ("vol_15m","15min"), ("vol_1h","1h")]:
        out.append(hdr(f"10{['a','b','c'][['vol_5m','vol_15m','vol_1h'].index(vol_key)]}. BY VOLUME {vol_label}"))
        out.append(tbl_header())
        out.append(tbl_sep())
        v_rows = [t for t in trades if t.get(vol_key) is not None]
        if not v_rows:
            out.append("  (no data)")
            continue
        cuts = percentile_cuts(v_rows, vol_key)
        if len(cuts) < 2:
            out.append("  (insufficient data)")
            continue
        p33, p66 = cuts
        vol_bkts = [
            (f"Low    (<{p33:,.0f})", lambda t, k=vol_key, p=p33: t[k] < p),
            (f"Medium ({p33:,.0f}–{p66:,.0f})", lambda t, k=vol_key, lo=p33, hi=p66: lo <= t[k] < hi),
            (f"High   (>{p66:,.0f})", lambda t, k=vol_key, p=p66: t[k] >= p),
        ]
        for label, cond in vol_bkts:
            grp = [t for t in v_rows if cond(t)]
            if not grp: continue
            n2, wr2, avg2, tot2 = stats(grp)
            out.append(row_fmt(label, n2, wr2, avg2, tot2))

    # ── Combined: velocity + volume 1h ───────────────────────────────────────
    out.append(hdr("11. COMBINED: VELOCITY |10s| × VOLUME 1h"))
    out.append(tbl_header())
    out.append(tbl_sep())
    v_rows = [t for t in trades if t["vel_10s_pct"] is not None and t["vol_1h"] is not None]
    cuts_1h = percentile_cuts(v_rows, "vol_1h")
    if len(cuts_1h) >= 2:
        p33v, p66v = cuts_1h
        combos = [
            ("Low vol  + low vel",  lambda t: t["vol_1h"] <  p33v and abs(t["vel_10s_pct"]) < 0.010),
            ("Low vol  + mid vel",  lambda t: t["vol_1h"] <  p33v and 0.010 <= abs(t["vel_10s_pct"]) < 0.050),
            ("Low vol  + high vel", lambda t: t["vol_1h"] <  p33v and abs(t["vel_10s_pct"]) >= 0.050),
            ("Med vol  + low vel",  lambda t: p33v <= t["vol_1h"] < p66v and abs(t["vel_10s_pct"]) < 0.010),
            ("Med vol  + mid vel",  lambda t: p33v <= t["vol_1h"] < p66v and 0.010 <= abs(t["vel_10s_pct"]) < 0.050),
            ("Med vol  + high vel", lambda t: p33v <= t["vol_1h"] < p66v and abs(t["vel_10s_pct"]) >= 0.050),
            ("High vol + low vel",  lambda t: t["vol_1h"] >= p66v and abs(t["vel_10s_pct"]) < 0.010),
            ("High vol + mid vel",  lambda t: t["vol_1h"] >= p66v and 0.010 <= abs(t["vel_10s_pct"]) < 0.050),
            ("High vol + high vel", lambda t: t["vol_1h"] >= p66v and abs(t["vel_10s_pct"]) >= 0.050),
        ]
        for label, cond in combos:
            grp = [t for t in v_rows if cond(t)]
            if not grp: continue
            n2, wr2, avg2, tot2 = stats(grp)
            out.append(row_fmt(label, n2, wr2, avg2, tot2))

    # ── Combined: coin + session ──────────────────────────────────────────────
    out.append(hdr("12. COMBINED: COIN × SESSION"))
    out.append(tbl_header())
    out.append(tbl_sep())
    for coin in ["BTC","ETH"]:
        for sess in SESSION_ORDER:
            grp = [t for t in trades if t["coin"]==coin and session(t["ts_dt"])==sess]
            if not grp: continue
            n2, wr2, avg2, tot2 = stats(grp)
            label = f"{coin} {sess[:8]}"
            out.append(row_fmt(label, n2, wr2, avg2, tot2))

    # ── Combined: coin + hour ─────────────────────────────────────────────────
    out.append(hdr("13. COMBINED: COIN × HOUR (UTC)"))
    out.append(tbl_header())
    out.append(tbl_sep())
    for coin in ["BTC","ETH"]:
        out.append(f"\n  — {coin} —")
        for h in range(24):
            grp = [t for t in trades if t["coin"]==coin and t["ts_dt"].hour==h]
            if not grp: continue
            n2, wr2, avg2, tot2 = stats(grp)
            out.append(row_fmt(f"  {h:02d}:00–{h:02d}:59", n2, wr2, avg2, tot2))

    # ── Cumulative PnL by day ─────────────────────────────────────────────────
    out.append(hdr("14. DAILY PnL SUMMARY"))
    out.append(f"  {'Date':<12} {'N':>4}  {'WinRate':>7}  {'DayPnL':>8}  {'CumPnL':>9}")
    out.append(f"  {SEP}")
    by_day = defaultdict(list)
    for t in trades:
        by_day[t["ts_dt"].date()].append(t)
    cum = 0.0
    for day in sorted(by_day):
        grp = by_day[day]
        n2, wr2, avg2, tot2 = stats(grp)
        cum += tot2
        out.append(f"  {str(day):<12} {n2:>4}  {wr2:>6.1f}%  {tot2:>+8.2f}  {cum:>+9.2f}")

    # ── Top worst trades ──────────────────────────────────────────────────────
    out.append(hdr("15. TOP 10 WORST TRADES"))
    out.append(f"  {'#':<4} {'Date/Time (UTC)':<20} {'Coin':<5} {'Dir':<5} {'Entry':>6} {'PnL':>7}  {'Vel10s':>8}  {'Vol1h':>10}")
    out.append(f"  {SEP}")
    worst = sorted(trades, key=lambda t: t["pnl"])[:10]
    for i, t in enumerate(worst, 1):
        v10 = f"{t['vel_10s_pct']:+.4f}%" if t["vel_10s_pct"] is not None else "  ---"
        v1h = f"{t['vol_1h']:,.0f}" if t["vol_1h"] is not None else "---"
        out.append(f"  {i:<4} {t['ts']:<20} {t['coin']:<5} {t['outcome']:<5} {t['entry_price']:>6.3f} {t['pnl']:>+7.3f}  {v10:>8}  {v1h:>10}")

    # ── Top best trades ───────────────────────────────────────────────────────
    out.append(hdr("16. TOP 10 BEST TRADES"))
    out.append(f"  {'#':<4} {'Date/Time (UTC)':<20} {'Coin':<5} {'Dir':<5} {'Entry':>6} {'PnL':>7}  {'Vel10s':>8}  {'Vol1h':>10}")
    out.append(f"  {SEP}")
    best = sorted(trades, key=lambda t: t["pnl"], reverse=True)[:10]
    for i, t in enumerate(best, 1):
        v10 = f"{t['vel_10s_pct']:+.4f}%" if t["vel_10s_pct"] is not None else "  ---"
        v1h = f"{t['vol_1h']:,.0f}" if t["vol_1h"] is not None else "---"
        out.append(f"  {i:<4} {t['ts']:<20} {t['coin']:<5} {t['outcome']:<5} {t['entry_price']:>6.3f} {t['pnl']:>+7.3f}  {v10:>8}  {v1h:>10}")

    out.append(f"\n{SEP2}\n  END OF REPORT\n{SEP2}\n")
    return "\n".join(out)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    trades = load_data()
    print(f"Loaded {len(trades)} trades — building report...")
    report = build_report(trades)
    print(report)
    with open(OUT_PATH, "w") as f:
        f.write(report)
    print(f"\nReport saved to {OUT_PATH}")
