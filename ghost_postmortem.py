"""
Ghost Post-Mortem Analyzer
==========================
Reads the paper (or live) trade DB and breaks down every resolved trade
by coin, direction, entry price, tier, hour-of-day, and duration.

Ranks each cut by Expected Value to show WHERE the real edge lives.

Usage:
  python ghost_postmortem.py            # reads crypto_ghost_PAPER.db
  python ghost_postmortem.py --live     # reads crypto_ghost.db
"""

import os, sqlite3, shutil, sys
from collections import defaultdict
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    _sd = os.path.dirname(os.path.abspath(__file__))
    _ep = os.path.join(_sd, ".env")
    if os.path.exists(_ep): load_dotenv(_ep, override=True)
except ImportError:
    _sd = os.path.dirname(os.path.abspath(__file__))

SCRIPT_DIR = _sd
LIVE       = "--live" in sys.argv
DB_PATH    = os.path.join(SCRIPT_DIR,
                          "crypto_ghost.db" if LIVE else "crypto_ghost_PAPER.db")

MIN_EV_PER_TRADE      = 0.30   # flag subsets with EV >= $0.30/trade
MIN_TRADES_FOR_SUBSET = 10


# ── Backup ────────────────────────────────────────────────────────────────────
def safety_backup():
    if not os.path.exists(DB_PATH):
        print(f"[X] DB not found: {DB_PATH}")
        return False
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode  = "LIVE" if LIVE else "PAPER"
    dst   = os.path.join(SCRIPT_DIR, f"crypto_ghost_{mode}_BACKUP_{stamp}.db")
    try:
        shutil.copy2(DB_PATH, dst)
        print(f"[OK] Backup: {os.path.basename(dst)} "
              f"({os.path.getsize(dst)//1024} KB)")
        return True
    except Exception as e:
        print(f"[X] Backup failed: {e}")
        return False


