"""
Ghost Analyst — CryptoGhost Intelligence Agent
===============================================
The data scientist of the team. Reads every resolved market from
marketghost.db and every trade from the paper DB, then builds a
complete statistical picture of where real edge exists.

Outputs:
  ghost_analyst_report.txt   — human-readable findings
  agent_reports.db           — machine-readable (other agents read this)

Run modes:
  python ghost_analyst.py              # one analysis pass, then exit
  python ghost_analyst.py --watch      # re-run every 30 minutes
  python ghost_analyst.py --verbose    # extra detail
"""

import sqlite3, os, sys, time, json, math
from datetime import datetime, timezone

# Fix Unicode output on Windows cmd (cp1252 chokes on box-drawing chars)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Paths ──────────────────────────────────────────────────────────────────────
SD        = os.path.dirname(os.path.abspath(__file__))
MG_DB     = os.path.join(SD, "marketghost.db")
TRADE_DB  = os.path.join(SD, "crypto_ghost_PAPER.db")
RPT_DB    = os.path.join(SD, "agent_reports.db")
RPT_TXT   = os.path.join(SD, "ghost_analyst_report.txt")

VERBOSE   = "--verbose" in sys.argv
WATCH     = "--watch"   in sys.argv
INTERVAL  = 1800  # re-run every 30 min in watch mode

COINS     = ["BTC", "ETH", "SOL", "XRP", "BNB"]

# ── Helpers ───────────────────────────────────────────────────────────────────
def q(db, sql, params=()):
    if not os.path.exists(db): return []
    try:
        c = sqlite3.connect(db, timeout=60)
        rows = c.execute(sql, params).fetchall()
        c.close(); return rows
    except Exception as e:
        if VERBOSE: print(f"[DB ERR] {e}")
        return []

def qone(db, sql, params=()):
    r = q(db, sql, params)
    return r[0][0] if r else 0

def pct(n, d): return round(n / d * 100, 1) if d else 0.0

def ev(win_rate_pct, avg_entry_price):
    """Expected value per $1 risked at a given win rate and entry price."""
    wr  = win_rate_pct / 100
    payout = (1.0 - avg_entry_price) / avg_entry_price  # net profit ratio
    return round(wr * payout - (1 - wr), 4)

def star(wr): return "★★★" if wr >= 60 else ("★★" if wr >= 55 else ("★" if wr >= 52 else ""))