# ── Load trades ───────────────────────────────────────────────────────────────
def load_trades():
    c = sqlite3.connect(DB_PATH, timeout=60)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT * FROM trades
        WHERE status IN ('won','lost','exited')
        ORDER BY ts ASC
    """).fetchall()
    cols = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
    c.close()
    return [dict(r) for r in rows], cols


# ── Stats helpers ─────────────────────────────────────────────────────────────
def stats_block(rows, label):
    n = len(rows)
    if n == 0: return None
    wins  = sum(1 for r in rows if r["status"] in ("won", "exited"))
    pnl   = sum((r.get("pnl") or 0) for r in rows)
    stake = sum((r.get("size_usdc") or 0) for r in rows) or 1
    wr    = 100.0 * wins / n
    ev    = pnl / n
    roi   = 100.0 * pnl / stake
    return {"label": label, "n": n, "wr": wr, "pnl": pnl,
            "ev": ev, "roi": roi, "wins": wins}


def print_table(title, blocks, min_n=1):
    blocks = [b for b in blocks if b and b["n"] >= min_n]
    if not blocks:
        print(f"\n{title}: (no data)")
        return
    blocks.sort(key=lambda b: b["ev"], reverse=True)
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)
    print("  {:<32}{:>5}{:>8}{:>10}{:>12}{:>8}".format(
          "Bucket", "n", "WR%", "PnL", "EV/trade", "ROI%"))
    for b in blocks:
        flag = " [STAR]" if (b["ev"] >= MIN_EV_PER_TRADE
                             and b["n"] >= MIN_TRADES_FOR_SUBSET) else ""
        print("  {:<32}{:>5}{:>7.1f}%{:>10}{:>12}{:>7.1f}%{}".format(
            b["label"], b["n"], b["wr"],
            "${:+.2f}".format(b["pnl"]),
            "${:+.3f}".format(b["ev"]),
            b["roi"], flag))


# ── Bucket helpers ────────────────────────────────────────────────────────────
def bucket_price(p):
    if p is None:   return "unk"
    if p < 0.010:   return "0.001-0.009"
    if p < 0.020:   return "0.010-0.019"
    if p < 0.030:   return "0.020-0.029"
    if p < 0.040:   return "0.030-0.039"
    if p < 0.060:   return "0.040-0.059"
    if p < 0.080:   return "0.060-0.079"
    if p < 0.100:   return "0.080-0.099"
    if p < 0.150:   return "0.100-0.149"
    return "0.150+"


def bucket_size(s):
    if s is None:   return "unk"
    if s <= 0.60:   return "$0.50"
    if s <= 0.90:   return "$0.75"
    if s <= 1.30:   return "$1.12"
    if s <= 1.80:   return "$1.50"
    return "$2.00+"


def detect_duration(question):
    """Return '15m', '5m', or '?' based on question text."""
    if not question: return "?"
    q_lo = question.lower()
    if "15m" in q_lo or "15-min" in q_lo or "15 min" in q_lo:
        return "15m"
    if "5m" in q_lo or "5-min" in q_lo or "5 min" in q_lo:
        return "5m"
    return "?"


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 78)
    print("  GHOST POST-MORTEM ANALYZER")
    print("=" * 78)

    if not safety_backup():
        return

    try:
        rows, cols = load_trades()
    except sqlite3.DatabaseError as e:
        print(f"[X] DB read failed: {e}")
        return

    n = len(rows)
    if n == 0:
        print("[X] No resolved trades to analyze.")
        return

    overall = stats_block(rows, "ALL")
    print()
    print("-" * 78)
    print(f"  {n} resolved trades loaded")
    print(f"  Mode: {'LIVE' if LIVE else 'PAPER'} | DB: {os.path.basename(DB_PATH)}")
    print("-" * 78)
    print(f"  Overall WR     : {overall['wr']:.1f}%  (wins+exits/closed)")
    print(f"  Overall PnL    : ${overall['pnl']:+.2f}")
    print(f"  EV per trade   : ${overall['ev']:+.3f}")
    print(f"  ROI on stake   : {overall['roi']:+.1f}%")
    print(f"  Columns        : {sorted(cols)}")

    # ── By direction (UP vs DOWN) ─────────────────────────────────────────────
    by_dir = defaultdict(list)
    for r in rows:
        by_dir[str(r.get("outcome") or "?").upper()].append(r)
    print_table(
        "BY DIRECTION — UP vs DOWN bias?",
        [stats_block(rs, d) for d, rs in by_dir.items()])

    # ── By coin ───────────────────────────────────────────────────────────────
    by_coin = defaultdict(list)
    for r in rows:
        by_coin[r.get("coin", "?")].append(r)
    print_table(
        "BY COIN — BTC vs ETH vs others",
        [stats_block(rs, c) for c, rs in by_coin.items()])

    # ── By tier ───────────────────────────────────────────────────────────────
    by_tier = defaultdict(list)
    for r in rows:
        by_tier[f"T{r.get('tier','?')}"].append(r)
    print_table(
        "BY TIER — which tier is most profitable?",
        [stats_block(rs, t) for t, rs in by_tier.items()])

    # ── By entry price bucket ─────────────────────────────────────────────────
    by_price = defaultdict(list)
    for r in rows:
        by_price[bucket_price(r.get("entry_price"))].append(r)
    print_table(
        "BY ENTRY PRICE — where is the real edge?",
        [stats_block(rs, p) for p, rs in by_price.items()])

    # ── By trade size (confidence) ────────────────────────────────────────────
    by_size = defaultdict(list)
    for r in rows:
        by_size[bucket_size(r.get("size_usdc"))].append(r)
    print_table(
        "BY TRADE SIZE — does higher confidence = better results?",
        [stats_block(rs, s) for s, rs in by_size.items()])

    # ── By market duration (5m vs 15m) ────────────────────────────────────────
    by_dur = defaultdict(list)
    for r in rows:
        by_dur[detect_duration(r.get("question",""))].append(r)
    print_table(
        "BY DURATION — 5m vs 15m market performance",
        [stats_block(rs, d) for d, rs in by_dur.items()])

    # ── By hour of day (UTC) ──────────────────────────────────────────────────
    by_hour = defaultdict(list)
    for r in rows:
        ts = str(r.get("ts") or "")
        hr = ts[11:13] if len(ts) >= 13 else "??"
        by_hour[hr].append(r)
    print_table(
        "BY HOUR-OF-DAY (UTC) — when does the bot actually win?",
        [stats_block(rs, f"{h}:00") for h, rs in sorted(by_hour.items())],
        min_n=3)

    # ── 2D: coin x direction ──────────────────────────────────────────────────
    by_cd = defaultdict(list)
    for r in rows:
        key = "{}/{}".format(r.get("coin","?"),
                             str(r.get("outcome") or "?").upper())
        by_cd[key].append(r)
    print_table(
        "BY COIN x DIRECTION — which combos win?",
        [stats_block(rs, k) for k, rs in by_cd.items()],
        min_n=5)

    # ── 2D: coin x price ─────────────────────────────────────────────────────
    by_cp = defaultdict(list)
    for r in rows:
        key = "{}/{}".format(r.get("coin","?"),
                             bucket_price(r.get("entry_price")))
        by_cp[key].append(r)
    print_table(
        "BY COIN x ENTRY PRICE — sweet spot hunt",
        [stats_block(rs, k) for k, rs in by_cp.items()],
        min_n=5)

    # ── 2D: hour x coin ──────────────────────────────────────────────────────
    by_hc = defaultdict(list)
    for r in rows:
        ts = str(r.get("ts") or "")
        hr = ts[11:13] if len(ts) >= 13 else "??"
        by_hc["{}/{}".format(hr, r.get("coin","?"))].append(r)
    print_table(
        "BY HOUR x COIN — specific combos that worked",
        [stats_block(rs, k) for k, rs in by_hc.items()],
        min_n=5)

    # ── 2D: direction x price ─────────────────────────────────────────────────
    by_dp = defaultdict(list)
    for r in rows:
        key = "{}/{}".format(
            str(r.get("outcome") or "?").upper(),
            bucket_price(r.get("entry_price")))
        by_dp[key].append(r)
    print_table(
        "BY DIRECTION x ENTRY PRICE — best direction-price combos",
        [stats_block(rs, k) for k, rs in by_dp.items()],
        min_n=5)

    # ── VERDICT ───────────────────────────────────────────────────────────────
    print()
    print("=" * 78)
    print("  VERDICT")
    print("=" * 78)

    all_cuts = [
        ("direction",   by_dir,   lambda k: k),
        ("coin",        by_coin,  lambda k: k),
        ("tier",        by_tier,  lambda k: k),
        ("price",       by_price, lambda k: k),
        ("size",        by_size,  lambda k: k),
        ("duration",    by_dur,   lambda k: k),
        ("hour",        by_hour,  lambda k: f"{k}:00"),
        ("coin*dir",    by_cd,    lambda k: k),
        ("coin*price",  by_cp,    lambda k: k),
        ("hour*coin",   by_hc,    lambda k: k),
        ("dir*price",   by_dp,    lambda k: k),
    ]

    starred = []
    for title, by_key, bkt_fn in all_cuts:
        for key, rs in by_key.items():
            s = stats_block(rs, bkt_fn(key))
            if s and s["n"] >= MIN_TRADES_FOR_SUBSET and s["ev"] >= MIN_EV_PER_TRADE:
                starred.append((title, s))

    if not starred:
        print(f"  [X] No subset cleared n>={MIN_TRADES_FOR_SUBSET} AND EV>=${MIN_EV_PER_TRADE:.2f}/trade.")
        print("      Keep paper trading and review the tables above.")
    else:
        print(f"  [OK] {len(starred)} profitable subset(s) with EV>=${MIN_EV_PER_TRADE:.2f}:")
        for title, s in sorted(starred, key=lambda x: x[1]["ev"], reverse=True):
            print(f"    [{title}] {s['label']}: "
                  f"n={s['n']}  WR={s['wr']:.1f}%  "
                  f"EV=${s['ev']:+.3f}  ROI={s['roi']:+.1f}%  [STAR]")
        print()
        print("  [STAR] = n>=10 AND EV>=$0.30/trade")
        print("  Focus on these subsets when tightening scanner filters.")

    print("=" * 78)


if __name__ == "__main__":
    main()