# ── Init agent_reports.db ──────────────────────────────────────────────────────
def init_report_db():
    c = sqlite3.connect(RPT_DB, timeout=60)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS analyst_findings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            category    TEXT,
            key         TEXT,
            value       REAL,
            label       TEXT,
            samples     INTEGER
        );
        CREATE TABLE IF NOT EXISTS agent_status (
            agent       TEXT PRIMARY KEY,
            last_run    TEXT,
            status      TEXT,
            summary     TEXT
        );
        CREATE TABLE IF NOT EXISTS param_recommendations (
            agent       TEXT,
            ts          TEXT,
            param       TEXT,
            current_val TEXT,
            rec_val     TEXT,
            reason      TEXT,
            priority    INTEGER DEFAULT 5
        );
    """)
    c.commit(); c.close()

def save_finding(cat, key, value, label="", samples=0):
    c = sqlite3.connect(RPT_DB, timeout=60)
    ts = datetime.now(timezone.utc).isoformat()
    c.execute("DELETE FROM analyst_findings WHERE category=? AND key=?", (cat, key))
    c.execute("INSERT INTO analyst_findings (ts,category,key,value,label,samples) VALUES (?,?,?,?,?,?)",
              (ts, cat, key, value, label, samples))
    c.commit(); c.close()

def save_recommendation(param, current, recommended, reason, priority=5):
    c = sqlite3.connect(RPT_DB, timeout=60)
    ts = datetime.now(timezone.utc).isoformat()
    c.execute("DELETE FROM param_recommendations WHERE agent='analyst' AND param=?", (param,))
    c.execute("INSERT INTO param_recommendations (agent,ts,param,current_val,rec_val,reason,priority) "
              "VALUES ('analyst',?,?,?,?,?,?)", (ts, param, str(current), str(recommended), reason, priority))
    c.commit(); c.close()

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — OVERALL MARKET STATS
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_overall(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 1 — OVERALL MARKET DATA")
    lines.append("═"*70)

    total_markets  = qone(MG_DB, "SELECT COUNT(*) FROM markets")
    total_resolved = qone(MG_DB, "SELECT COUNT(*) FROM resolutions")
    total_snaps    = qone(MG_DB, "SELECT COUNT(*) FROM snapshots")
    up_wins        = qone(MG_DB, "SELECT COUNT(*) FROM resolutions WHERE winner='UP'")
    dn_wins        = total_resolved - up_wins

    lines.append(f"  Markets discovered  : {total_markets:,}")
    lines.append(f"  Markets resolved    : {total_resolved:,}")
    lines.append(f"  Snapshots collected : {total_snaps:,}")
    lines.append(f"  UP wins             : {up_wins:,} ({pct(up_wins, total_resolved)}%)")
    lines.append(f"  DOWN wins           : {dn_wins:,} ({pct(dn_wins, total_resolved)}%)")
    lines.append(f"  Overall UP bias     : {'UP-leaning' if up_wins > total_resolved*0.52 else 'DOWN-leaning' if dn_wins > total_resolved*0.52 else 'Near-50/50'}")

    # Duration breakdown
    dur5  = qone(MG_DB, "SELECT COUNT(*) FROM resolutions r JOIN markets m ON r.market_id=m.market_id WHERE m.duration_minutes<=6")
    dur15 = qone(MG_DB, "SELECT COUNT(*) FROM resolutions r JOIN markets m ON r.market_id=m.market_id WHERE m.duration_minutes BETWEEN 7 AND 20")
    lines.append(f"\n  5-minute markets    : {dur5:,}")
    lines.append(f"  15-minute markets   : {dur15:,}")
    lines.append(f"  Other durations     : {total_resolved - dur5 - dur15:,}")

    save_finding("overall", "total_resolved", total_resolved, "total resolved markets", total_resolved)
    save_finding("overall", "up_pct", pct(up_wins, total_resolved), "overall UP%", total_resolved)
    return total_resolved

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — COIN-HOUR MATRIX (the money-maker)
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_coin_hour(lines, total_resolved):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 2 — COIN × HOUR BIAS MATRIX (UTC)")
    lines.append("  > 60% = strong UP bias | < 40% = strong DOWN bias | 10+ samples = reliable")
    lines.append("═"*70)

    rows = q(MG_DB, """
        SELECT coin,
               CAST(strftime('%H', end_utc) AS INTEGER) as hour,
               COUNT(*) as total,
               SUM(CASE WHEN winner='UP' THEN 1 ELSE 0 END) as up_wins
        FROM resolutions
        GROUP BY coin, hour
        ORDER BY coin, hour
    """)

    matrix = {}  # coin → {hour → (total, up_pct)}
    for coin, hour, total, up in rows:
        if coin not in matrix: matrix[coin] = {}
        up_p = pct(up, total)
        matrix[coin][hour] = (total, up_p)
        save_finding("coin_hour", f"{coin}_{hour:02d}", up_p, f"{coin} UTC{hour:02d}", total)

    # Print matrix
    header = "Coin   " + "".join(f"{h:>5}" for h in range(24))
    lines.append(f"\n  {header}")
    lines.append("  " + "-"*len(header))

    strong_up_slots = []
    strong_dn_slots = []
    for coin in COINS:
        row_str = f"{coin:<7}"
        for h in range(24):
            if coin in matrix and h in matrix[coin]:
                t, up_p = matrix[coin][h]
                if t >= 10:
                    mark = f"{up_p:>4.0f}%"
                    if up_p >= 62: strong_up_slots.append((coin, h, up_p, t))
                    elif up_p <= 38: strong_dn_slots.append((coin, h, 100-up_p, t))
                else:
                    mark = "  -- "
            else:
                mark = "  -- "
            row_str += mark
        lines.append(f"  {row_str}")

    # Best UP hours
    if strong_up_slots:
        strong_up_slots.sort(key=lambda x: -x[2])
        lines.append(f"\n  TOP UP-BIASED SLOTS (>=62% UP, >=10 samples):")
        for coin, h, up_p, t in strong_up_slots[:10]:
            lines.append(f"    {coin} UTC {h:02d}:xx  →  {up_p:.1f}% UP  ({t} samples)  {star(up_p)}")

    if strong_dn_slots:
        strong_dn_slots.sort(key=lambda x: -x[2])
        lines.append(f"\n  TOP DOWN-BIASED SLOTS (>=62% DOWN, >=10 samples):")
        for coin, h, dn_p, t in strong_dn_slots[:10]:
            lines.append(f"    {coin} UTC {h:02d}:xx  →  {dn_p:.1f}% DOWN  ({t} samples)  {star(dn_p)}")

    save_finding("coin_hour", "strong_up_count", len(strong_up_slots), "strong UP slots", len(strong_up_slots))
    save_finding("coin_hour", "strong_dn_count", len(strong_dn_slots), "strong DOWN slots", len(strong_dn_slots))
    return matrix

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — ENTRY PRICE BUCKET SIMULATION
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_entry_price(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 3 — ENTRY PRICE vs WIN RATE (from snapshots near close)")
    lines.append("  Simulates: if we'd bought at price X, what would WR and EV be?")
    lines.append("═"*70)

    # Join snapshots (last 3 mins) to resolutions
    rows = q(MG_DB, """
        SELECT s.up_ask, s.down_ask, r.winner, r.coin
        FROM snapshots s
        JOIN resolutions r ON s.market_id = r.market_id
        WHERE s.mins_to_close BETWEEN 0.5 AND 3.0
          AND s.up_ask > 0.005 AND s.up_ask < 0.50
          AND s.down_ask > 0.005 AND s.down_ask < 0.50
    """)

    if not rows:
        lines.append("  (no snapshot data in 0.5-3min window yet)")
        return

    # UP token buckets
    buckets = [
        ("$0.01-0.02", 0.010, 0.020),
        ("$0.02-0.03", 0.020, 0.030),
        ("$0.03-0.05", 0.030, 0.050),
        ("$0.05-0.08", 0.050, 0.080),
        ("$0.08-0.15", 0.080, 0.150),
    ]

    lines.append(f"\n  {'Price Range':<14} {'Trades':>7} {'UP WR%':>8} {'EV/trade':>10}  {'Verdict'}")
    lines.append("  " + "-"*65)

    best_ev = -999
    best_bucket = None

    for label, lo, hi in buckets:
        subset = [(up_ask, winner) for up_ask, down_ask, winner, coin in rows
                  if lo <= up_ask < hi]
        total = len(subset)
        if total < 5:
            lines.append(f"  {label:<14} {'<5 trades':>7}  (insufficient data)")
            continue
        wins = sum(1 for _, w in subset if w == "UP")
        wr   = pct(wins, total)
        avg_ask = sum(a for a, _ in subset) / total
        expected_v = ev(wr, avg_ask)
        verdict = ("✅ POSITIVE EV" if expected_v > 0 else "❌ NEGATIVE EV") + f"  breakeven={round(avg_ask/(1-avg_ask)*100,1)}% WR needed"
        lines.append(f"  {label:<14} {total:>7,}  {wr:>6.1f}%   {expected_v:>+.4f}  {verdict}")

        save_finding("entry_price", f"wr_{label}", wr, label, total)
        save_finding("entry_price", f"ev_{label}", expected_v, f"EV {label}", total)

        if expected_v > best_ev:
            best_ev = expected_v
            best_bucket = (label, lo, hi, wr, total, avg_ask)

    if best_bucket:
        label, lo, hi, wr, n, avg_ask = best_bucket
        lines.append(f"\n  BEST PRICE BUCKET: {label}  (WR={wr:.1f}%  EV={best_ev:+.4f}  n={n})")
        if hi <= 0.03:
            save_recommendation("T2_MAX_ENTRY", 0.10, hi,
                f"Best EV at entry <${hi:.2f}. Current $0.10 ceiling likely negative EV.", priority=1)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — OPTIMAL ENTRY TIMING (minutes to close)
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_entry_timing(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 4 — ENTRY TIMING (when in the market lifecycle to enter)")
    lines.append("═"*70)

    timing_buckets = [
        (">4.0 min", 4.0, 60.0),
        ("3.0-4.0 min", 3.0, 4.0),
        ("2.0-3.0 min", 2.0, 3.0),
        ("1.0-2.0 min", 1.0, 2.0),
        ("0.5-1.0 min", 0.5, 1.0),
        ("<0.5 min",   0.0, 0.5),
    ]

    rows = q(MG_DB, """
        SELECT s.mins_to_close, s.up_ask, r.winner
        FROM snapshots s
        JOIN resolutions r ON s.market_id = r.market_id
        WHERE s.up_ask > 0.005 AND s.up_ask < 0.15
    """)

    if not rows:
        lines.append("  (no data yet)")
        return

    lines.append(f"\n  {'Entry Time':<16} {'Samples':>8} {'UP Price':>10} {'UP WR%':>8} {'EV':>8}")
    lines.append("  " + "-"*55)

    best_timing = None
    best_timing_ev = -999

    for label, lo, hi in timing_buckets:
        subset = [(up_ask, winner) for mtc, up_ask, winner in rows if lo <= mtc < hi]
        n = len(subset)
        if n < 5:
            lines.append(f"  {label:<16} {'<5':>8}")
            continue
        wins    = sum(1 for _, w in subset if w == "UP")
        wr      = pct(wins, n)
        avg_ask = sum(a for a, _ in subset) / n
        e       = ev(wr, avg_ask)
        lines.append(f"  {label:<16} {n:>8,}  {avg_ask:>8.3f}  {wr:>6.1f}%  {e:>+.4f}")
        save_finding("timing", f"ev_{label}", e, label, n)
        if e > best_timing_ev:
            best_timing_ev = e
            best_timing = label

    if best_timing:
        lines.append(f"\n  BEST ENTRY WINDOW: {best_timing}  (EV={best_timing_ev:+.4f})")

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — PRICE DIRECTION SIGNAL VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_direction_signal(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 5 — DIRECTION SIGNAL: does price_end > price_start predict UP?")
    lines.append("  (Tests whether the 1h Binance deviation actually aligns with resolution)")
    lines.append("═"*70)

    rows = q(MG_DB, """
        SELECT price_start, price_end, winner, coin
        FROM resolutions
        WHERE price_start > 0 AND price_end > 0
    """)

    if not rows:
        lines.append("  (no price data in resolutions yet)")
        return

    # How often does 'price went up in the market window' = UP resolution?
    agree = sum(1 for ps, pe, w, c in rows if (pe >= ps and w == "UP") or (pe < ps and w == "DOWN"))
    total = len(rows)
    agree_pct = pct(agree, total)

    lines.append(f"\n  Resolutions with price data : {total:,}")
    lines.append(f"  Price direction matches win : {agree:,} ({agree_pct:.1f}%)")
    lines.append(f"  (100% expected if resolver is perfect — gaps = UMA dispute overrides)")

    # By coin
    lines.append(f"\n  {'Coin':<6} {'Total':>7} {'Match%':>8}")
    for coin in COINS:
        crows = [(ps, pe, w) for ps, pe, w, c in rows if c == coin]
        if not crows: continue
        ca = sum(1 for ps, pe, w in crows if (pe >= ps and w == "UP") or (pe < ps and w == "DOWN"))
        lines.append(f"  {coin:<6} {len(crows):>7,}  {pct(ca, len(crows)):>7.1f}%")

    # Deviation strength buckets: does stronger 1h dev → better prediction?
    rows2 = q(MG_DB, """
        SELECT (price_end - price_start) / price_start as dev, winner
        FROM resolutions
        WHERE price_start > 0 AND price_end > 0
    """)
    if rows2:
        lines.append(f"\n  DEVIATION BUCKETS (does bigger move = more predictable?)")
        lines.append(f"  {'Dev Range':<18} {'Samples':>8} {'Correct dir%':>14}")
        dev_buckets = [
            (">+0.5%",  0.005, 1.0),
            ("+0.1-0.5%", 0.001, 0.005),
            ("+0.0-0.1%", 0.0, 0.001),
            ("-0.1-0.0%", -0.001, 0.0),
            ("-0.5 to -0.1%", -0.005, -0.001),
            ("<-0.5%", -1.0, -0.005),
        ]
        for label, lo, hi in dev_buckets:
            sub = [(dev, w) for dev, w in rows2 if lo <= dev < hi]
            if len(sub) < 5: continue
            correct = sum(1 for dev, w in sub if (dev >= 0 and w == "UP") or (dev < 0 and w == "DOWN"))
            lines.append(f"  {label:<18} {len(sub):>8,}  {pct(correct, len(sub)):>13.1f}%")
        save_finding("signal", "dev_agreement_pct", agree_pct, "price_dir matches resolution", total)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — SPREAD / MARKET QUALITY
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_spread(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 6 — MARKET SPREAD QUALITY")
    lines.append("  (up_ask + down_ask should be close to 1.0 — excess = house take)")
    lines.append("═"*70)

    rows = q(MG_DB, """
        SELECT s.up_ask, s.down_ask, r.winner
        FROM snapshots s
        JOIN resolutions r ON s.market_id = r.market_id
        WHERE s.up_ask > 0.005 AND s.down_ask > 0.005
          AND s.mins_to_close BETWEEN 1.0 AND 3.0
    """)
    if not rows:
        lines.append("  (no data yet)")
        return

    spreads = [(up_ask + down_ask, winner) for up_ask, down_ask, winner in rows]
    avg_spread = sum(s for s, _ in spreads) / len(spreads)
    lines.append(f"\n  Avg bid-ask sum (should be ~1.0): {avg_spread:.3f}")
    lines.append(f"  House edge estimate: {(avg_spread - 1.0) * 100:.2f}%")

    # By bucket
    sp_buckets = [
        ("Efficient (<1.05)", 0.0, 1.05),
        ("OK (1.05-1.10)", 1.05, 1.10),
        ("Wide (1.10-1.20)", 1.10, 1.20),
        ("Terrible (>1.20)", 1.20, 9.9),
    ]
    lines.append(f"\n  {'Spread Quality':<22} {'Samples':>8} {'UP WR%':>8}")
    for label, lo, hi in sp_buckets:
        sub = [(s, w) for s, w in spreads if lo <= s < hi]
        if len(sub) < 5: continue
        wins = sum(1 for _, w in sub if w == "UP")
        lines.append(f"  {label:<22} {len(sub):>8,}  {pct(wins, len(sub)):>7.1f}%")
        save_finding("spread", f"wr_{label}", pct(wins, len(sub)), label, len(sub))

    save_finding("spread", "avg_spread", avg_spread, "avg bid-ask sum near close", len(spreads))

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — BOT PERFORMANCE OVERLAY
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_bot_performance(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 7 — BOT PERFORMANCE vs THEORETICAL EDGE")
    lines.append("═"*70)

    if not os.path.exists(TRADE_DB):
        lines.append("  (trade DB not found)")
        return

    total  = qone(TRADE_DB, "SELECT COUNT(*) FROM trades WHERE status IN ('won','lost') AND strategy='lottery'")
    wins   = qone(TRADE_DB, "SELECT COUNT(*) FROM trades WHERE status='won' AND strategy='lottery'")
    losses = total - wins
    pnl    = qone(TRADE_DB, "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status IN ('won','lost') AND strategy='lottery'")
    open_  = qone(TRADE_DB, "SELECT COUNT(*) FROM trades WHERE status='open' AND strategy='lottery'")
    total_trades = qone(TRADE_DB, "SELECT COUNT(*) FROM trades WHERE strategy='lottery'")
    avg_entry = qone(TRADE_DB, "SELECT AVG(entry_price) FROM trades WHERE status IN ('won','lost') AND strategy='lottery'")
    avg_size  = qone(TRADE_DB, "SELECT AVG(size_usdc)   FROM trades WHERE status IN ('won','lost') AND strategy='lottery'")

    wr_actual = pct(wins, total)
    breakeven_wr = round(avg_entry / (1 - avg_entry) * 100, 1) if avg_entry else 0

    lines.append(f"\n  Total trades fired   : {total_trades:,}")
    lines.append(f"  Closed (W/L)         : {total:,}  ({wins}W / {losses}L)")
    lines.append(f"  Open positions       : {open_:,}")
    lines.append(f"  Actual WR            : {wr_actual:.1f}%")
    lines.append(f"  Avg entry price      : ${avg_entry:.4f}" if avg_entry else "  Avg entry price: N/A")
    lines.append(f"  Break-even WR needed : {breakeven_wr:.1f}% (at avg entry)")
    lines.append(f"  WR gap               : {wr_actual - breakeven_wr:+.1f}% ({'edge exists' if wr_actual >= breakeven_wr else 'NEGATIVE EV'})")
    lines.append(f"  Realized P&L         : ${pnl:+.2f}")

    # Tier breakdown
    tier_rows = q(TRADE_DB, """
        SELECT tier, COUNT(*), SUM(CASE WHEN status='won' THEN 1 ELSE 0 END), COALESCE(SUM(pnl),0)
        FROM trades WHERE status IN ('won','lost') AND strategy='lottery'
        GROUP BY tier ORDER BY tier
    """)
    if tier_rows:
        lines.append(f"\n  TIER BREAKDOWN:")
        lines.append(f"  {'Tier':<6} {'Trades':>8} {'WR%':>7} {'P&L':>10}")
        for tier, n, w, p in tier_rows:
            wr_t = pct(w, n)
            lines.append(f"  T{tier}    {n:>8,}  {wr_t:>6.1f}%  ${p:>+8.2f}")
            save_finding("bot_tier", f"wr_t{tier}", wr_t, f"T{tier} WR", n)
            save_finding("bot_tier", f"pnl_t{tier}", p, f"T{tier} PnL", n)

    # Coin breakdown
    coin_rows = q(TRADE_DB, """
        SELECT coin, COUNT(*), SUM(CASE WHEN status='won' THEN 1 ELSE 0 END), COALESCE(SUM(pnl),0)
        FROM trades WHERE status IN ('won','lost') AND strategy='lottery'
        GROUP BY coin ORDER BY COUNT(*) DESC
    """)
    if coin_rows:
        lines.append(f"\n  COIN BREAKDOWN:")
        lines.append(f"  {'Coin':<6} {'Trades':>8} {'WR%':>7} {'P&L':>10}")
        for coin, n, w, p in coin_rows:
            wr_c = pct(w, n)
            lines.append(f"  {coin:<6} {n:>8,}  {wr_c:>6.1f}%  ${p:>+8.2f}")
            save_finding("bot_coin", f"wr_{coin}", wr_c, f"{coin} WR", n)

    # Win streak expectation
    if total >= 10:
        expected_first_win = round(1 / (wr_actual/100)) if wr_actual > 0 else 999
        lines.append(f"\n  Expected 1st win after  : trade #{expected_first_win}")
        lines.append(f"  Expected wins in 100    : {round(wr_actual, 0)} wins")

    save_finding("bot_overall", "actual_wr", wr_actual, "actual WR", total)
    save_finding("bot_overall", "total_pnl", pnl, "total PnL", total)
    save_finding("bot_overall", "breakeven_wr", breakeven_wr, "break-even WR", total)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7B — DIRECTION & DURATION SPLIT
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_direction_duration(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 7B — DIRECTION SPLIT (UP vs DOWN) & 5m vs 15m DURATION")
    lines.append("═"*70)

    if not os.path.exists(TRADE_DB):
        lines.append("  (trade DB not found)")
        return

    # ── UP vs DOWN performance ───────────────────────────────────────────────
    dir_rows = q(TRADE_DB, """
        SELECT outcome,
               COUNT(*) as n,
               SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as wins,
               COALESCE(SUM(pnl), 0) as pnl,
               COALESCE(SUM(size_usdc), 1) as stake
        FROM trades
        WHERE status IN ('won','lost') AND strategy='lottery'
        GROUP BY outcome
    """)

    if dir_rows:
        lines.append(f"\n  DIRECTION PERFORMANCE:")
        lines.append(f"  {'Direction':<12} {'Trades':>8} {'WR%':>7} {'P&L':>10} {'EV/trade':>10} {'ROI%':>8}")
        lines.append("  " + "-"*60)
        best_dir = None
        best_dir_wr = 0
        for outcome, n, wins, pnl_val, stake in dir_rows:
            wr    = pct(wins, n)
            ev_t  = pnl_val / n if n else 0
            roi   = 100.0 * pnl_val / stake if stake else 0
            flag  = " ★" if wr >= 35 and n >= 10 else ""
            lines.append(f"  {str(outcome):<12} {n:>8,}  {wr:>6.1f}%  "
                         f"${pnl_val:>+8.2f}  ${ev_t:>+8.3f}  {roi:>+7.1f}%{flag}")
            save_finding("direction", f"wr_{outcome}", wr, f"{outcome} WR", n)
            save_finding("direction", f"pnl_{outcome}", pnl_val, f"{outcome} PnL", n)
            if wr > best_dir_wr:
                best_dir_wr = wr
                best_dir = outcome

        # Recommendation: if one direction strongly dominates, flag it
        if len(dir_rows) == 2:
            counts = {r[0]: r[1] for r in dir_rows}
            wrs = {r[0]: pct(r[2], r[1]) for r in dir_rows if r[1] >= 10}
            up_wr = wrs.get("UP", wrs.get("up", 0))
            dn_wr = wrs.get("DOWN", wrs.get("down", 0))
            up_n  = counts.get("UP", counts.get("up", 0))
            dn_n  = counts.get("DOWN", counts.get("down", 0))

            # Read current DIRECTION_FILTER from .env
            current_filter = "auto"
            try:
                import re as _re
                env_path = os.path.join(SD, ".env")
                if os.path.exists(env_path):
                    with open(env_path) as _f:
                        m = _re.search(r"DIRECTION_FILTER\s*=\s*(\w+)", _f.read())
                        if m: current_filter = m.group(1).strip().lower()
            except Exception:
                pass

            if up_wr > 0 and dn_wr > 0:
                gap = up_wr - dn_wr
                if gap >= 15:
                    lines.append(f"\n  ★ UP massively outperforms DOWN by {gap:.1f}pp — "
                                 f"consider DIRECTION_FILTER=up_only.")
                    save_finding("direction", "up_down_gap", gap,
                                 f"UP outperforms DOWN by {gap:.1f}pp", 0)
                    # Recommend turning on up_only mode if not already
                    if current_filter != "up_only" and (up_n + dn_n) >= 30:
                        save_recommendation(
                            "DIRECTION_FILTER", current_filter, "up_only",
                            f"UP wins {up_wr:.1f}% (n={up_n}) vs DOWN {dn_wr:.1f}% (n={dn_n}) — "
                            f"{gap:.1f}pp gap. Skipping DOWN trades should improve overall WR.",
                            priority=1)
                elif gap <= -15:
                    lines.append(f"\n  ★ DOWN massively outperforms UP by {-gap:.1f}pp — "
                                 f"consider DIRECTION_FILTER=down_only.")
                    save_finding("direction", "up_down_gap", gap, f"DOWN outperforms UP by {-gap:.1f}pp", 0)
                    if current_filter != "down_only" and (up_n + dn_n) >= 30:
                        save_recommendation(
                            "DIRECTION_FILTER", current_filter, "down_only",
                            f"DOWN wins {dn_wr:.1f}% (n={dn_n}) vs UP {up_wr:.1f}% (n={up_n}) — "
                            f"{-gap:.1f}pp gap. Skipping UP trades should improve overall WR.",
                            priority=1)
                elif abs(gap) < 5 and current_filter in ("up_only","down_only") and (up_n+dn_n) >= 50:
                    # Rebalance back to auto if filter is on but gap has closed
                    save_recommendation(
                        "DIRECTION_FILTER", current_filter, "auto",
                        f"UP/DOWN performance has rebalanced ({up_wr:.1f}% vs {dn_wr:.1f}%, gap={gap:+.1f}pp). "
                        f"Returning to auto/hour-bias mode.",
                        priority=2)

    # ── 5m vs 15m duration split (detect from question text) ────────────────
    dur_rows = q(TRADE_DB, """
        SELECT question, status, pnl, size_usdc
        FROM trades
        WHERE status IN ('won','lost') AND strategy='lottery'
    """)

    if dur_rows:
        five_m  = [(s, p, sz) for q_txt, s, p, sz in dur_rows
                   if q_txt and ("5m"  in q_txt.lower() or
                                 "5-min" in q_txt.lower() or
                                 "5 min" in q_txt.lower())]
        fifteen = [(s, p, sz) for q_txt, s, p, sz in dur_rows
                   if q_txt and ("15m" in q_txt.lower() or
                                 "15-min" in q_txt.lower() or
                                 "15 min" in q_txt.lower())]
        unknown_dur = len(dur_rows) - len(five_m) - len(fifteen)

        if five_m or fifteen:
            lines.append(f"\n  DURATION SPLIT (from question text):")
            lines.append(f"  {'Duration':<12} {'Trades':>8} {'WR%':>7} {'P&L':>10} {'EV/trade':>10}")
            lines.append("  " + "-"*55)
            for label, subset in [("5-minute", five_m), ("15-minute", fifteen)]:
                if not subset: continue
                n = len(subset)
                wins = sum(1 for s, p, sz in subset if s == "won")
                pnl_val = sum(p or 0 for s, p, sz in subset)
                wr = pct(wins, n)
                ev_t = pnl_val / n if n else 0
                lines.append(f"  {label:<12} {n:>8,}  {wr:>6.1f}%  "
                             f"${pnl_val:>+8.2f}  ${ev_t:>+8.3f}")
                save_finding("duration", f"wr_{label}", wr, f"{label} WR", n)
                save_finding("duration", f"pnl_{label}", pnl_val, f"{label} PnL", n)
            if unknown_dur > 0:
                lines.append(f"  {'unknown':<12} {unknown_dur:>8,}  (question text pattern not matched)")
        else:
            lines.append(f"\n  Duration split: question text did not contain '5m'/'15m' patterns.")
            lines.append(f"  (All {len(dur_rows)} trades may be 5m — raise T2_WINDOW_MIN to enable 15m trading)")
            # If no 15m trades and T2_WINDOW_MIN is low, recommend raising it
            try:
                import re as _re
                env_path = os.path.join(SD, ".env")
                if os.path.exists(env_path):
                    txt = open(env_path).read()
                    m = _re.search(r"T2_WINDOW_MIN\s*=\s*([0-9]+)", txt)
                    if m and int(m.group(1)) < 15:
                        save_recommendation(
                            "T2_WINDOW_MIN", int(m.group(1)), 20,
                            "No 15m market trades detected. T2_WINDOW_MIN<15 blocks 15m market entry "
                            "beyond the final few minutes. 15m markets showed 34.1% WR in analysis vs "
                            "27.4% for 5m markets.",
                            priority=1)
            except Exception:
                pass

    # ── Coin × direction cross-tab ────────────────────────────────────────────
    cd_rows = q(TRADE_DB, """
        SELECT coin, outcome,
               COUNT(*) as n,
               SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as wins,
               COALESCE(SUM(pnl), 0) as pnl
        FROM trades
        WHERE status IN ('won','lost') AND strategy='lottery'
        GROUP BY coin, outcome
        ORDER BY coin, outcome
    """)
    if cd_rows:
        lines.append(f"\n  COIN × DIRECTION CROSS-TAB:")
        lines.append(f"  {'Combo':<14} {'n':>6} {'WR%':>7} {'P&L':>10}")
        lines.append("  " + "-"*42)
        for coin, outcome, n, wins, pnl_val in cd_rows:
            if n < 3: continue
            wr = pct(wins, n)
            lines.append(f"  {coin}/{outcome:<9} {n:>6,}  {wr:>6.1f}%  ${pnl_val:>+8.2f}")
            save_finding("coin_direction", f"wr_{coin}_{outcome}", wr,
                         f"{coin}/{outcome} WR", n)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7D — GHOST BRAIN STATE CORRELATION
#   Correlates the brain's "HAUNTING/STALKING/DRIFTING/DORMANT" classification
#   at trade-fire-time with the actual win/loss outcome. After 30+ trades, this
#   tells us whether the brain's signals predict trade outcomes.
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_brain_states(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 7D — GHOST BRAIN STATE vs TRADE OUTCOME")
    lines.append("═"*70)

    if not os.path.exists(TRADE_DB):
        lines.append("  (trade DB not found)")
        return

    # Check if brain_state column exists yet (added when scanner first fires after upgrade)
    cols = q(TRADE_DB, "PRAGMA table_info(trades)")
    has_col = any(c[1] == 'brain_state' for c in cols)
    if not has_col:
        lines.append("\n  brain_state column not yet present — first trade after restart will create it.")
        return

    rows = q(TRADE_DB, """
        SELECT brain_state, brain_score, status, pnl, size_usdc
        FROM trades
        WHERE status IN ('won','lost') AND strategy='lottery' AND brain_state IS NOT NULL
    """)

    if not rows:
        lines.append("\n  No trades yet have brain_state recorded (brain may not be running).")
        lines.append("  Confirm Ghost Brain window is open — it writes ghost_brain.json every 10s.")
        return

    # Aggregate by state
    by_state = {}
    for state, score, status, pnl_val, size in rows:
        s = state or 'UNKNOWN'
        by_state.setdefault(s, []).append((status, pnl_val or 0, size or 0))

    lines.append(f"\n  {'State':<12} {'n':>5}  {'WR%':>7}  {'PnL':>10}  {'EV/trade':>10}  {'ROI%':>8}")
    lines.append("  " + "-"*60)

    # Show in canonical order, then any extras
    state_order = ['HAUNTING','STALKING','DRIFTING','DORMANT','UNKNOWN']
    seen = set()
    for s in state_order + sorted(set(by_state.keys()) - set(state_order)):
        if s in seen or s not in by_state: continue
        seen.add(s)
        recs = by_state[s]
        n     = len(recs)
        wins  = sum(1 for st, _, _ in recs if st == 'won')
        pnl_v = sum(p for _, p, _ in recs)
        stake = sum(sz for _, _, sz in recs) or 1
        wr    = pct(wins, n)
        ev    = pnl_v / n if n else 0
        roi   = 100.0 * pnl_v / stake
        flag  = ' ★' if wr >= 35 and n >= 5 else ''
        lines.append(f"  {s:<12} {n:>5}   {wr:>5.1f}%  ${pnl_v:>+8.2f}  ${ev:>+8.3f}  {roi:>+7.1f}%{flag}")
        save_finding("brain_state", f"wr_{s}", wr, f"brain={s} WR", n)
        save_finding("brain_state", f"ev_{s}", ev,  f"brain={s} EV", n)

    # Action threshold: if HAUNTING WR is meaningfully better than DORMANT,
    # recommend a MIN_BRAIN_STATE filter once we have enough data
    h_wr = (pct(sum(1 for st,_,_ in by_state.get('HAUNTING',[]) if st=='won'),
                len(by_state.get('HAUNTING',[]))) if by_state.get('HAUNTING') else None)
    d_wr = (pct(sum(1 for st,_,_ in by_state.get('DORMANT',[]) if st=='won'),
                len(by_state.get('DORMANT',[]))) if by_state.get('DORMANT') else None)
    h_n  = len(by_state.get('HAUNTING',[]))
    d_n  = len(by_state.get('DORMANT',[]))
    if h_n >= 10 and d_n >= 10 and h_wr is not None and d_wr is not None:
        gap = h_wr - d_wr
        if gap >= 15:
            lines.append(f"\n  ★ HAUNTING beats DORMANT by {gap:.1f}pp — brain IS predictive. "
                         f"Consider MIN_BRAIN_STATE=STALKING filter.")
        elif gap <= -10:
            lines.append(f"\n  ⚠ DORMANT beats HAUNTING by {-gap:.1f}pp — brain may be inverted "
                         f"or noise. Investigate.")
        else:
            lines.append(f"\n  Brain shows weak/no edge at this sample (gap={gap:+.1f}pp).")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7C — TIME-OF-DAY (per-hour WR for the bot's actual trades)
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_hours(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 7C — TIME-OF-DAY WIN RATE (BLOCKED_HOURS auto-tuning)")
    lines.append("═"*70)

    if not os.path.exists(TRADE_DB):
        lines.append("  (trade DB not found)")
        return

    rows = q(TRADE_DB, """
        SELECT substr(ts,12,2) AS h, COUNT(*) AS n,
               SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS w,
               COALESCE(SUM(pnl),0) AS pnl
        FROM trades
        WHERE status IN ('won','lost') AND strategy='lottery'
        GROUP BY h ORDER BY h
    """)

    # Read current BLOCKED_HOURS from .env
    current_blocked = set()
    try:
        import re as _re
        env_path = os.path.join(SD, ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                m = _re.search(r"^BLOCKED_HOURS\s*=\s*([^\n]*)", f.read(), _re.M)
                if m:
                    raw = m.group(1).strip()
                    for tok in raw.split(","):
                        try:
                            v = int(tok.strip())
                            if 0 <= v <= 23: current_blocked.add(v)
                        except Exception: pass
    except Exception: pass

    if not rows:
        lines.append(f"\n  No closed trades yet to analyze hours.")
        lines.append(f"  Current BLOCKED_HOURS = {sorted(current_blocked) if current_blocked else 'empty'}")
        return

    lines.append(f"\n  Current BLOCKED_HOURS in .env: {sorted(current_blocked) if current_blocked else '(empty)'}")
    lines.append(f"\n  {'UTC Hour':<10}{'n':>5}{'Wins':>7}{'WR%':>8}{'PnL':>10}  Verdict")
    lines.append("  " + "-"*56)

    MIN_TRADES = 5      # need at least 5 trades to act on an hour
    BLOCK_AT_WR = 15.0  # WR threshold below which hour MIGHT be cold
    UNBLOCK_AT  = 25.0  # unblock if WR recovers to this with enough samples
    # CRITICAL: only block hours that are ACTUALLY losing money (negative PnL).
    # Low WR with positive PnL = the cheap-longshot strategy working as designed
    # (1 big win covers many losses). Blocking those would crater profits.

    new_blocked = set(current_blocked)
    block_reasons   = {}
    unblock_reasons = {}

    for h, n, w, pnl_val in rows:
        try: hour_int = int(h)
        except: continue
        wr = pct(w, n)
        is_blocked = hour_int in current_blocked

        if is_blocked:
            verdict = "BLOCKED"
            # Unblock if WR recovers OR if PnL has turned positive
            if n >= MIN_TRADES and (wr >= UNBLOCK_AT or pnl_val > 0):
                verdict = "★ unblock candidate (recovered)"
                new_blocked.discard(hour_int)
                unblock_reasons[hour_int] = (f"WR {wr:.1f}% / PnL ${pnl_val:+.2f} over {n} trades")
        else:
            # Block ONLY if low WR AND negative PnL (actually losing money)
            if n >= MIN_TRADES and wr < BLOCK_AT_WR and pnl_val < 0:
                verdict = "★ block candidate (cold AND losing)"
                new_blocked.add(hour_int)
                block_reasons[hour_int] = (f"WR {wr:.1f}% AND PnL ${pnl_val:+.2f} over {n} trades")
            elif n >= MIN_TRADES and wr < BLOCK_AT_WR and pnl_val >= 0:
                verdict = f"low WR but +${pnl_val:.0f} PnL (KEEP)"
            elif n >= MIN_TRADES and wr < BLOCK_AT_WR + 10:
                verdict = "watching"
            else:
                verdict = ""

        lines.append(f"  UTC {h}:xx   {n:>4} {w:>6}  {wr:>5.1f}%  ${pnl_val:>+8.2f}  {verdict}")
        save_finding("hour_wr", f"wr_{h}", wr, f"UTC {h}:xx WR", n)

    # Emit a single consolidated recommendation if BLOCKED_HOURS should change
    if new_blocked != current_blocked:
        new_str  = ",".join(str(h) for h in sorted(new_blocked))
        cur_str  = ",".join(str(h) for h in sorted(current_blocked))
        added    = sorted(new_blocked - current_blocked)
        removed  = sorted(current_blocked - new_blocked)
        parts = []
        for hh in added:
            parts.append(f"ADD {hh:02d}: {block_reasons.get(hh,'cold')}")
        for hh in removed:
            parts.append(f"REMOVE {hh:02d}: {unblock_reasons.get(hh,'recovered')}")
        reason = " | ".join(parts)[:140]
        priority = 1 if added else 2
        save_recommendation("BLOCKED_HOURS", cur_str or "(empty)", new_str or "(empty)",
                            reason, priority=priority)
        lines.append(f"\n  ★ Recommendation: BLOCKED_HOURS = {cur_str or '(empty)'} → {new_str or '(empty)'}")
        lines.append(f"     {reason[:100]}")
    else:
        lines.append(f"\n  No BLOCKED_HOURS changes recommended (need n≥{MIN_TRADES} per hour to act).")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — CHEAP TOKEN ENTRY SIGNAL ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_penny_ask(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 8 — CHEAP TOKEN SIGNAL (tradeable low-ask entries)")
    lines.append("  Looks for real $0.01-$0.05 ask prices in the near-close window.")
    lines.append("  NOTE: ask=0.0 means EMPTY ORDERBOOK (untradeable) — excluded here.")
    lines.append("═"*70)

    # Only count REAL ask prices — exclude 0.0 (empty orderbook)
    rows = q(MG_DB, """
        SELECT s.up_ask, s.down_ask, r.winner, r.coin,
               CAST(strftime('%H', r.end_utc) AS INTEGER) as hour
        FROM snapshots s
        JOIN resolutions r ON s.market_id = r.market_id
        WHERE s.mins_to_close BETWEEN 0.5 AND 3.0
          AND s.up_ask > 0.005 AND s.down_ask > 0.005
    """)

    tradeable_cheap = [(ua, da, winner, coin, hour)
                       for ua, da, winner, coin, hour in rows
                       if ua < 0.10 or da < 0.10]

    lines.append(f"\n  Total near-close tradeable snapshots : {len(rows):,}")
    lines.append(f"  With at least one cheap side (<$0.10): {len(tradeable_cheap):,}")

    if not tradeable_cheap:
        lines.append("\n  ⚠️  No genuine cheap-entry snapshots yet.")
        lines.append("  MarketGhost needs to catch markets while they are still in play,")
        lines.append("  not just near resolution. Data collection is working — keep running.")
        save_finding("penny_ask", "wr_up_penny", 0, "no valid data yet", 0)
        save_finding("penny_ask", "wr_down_penny", 0, "no valid data yet", 0)
        return

    # UP-cheap: up_ask < $0.05, buy UP (crowd pricing UP as likely loser)
    up_cheap = [(ua, winner, coin) for ua, da, winner, coin, hour in tradeable_cheap if ua < 0.05]
    dn_cheap = [(da, winner, coin) for ua, da, winner, coin, hour in tradeable_cheap if da < 0.05]

    if up_cheap:
        wins = sum(1 for _, w, _ in up_cheap if w == "UP")
        wr = pct(wins, len(up_cheap))
        avg_ask = sum(a for a, _, _ in up_cheap) / len(up_cheap)
        lines.append(f"\n  BUY UP when up_ask < $0.05 ({len(up_cheap)} samples):")
        lines.append(f"    WR={wr:.1f}%  avg_ask=${avg_ask:.3f}  EV/trade={ev(wr, avg_ask):+.3f}")
        save_finding("penny_ask", "wr_up_penny", wr, "UP wins when up_ask<$0.05", len(up_cheap))
    else:
        save_finding("penny_ask", "wr_up_penny", 0, "no valid cheap UP entries", 0)

    if dn_cheap:
        wins = sum(1 for _, w, _ in dn_cheap if w == "DOWN")
        wr = pct(wins, len(dn_cheap))
        avg_ask = sum(a for a, _, _ in dn_cheap) / len(dn_cheap)
        lines.append(f"\n  BUY DOWN when down_ask < $0.05 ({len(dn_cheap)} samples):")
        lines.append(f"    WR={wr:.1f}%  avg_ask=${avg_ask:.3f}  EV/trade={ev(wr, avg_ask):+.3f}")
        save_finding("penny_ask", "wr_down_penny", wr, "DOWN wins when down_ask<$0.05", len(dn_cheap))
    else:
        save_finding("penny_ask", "wr_down_penny", 0, "no valid cheap DOWN entries", 0)

    lines.append(f"\n  NOTE: ask=0.0 in snapshot = empty orderbook (untradeable) — not counted.")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — BNB DEEP DIVE
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_bnb(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 9 — BNB DEEP DIVE (bot never trades BNB — should it?)")
    lines.append("═"*70)

    total_bnb = qone(MG_DB, "SELECT COUNT(*) FROM resolutions WHERE coin='BNB'")
    up_bnb    = qone(MG_DB, "SELECT COUNT(*) FROM resolutions WHERE coin='BNB' AND winner='UP'")
    if total_bnb == 0:
        lines.append("  (no BNB resolution data yet)")
        return

    overall_up = pct(up_bnb, total_bnb)
    lines.append(f"\n  BNB total resolved: {total_bnb:,}")
    lines.append(f"  Overall UP rate   : {overall_up:.1f}%  {'★ STRONG UP BIAS' if overall_up >= 60 else ''}")

    save_finding("bnb", "overall_up_pct", overall_up, "BNB overall UP%", total_bnb)

    # Hour-by-hour BNB analysis
    hour_rows = q(MG_DB, """
        SELECT CAST(strftime('%H', end_utc) AS INTEGER) as hour,
               COUNT(*) as total,
               SUM(CASE WHEN winner='UP' THEN 1 ELSE 0 END) as up
        FROM resolutions WHERE coin='BNB'
        GROUP BY hour ORDER BY hour
    """)

    if hour_rows:
        lines.append(f"\n  BNB by UTC hour:")
        lines.append(f"  {'Hour':>6}  {'Total':>6}  {'UP%':>6}  Signal")
        strong_hours_up = []
        strong_hours_dn = []
        for hour, total, up in hour_rows:
            if total < 5:
                continue
            up_p = pct(up, total)
            signal = "▲▲▲ STRONG UP" if up_p >= 70 else ("▲ UP" if up_p >= 60 else
                     ("▼▼▼ STRONG DN" if up_p <= 30 else ("▼ DN" if up_p <= 40 else "~")))
            lines.append(f"  UTC {hour:02d}:xx  {total:>6,}  {up_p:>5.1f}%  {signal}")
            save_finding("bnb_hour", f"bnb_{hour:02d}", up_p, f"BNB UTC{hour:02d}", total)
            if up_p >= 65 and total >= 8:
                strong_hours_up.append((hour, up_p, total))
            elif up_p <= 35 and total >= 8:
                strong_hours_dn.append((hour, 100-up_p, total))

        if strong_hours_up:
            best_hours = ",".join(str(h) for h, _, _ in sorted(strong_hours_up, key=lambda x: -x[1]))
            lines.append(f"\n  BNB best UP hours (UTC): {best_hours}")
            save_finding("bnb", "best_up_hours", len(strong_hours_up),
                         f"BNB strong UP hours: {best_hours}", len(strong_hours_up))
        if strong_hours_dn:
            best_dn = ",".join(str(h) for h, _, _ in sorted(strong_hours_dn, key=lambda x: -x[1]))
            lines.append(f"  BNB best DOWN hours (UTC): {best_dn}")

    # BNB snapshot signal strength
    bnb_snaps = q(MG_DB, """
        SELECT s.up_ask, r.winner
        FROM snapshots s
        JOIN resolutions r ON s.market_id = r.market_id
        WHERE r.coin = 'BNB'
          AND s.mins_to_close BETWEEN 1.0 AND 3.0
          AND s.up_ask > 0.005 AND s.up_ask < 0.20
    """)
    if bnb_snaps:
        bnb_up_wins = sum(1 for _, w in bnb_snaps if w == "UP")
        bnb_wr = pct(bnb_up_wins, len(bnb_snaps))
        avg_ask = sum(a for a, _ in bnb_snaps) / len(bnb_snaps)
        lines.append(f"\n  BNB snapshot WR (buying UP always): {bnb_wr:.1f}%  "
                     f"(n={len(bnb_snaps)}, avg_ask={avg_ask:.3f})")
        lines.append(f"  EV of always-UP strategy: {ev(bnb_wr, avg_ask):+.4f}")
        save_finding("bnb", "snapshot_up_wr", bnb_wr, "BNB always-UP WR from snapshots", len(bnb_snaps))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — COIN PERFORMANCE RANKING
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_coin_ranking(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 10 — COIN PERFORMANCE RANKING (should we trade all coins?)")
    lines.append("═"*70)

    rows = q(MG_DB, """
        SELECT coin, COUNT(*) as total,
               SUM(CASE WHEN winner='UP' THEN 1 ELSE 0 END) as up,
               SUM(CASE WHEN winner='DOWN' THEN 1 ELSE 0 END) as dn
        FROM resolutions
        GROUP BY coin ORDER BY total DESC
    """)

    if not rows:
        lines.append("  (no data)")
        return

    # Bot actual performance per coin (most reliable signal we have)
    bot_rows = q(TRADE_DB, """
        SELECT coin, COUNT(*), SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
               COALESCE(SUM(pnl),0), AVG(entry_price)
        FROM trades WHERE status IN ('won','lost') AND strategy='lottery'
        GROUP BY coin
    """) if os.path.exists(TRADE_DB) else []
    bot_stats = {r[0]: (r[1], r[2], r[3], r[4]) for r in bot_rows}  # coin → (n, wins, pnl, avg_entry)

    lines.append(f"\n  {'Coin':<5}  {'Resolved':>9}  {'MG UP%':>7}  {'Bot':>10}  {'Bot WR':>8}  {'Bot PnL':>9}  Verdict")
    lines.append("  " + "-"*70)

    coin_verdicts = {}
    for coin, total, up, dn in rows:
        mg_up_p = pct(up, total)
        bstats  = bot_stats.get(coin)
        if bstats:
            bn, bw, bp, bavg = bstats
            bwr     = pct(bw, bn)
            verdict = "✅ OK" if bp >= 0 else ("⚠️  LOSS" if bp > -20 else "❌ LOSING")
            bot_str = f"{bw}W/{bn-bw}L"
        else:
            bwr     = 0.0; bp = 0.0; bot_str = "no trades"
            verdict = "— BNB: add to scanner" if coin == "BNB" else "no bot data"

        lines.append(f"  {coin:<5}  {total:>9,}  {mg_up_p:>6.1f}%  "
                     f"{bot_str:>10}  {bwr:>7.1f}%  ${bp:>+8.2f}  {verdict}")
        save_finding("coin_rank", f"ev_{coin}", bp, f"{coin} realized PnL", bstats[0] if bstats else 0)
        coin_verdicts[coin] = bp

    lines.append(f"\n  MG UP% = historical UP win rate from MarketGhost (6k+ markets)")
    lines.append(f"  Bot columns = actual paper-trade performance")
    return coin_verdicts


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════════════════════════
def generate_recommendations(lines):
    lines.append("\n" + "═"*70)
    lines.append("  SECTION 8 — ANALYST RECOMMENDATIONS")
    lines.append("═"*70)

    recs = []

    # Clear ALL previous analyst recommendations before re-generating
    # This ensures stale recs from prior runs don't persist when conditions change
    try:
        _c = sqlite3.connect(RPT_DB, timeout=60)
        _c.execute("DELETE FROM param_recommendations WHERE agent='analyst'")
        _c.commit(); _c.close()
    except Exception:
        pass

    # Read findings from this run
    c = sqlite3.connect(RPT_DB, timeout=60)
    findings = {row[0]: (row[1], row[2]) for row in
                c.execute("SELECT key, value, samples FROM analyst_findings").fetchall()}
    c.close()

    def get(key, default=0):
        return findings.get(key, (default, 0))[0]

    # ── Bot actual performance ──────────────────────────────────────────────
    actual_wr     = get("actual_wr", 0)
    total_pnl     = get("total_pnl", 0)
    breakeven_wr  = get("breakeven_wr", 0)

    # ── Rec 1: T2_MAX_ENTRY ─────────────────────────────────────────────────
    best_ev_01 = get("ev_$0.01-0.02", -99)
    best_ev_02 = get("ev_$0.02-0.03", -99)
    # Read current .env value to compute correct diff
    current_max_entry = 0.10
    try:
        import re as _re
        env_path = os.path.join(SD, ".env")
        if os.path.exists(env_path):
            txt = open(env_path).read()
            m = _re.search(r"T2_MAX_ENTRY\s*=\s*([0-9.]+)", txt)
            if m: current_max_entry = float(m.group(1))
    except Exception:
        pass

    if current_max_entry > 0.03:
        if best_ev_01 > 0 or best_ev_02 > 0:
            reason = (f"MarketGhost confirms: entries below $0.03 have POSITIVE EV "
                      f"(EV_{'{$0.01-0.02}'}={best_ev_01:+.4f}, EV_{'{$0.02-0.03}'}={best_ev_02:+.4f}). "
                      f"Current ${current_max_entry:.2f} ceiling allows bad high-cost entries.")
        else:
            reason = (f"MATH: at ${current_max_entry:.2f} entry, break-even WR = "
                      f"{current_max_entry/(1-current_max_entry)*100:.1f}%. "
                      f"At $0.03 ceiling break-even drops to 3.3%. "
                      f"This is the highest-impact single change.")
        recs.append(("CRITICAL", "T2_MAX_ENTRY", f"{current_max_entry} → 0.03", reason, 1))
        save_recommendation("T2_MAX_ENTRY", current_max_entry, 0.03, reason, priority=1)
    else:
        lines.append("\n  ✅ T2_MAX_ENTRY already at optimal $0.03 ceiling.")

    # ── Rec 2: HOUR_BIAS_STRONG ─────────────────────────────────────────────
    strong_up = get("strong_up_count", 0)
    strong_dn = get("strong_dn_count", 0)
    total_bias_slots = int(strong_up + strong_dn)
    current_bias_strong = 60
    try:
        import re as _re
        env_path = os.path.join(SD, ".env")
        if os.path.exists(env_path):
            txt = open(env_path).read()
            m = _re.search(r"HOUR_BIAS_STRONG\s*=\s*([0-9]+)", txt)
            if m: current_bias_strong = int(m.group(1))
    except Exception:
        pass

    if total_bias_slots >= 5 and current_bias_strong > 58:
        reason = (f"Found {total_bias_slots} coin-hour slots with >=62% historical bias "
                  f"from {int(get('total_resolved',0)):,} markets. "
                  f"Lowering {current_bias_strong}→58 activates filter in more proven slots.")
        recs.append(("HIGH", "HOUR_BIAS_STRONG", f"{current_bias_strong} → 58", reason, 2))
        save_recommendation("HOUR_BIAS_STRONG", current_bias_strong, 58, reason, priority=2)
    elif total_bias_slots < 5:
        lines.append(f"\n  ℹ️  HOUR_BIAS_STRONG: only {total_bias_slots} strong slots — keep as-is until more data.")

    # ── Rec 3: HOUR_BIAS_MIN_SAMPLES ────────────────────────────────────────
    current_min_samples = 15
    try:
        import re as _re
        env_path = os.path.join(SD, ".env")
        if os.path.exists(env_path):
            txt = open(env_path).read()
            m = _re.search(r"HOUR_BIAS_MIN_SAMPLES\s*=\s*([0-9]+)", txt)
            if m: current_min_samples = int(m.group(1))
    except Exception:
        pass

    total_resolved = int(get("total_resolved", 0))
    if total_resolved >= 500 and current_min_samples > 10:
        reason = (f"With {total_resolved:,} resolved markets, 10-sample threshold is "
                  f"statistically valid and activates the hour-bias filter in more slots.")
        recs.append(("HIGH", "HOUR_BIAS_MIN_SAMPLES", f"{current_min_samples} → 10", reason, 2))
        save_recommendation("HOUR_BIAS_MIN_SAMPLES", current_min_samples, 10, reason, priority=2)

    # ── Rec 4: T2_MIN_DEV — based on deviation band accuracy from signal analysis ─
    # ask=0.0 in snapshots = empty orderbook (untradeable), not a real price signal.
    # T2_MIN_DEV should stay at 0.005 to require a real 1h trend before entering.
    current_min_dev = 0.003
    try:
        import re as _re
        env_path = os.path.join(SD, ".env")
        if os.path.exists(env_path):
            txt = open(env_path).read()
            m = _re.search(r"T2_MIN_DEV\s*=\s*([0-9.]+)", txt)
            if m: current_min_dev = float(m.group(1))
    except Exception:
        pass

    # Deviation signal accuracy from Section 5
    dev_agree = get("dev_agreement_pct", 0)
    if dev_agree > 0 and current_min_dev < 0.003:
        reason = (f"Price direction matches resolution {dev_agree:.1f}% of the time. "
                  f"Requiring at least 0.3% 1h deviation ensures the signal is real "
                  f"before entering — very small moves are noise.")
        recs.append(("HIGH", "T2_MIN_DEV", f"{current_min_dev} → 0.003", reason, 2))
        save_recommendation("T2_MIN_DEV", current_min_dev, 0.003, reason, priority=2)
    elif current_min_dev > 0.010:
        reason = (f"T2_MIN_DEV={current_min_dev:.4f} may be too restrictive — "
                  f"filtering out valid opportunities. 0.005 (0.5%) is a good balance.")
        recs.append(("MEDIUM", "T2_MIN_DEV", f"{current_min_dev} → 0.005", reason, 3))
        save_recommendation("T2_MIN_DEV", current_min_dev, 0.005, reason, priority=3)

    # ── Rec 5: DISABLED_COINS — auto-manage based on per-coin performance ───
    # Logic: disable coins with 0 wins after 6+ trades. Never touch BTC/ETH.
    # BNB is protected until it accumulates 20 trades (it's new).
    # Re-enable a disabled coin if it starts winning or needs more data.
    PROTECTED = {"BTC", "ETH"}   # never disabled
    BNB_MIN_TRADES = 20          # don't judge BNB until it has 20 trades

    current_disabled_str = ""
    try:
        import re as _re2
        _env_path = os.path.join(SD, ".env")
        if os.path.exists(_env_path):
            _txt = open(_env_path).read()
            _m = _re2.search(r"DISABLED_COINS\s*=\s*([^\n]*)", _txt)
            if _m: current_disabled_str = _m.group(1).strip()
    except Exception:
        pass
    currently_disabled = {c.strip().upper() for c in current_disabled_str.split(",") if c.strip()}

    # Get per-coin bot stats
    trade_db = os.path.join(SD, "crypto_ghost_PAPER.db")
    coin_stats = {}
    if os.path.exists(trade_db):
        try:
            _dc = sqlite3.connect(trade_db, timeout=60)
            for row in _dc.execute("""SELECT coin, COUNT(*), SUM(CASE WHEN status='won' THEN 1 ELSE 0 END)
                FROM trades WHERE status IN ('won','lost') AND strategy='lottery' GROUP BY coin""").fetchall():
                coin_stats[row[0]] = (row[1], row[2])  # (total, wins)
            _dc.close()
        except Exception:
            pass

    recommended_disabled = set(currently_disabled)  # start from current state
    disable_reasons = {}
    enable_reasons  = {}

    for coin, (total, wins) in coin_stats.items():
        if coin in PROTECTED:
            continue
        if coin == "BNB" and total < BNB_MIN_TRADES:
            continue
        # Disable: 0 wins after 6+ trades
        if total >= 6 and wins == 0 and coin not in currently_disabled:
            recommended_disabled.add(coin)
            disable_reasons[coin] = f"0W/{total}L — strategy has no edge on this coin yet"
        # Re-enable: coin is disabled but now has wins (was re-enabled externally or restarted)
        if coin in currently_disabled and wins > 0:
            recommended_disabled.discard(coin)
            enable_reasons[coin] = f"{wins}W/{total-wins}L — positive performance, re-enabling"

    new_disabled_str = ",".join(sorted(recommended_disabled)) if recommended_disabled else ""
    # Normalize current for comparison (order doesn't matter functionally)
    current_disabled_normalized = ",".join(sorted(currently_disabled)) if currently_disabled else ""

    if new_disabled_str != current_disabled_normalized:
        changes = []
        newly_disabled = recommended_disabled - currently_disabled
        newly_enabled  = currently_disabled - recommended_disabled
        for coin in sorted(newly_disabled):
            changes.append(f"DISABLE {coin}: {disable_reasons.get(coin,'performance')}")
        for coin in sorted(newly_enabled):
            changes.append(f"ENABLE {coin}: {enable_reasons.get(coin,'performance improved')}")
        reason = " | ".join(changes)
        recs.append(("CRITICAL", "DISABLED_COINS", f"{current_disabled_str} → {new_disabled_str}", reason, 1))
        save_recommendation("DISABLED_COINS", current_disabled_str, new_disabled_str, reason, priority=1)
    else:
        lines.append(f"\n  ✅ DISABLED_COINS={current_disabled_str!r} — no change needed.")

    # ── Rec 6: MIN_NO_PRICE — tighten when spread data supports it ──────────
    avg_spread = get("avg_spread", 0)
    current_min_no_price = 0.80
    try:
        import re as _re
        env_path = os.path.join(SD, ".env")
        if os.path.exists(env_path):
            txt = open(env_path).read()
            m = _re.search(r"MIN_NO_PRICE\s*=\s*([0-9.]+)", txt)
            if m: current_min_no_price = float(m.group(1))
    except Exception:
        pass

    if avg_spread > 1.08 and current_min_no_price < 0.85:
        reason = (f"Avg market spread {avg_spread:.3f} implies {(avg_spread-1)*100:.1f}% house edge. "
                  f"Tightening MIN_NO_PRICE {current_min_no_price:.2f}→0.85 filters the worst spreads.")
        recs.append(("MEDIUM", "MIN_NO_PRICE", f"{current_min_no_price:.2f} → 0.85", reason, 3))
        save_recommendation("MIN_NO_PRICE", current_min_no_price, 0.85, reason, priority=3)

    # ── Rec 6: T2_WINDOW_MIN — check if 3-minute window is optimal ──────────
    best_timing_ev = get("ev_1.0-2.0 min", -99)
    if best_timing_ev > 0:
        current_window = 3
        try:
            import re as _re
            env_path = os.path.join(SD, ".env")
            if os.path.exists(env_path):
                txt = open(env_path).read()
                m = _re.search(r"T2_WINDOW_MIN\s*=\s*([0-9.]+)", txt)
                if m: current_window = float(m.group(1))
        except Exception:
            pass
        if current_window > 2:
            reason = (f"Entry at 1.0-2.0 min to close shows highest EV ({best_timing_ev:+.4f}). "
                      f"Tightening window to 2 min ensures entries in the sweet spot.")
            recs.append(("MEDIUM", "T2_WINDOW_MIN", f"{current_window:.0f} → 2", reason, 3))
            save_recommendation("T2_WINDOW_MIN", int(current_window), 2, reason, priority=3)

    if not recs:
        lines.append("\n  All parameters appear optimal for current data. Keep running.")
        return

    lines.append(f"\n  Total recommendations: {len(recs)}")
    for priority, param, change, reason, _ in recs:
        lines.append(f"\n  [{priority}] {param}: {change}")
        lines.append(f"    Reason: {reason[:100]}")

    lines.append(f"\n  Recommendations saved to agent_reports.db")
    lines.append(f"  Ghost Command will apply these automatically on next cycle.")

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def run_analysis():
    init_report_db()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append("╔" + "═"*70 + "╗")
    lines.append("║     GHOST ANALYST — CryptoGhost Intelligence Report              ║")
    lines.append(f"║     Generated: {now_str:<38}║")
    lines.append("╚" + "═"*70 + "╝")

    if not os.path.exists(MG_DB):
        lines.append("\n  ⚠️  marketghost.db not found. Run marketghost.py first.\n")
        report = "\n".join(lines)
        print(report)
        with open(RPT_TXT, "w") as f: f.write(report)
        return

    total_resolved = analyze_overall(lines)
    analyze_coin_hour(lines, total_resolved)
    analyze_entry_price(lines)
    analyze_entry_timing(lines)
    analyze_direction_signal(lines)
    analyze_spread(lines)
    analyze_bot_performance(lines)
    analyze_direction_duration(lines)
    analyze_hours(lines)
    analyze_brain_states(lines)
    analyze_penny_ask(lines)
    analyze_bnb(lines)
    analyze_coin_ranking(lines)
    generate_recommendations(lines)

    lines.append("\n" + "═"*70)
    lines.append(f"  Analysis complete. Full data in agent_reports.db")
    lines.append("═"*70 + "\n")

    report = "\n".join(lines)
    print(report)
    with open(RPT_TXT, "w", encoding="utf-8") as f:
        f.write(report)

    # Update agent status
    c = sqlite3.connect(RPT_DB, timeout=60)
    penny_wr = 0
    try:
        row = c.execute("SELECT value FROM analyst_findings WHERE key='wr_up_penny'").fetchone()
        if row: penny_wr = row[0]
    except Exception:
        pass
    summary = f"Analyzed {total_resolved} resolved markets, penny WR={penny_wr:.0f}%"
    c.execute("INSERT OR REPLACE INTO agent_status VALUES ('analyst',?,?,?)",
              (datetime.now(timezone.utc).isoformat(), "OK", summary))
    c.commit(); c.close()
    print(f"[Analyst] Report saved → {RPT_TXT}")

if __name__ == "__main__":
    if WATCH:
        print(f"[Analyst] WATCH mode — re-running every {INTERVAL//60} minutes")
        while True:
            run_analysis()
            time.sleep(INTERVAL)
    else:
        run_analysis()
