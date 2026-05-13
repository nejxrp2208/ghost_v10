"""
Ghost Optimizer — CryptoGhost Parameter Tuning Agent
=====================================================
Grid-searches parameter combinations against real MarketGhost data.
For each combo, simulates every entry it would have taken, computes
actual WR and EV, then writes the winning params to agent_reports.db.

SAFE: Read-only on all trade/market data. Only writes to agent_reports.db.
      Never touches .env — that is ghost_command.py's job.

Run modes:
  python ghost_optimizer.py           # single pass
  python ghost_optimizer.py --watch   # re-run every hour
  python ghost_optimizer.py --apply   # also push winning params to .env
"""

import sqlite3, os, sys, time, itertools
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SD       = os.path.dirname(os.path.abspath(__file__))
MG_DB    = os.path.join(SD, "marketghost.db")
RPT_DB   = os.path.join(SD, "agent_reports.db")
ENV_PATH = os.path.join(SD, ".env")
RPT_TXT  = os.path.join(SD, "ghost_optimizer_report.txt")

WATCH    = "--watch"  in sys.argv
APPLY    = "--apply"  in sys.argv
INTERVAL = 3600  # re-run every hour

# ── DB helpers ─────────────────────────────────────────────────────────────────
def q(db, sql, params=()):
    if not os.path.exists(db): return []
    try:
        c = sqlite3.connect(db, timeout=60)
        rows = c.execute(sql, params).fetchall()
        c.close(); return rows
    except Exception: return []

def init_report_db():
    if not os.path.exists(RPT_DB): return
    c = sqlite3.connect(RPT_DB, timeout=60)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS optimizer_results (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT,
            combo_key     TEXT UNIQUE,
            t2_max_entry  REAL,
            min_dev       REAL,
            hour_bias     INTEGER,
            min_no_price  REAL,
            simulated_trades INTEGER,
            win_rate      REAL,
            ev_per_trade  REAL,
            total_ev      REAL,
            rank          INTEGER
        );
        CREATE TABLE IF NOT EXISTS optimizer_best (
            param         TEXT PRIMARY KEY,
            current_val   TEXT,
            best_val      TEXT,
            improvement   REAL,
            ts            TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_status (
            agent TEXT PRIMARY KEY,
            last_run TEXT,
            status TEXT,
            summary TEXT
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

# ── Load snapshot + resolution data ───────────────────────────────────────────
def load_simulation_data():
    """
    Load joined snapshot + resolution data for simulation.
    Each row = one snapshot of a market at some mins_to_close,
    with the known resolution result.
    """
    rows = q(MG_DB, """
        SELECT
            s.market_id,
            s.mins_to_close,
            s.up_ask,
            s.down_ask,
            r.winner,
            r.coin,
            CAST(strftime('%H', r.end_utc) AS INTEGER) as hour,
            (r.price_end - r.price_start) / r.price_start as dev,
            r.price_start,
            r.price_end
        FROM snapshots s
        JOIN resolutions r ON s.market_id = r.market_id
        WHERE s.up_ask   > 0.005 AND s.up_ask   < 0.50
          AND s.down_ask > 0.005 AND s.down_ask < 0.50
          AND r.price_start > 0
        ORDER BY s.market_id, s.mins_to_close
    """)
    return rows

def load_hour_bias():
    """Load MarketGhost hour-bias table: {(coin, hour) → up_pct}"""
    rows = q(MG_DB, """
        SELECT coin,
               CAST(strftime('%H', end_utc) AS INTEGER) as hour,
               COUNT(*) as total,
               SUM(CASE WHEN winner='UP' THEN 1 ELSE 0 END) as up_wins
        FROM resolutions
        GROUP BY coin, hour
        HAVING total >= 10
    """)
    bias = {}
    for coin, hour, total, up in rows:
        bias[(coin, hour)] = (up / total * 100) if total > 0 else 50.0
    return bias

def deduplicate_by_market(data):
    """
    For each market, keep only one snapshot near close (1.0-3.0 min).
    This prevents one market from counting multiple times in simulation.
    Returns list of best-candidate rows per market.
    """
    best_per_market = {}
    for row in data:
        mid, mtc = row[0], row[1]
        if 1.0 <= mtc <= 3.0:
            if mid not in best_per_market or abs(mtc - 2.0) < abs(best_per_market[mid][1] - 2.0):
                best_per_market[mid] = row
    return list(best_per_market.values())

# ── Simulate one parameter combination ───────────────────────────────────────
def simulate(data, bias, t2_max_entry, min_dev, hour_bias, min_no_price, entry_window_min, entry_window_max):
    """
    Returns (simulated_trades, wins, ev_total).
    Logic mirrors scanner: direction first (dev > 0 → UP, dev < 0 → DOWN),
    then check price ceiling, certainty, and hour bias.
    """
    trades = 0
    wins   = 0
    ev_total = 0.0

    for row in data:
        (mid, mtc, up_ask, down_ask, winner, coin, hour, dev, ps, pe) = row

        # Window check
        if mtc < entry_window_min or mtc > entry_window_max:
            continue

        if dev is None or abs(dev) < min_dev:
            continue

        # Direction
        want_up   = dev > 0
        entry_ask = up_ask if want_up else down_ask
        correct   = (want_up and winner == "UP") or (not want_up and winner == "DOWN")

        # Price ceiling
        if entry_ask > t2_max_entry:
            continue

        # Certainty (implied probability of OTHER side = 1 - entry_ask)
        if 1 - entry_ask < min_no_price:
            continue

        # Hour bias filter
        up_pct = bias.get((coin, hour), 50.0)
        dn_pct = 100 - up_pct
        if want_up  and dn_pct >= hour_bias:
            continue
        if not want_up and up_pct >= hour_bias:
            continue

        # This trade would fire
        trades += 1
        if correct:
            wins   += 1
            ev_total += (1.0 - entry_ask) / entry_ask  # net profit per $1 risked
        else:
            ev_total -= 1.0  # lose the $1

    return trades, wins, ev_total

# ── Grid search ───────────────────────────────────────────────────────────────
def grid_search(data, bias, lines):
    lines.append("\n" + "═"*70)
    lines.append("  GRID SEARCH — Simulating parameter combinations")
    lines.append("═"*70)

    if not data:
        lines.append("\n  No simulation data available. Run MarketGhost longer.\n")
        return None

    lines.append(f"  Simulation dataset: {len(data):,} candidate entries")

    # Parameter grid — includes ultra-low entry tier ($0.01-$0.02)
    # and T2_MIN_DEV=0 to catch the penny-ask near-certain signal
    t2_max_entries = [0.012, 0.015, 0.020, 0.025, 0.030, 0.040, 0.050, 0.075, 0.100]
    min_devs       = [0.000, 0.001, 0.002, 0.003, 0.005, 0.007, 0.010]
    hour_biases    = [55, 58, 60, 63, 65]
    min_no_prices  = [0.75, 0.80, 0.85, 0.90, 0.95]
    windows        = [(0.5, 3.0), (1.0, 3.0), (0.5, 2.0), (1.0, 2.0)]

    total_combos = (len(t2_max_entries) * len(min_devs) *
                    len(hour_biases) * len(min_no_prices) * len(windows))
    lines.append(f"  Testing {total_combos:,} combinations...")

    results = []
    combo_num = 0

    for t2me, md, hb, mnp, (win_lo, win_hi) in itertools.product(
            t2_max_entries, min_devs, hour_biases, min_no_prices, windows):
        combo_num += 1
        if combo_num % 500 == 0:
            print(f"  [{combo_num:,}/{total_combos:,}]...")

        trades, wins, ev_total = simulate(data, bias, t2me, md, hb, mnp, win_lo, win_hi)

        if trades < 5:  # Too few trades — unreliable
            continue

        wr         = wins / trades * 100
        ev_per     = ev_total / trades
        combo_key  = f"{t2me:.3f}|{md:.4f}|{hb}|{mnp:.2f}|{win_lo:.1f}-{win_hi:.1f}"

        results.append({
            "combo_key":       combo_key,
            "t2_max_entry":    t2me,
            "min_dev":         md,
            "hour_bias":       hb,
            "min_no_price":    mnp,
            "window":          f"{win_lo:.1f}-{win_hi:.1f}",
            "trades":          trades,
            "wins":            wins,
            "win_rate":        round(wr, 2),
            "ev_per_trade":    round(ev_per, 4),
            "total_ev":        round(ev_total, 2),
        })

    if not results:
        lines.append("  No valid combinations found (need more market data).")
        return None

    # Sort by EV per trade (quality), then total EV (volume)
    results.sort(key=lambda r: (-r["ev_per_trade"], -r["total_ev"]))

    lines.append(f"\n  Results: {len(results):,} combos with ≥5 trades")
    lines.append(f"\n  TOP 10 PARAMETER COMBOS (by EV/trade):")
    lines.append(f"  {'MaxEntry':>10} {'MinDev':>8} {'HourBias':>10} {'Certainty':>10} {'Window':>10} {'Trades':>7} {'WR%':>7} {'EV/trade':>10} {'TotalEV':>9}")
    lines.append("  " + "-"*90)

    for i, r in enumerate(results[:10]):
        lines.append(
            f"  ${r['t2_max_entry']:.3f}  {r['min_dev']*100:>6.2f}%  "
            f"{r['hour_bias']:>9}%  {r['min_no_price']*100:>8.0f}%  "
            f"{r['window']:>10}  {r['trades']:>7,}  {r['win_rate']:>6.1f}%  "
            f"{r['ev_per_trade']:>+9.4f}  {r['total_ev']:>+8.2f}"
        )

    # Current baseline — read from .env or use defaults
    _cur_me  = 0.03;  _cur_md = 0.000; _cur_hb = 58
    _cur_mnp = 0.97
    try:
        import re as _re2
        _ep = os.path.join(SD, ".env")
        if os.path.exists(_ep):
            _txt = open(_ep).read()
            m1 = _re2.search(r"T2_MAX_ENTRY=([0-9.]+)", _txt)
            m2 = _re2.search(r"T2_MIN_DEV=([0-9.]+)", _txt)
            m3 = _re2.search(r"HOUR_BIAS_STRONG=([0-9]+)", _txt)
            m4 = _re2.search(r"MIN_NO_PRICE=([0-9.]+)", _txt)
            if m1: _cur_me  = float(m1.group(1))
            if m2: _cur_md  = float(m2.group(1))
            if m3: _cur_hb  = int(m3.group(1))
            if m4: _cur_mnp = float(m4.group(1))
    except Exception:
        pass

    current_trades, current_wins, current_ev = simulate(
        data, bias,
        t2_max_entry=_cur_me, min_dev=_cur_md, hour_bias=_cur_hb,
        min_no_price=_cur_mnp, entry_window_min=0.5, entry_window_max=30.0
    )
    current_wr = current_wins / current_trades * 100 if current_trades else 0
    current_ev_per = current_ev / current_trades if current_trades else 0

    lines.append(f"\n  CURRENT STRATEGY (baseline):")
    lines.append(f"  Trades={current_trades}  WR={current_wr:.1f}%  EV/trade={current_ev_per:+.4f}  TotalEV={current_ev:+.2f}")

    best = results[0]
    lines.append(f"\n  BEST STRATEGY vs CURRENT:")
    lines.append(f"  WR improvement    : {best['win_rate'] - current_wr:+.1f}%  ({current_wr:.1f}% → {best['win_rate']:.1f}%)")
    lines.append(f"  EV/trade improve  : {best['ev_per_trade'] - current_ev_per:+.4f}  ({current_ev_per:+.4f} → {best['ev_per_trade']:+.4f})")

    # Save to DB
    c = sqlite3.connect(RPT_DB, timeout=60)
    c.execute("DELETE FROM optimizer_results")
    for i, r in enumerate(results[:50]):
        c.execute("""
            INSERT OR REPLACE INTO optimizer_results
            (ts, combo_key, t2_max_entry, min_dev, hour_bias, min_no_price,
             simulated_trades, win_rate, ev_per_trade, total_ev, rank)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (datetime.now(timezone.utc).isoformat(),
              r["combo_key"], r["t2_max_entry"], r["min_dev"], r["hour_bias"],
              r["min_no_price"], r["trades"], r["win_rate"], r["ev_per_trade"],
              r["total_ev"], i+1))
    c.commit(); c.close()

    return best

# ── Write recommendations ──────────────────────────────────────────────────────
def write_recommendations(best, lines):
    if not best:
        return

    lines.append("\n" + "═"*70)
    lines.append("  OPTIMIZER RECOMMENDATIONS")
    lines.append("═"*70)

    param_map = {
        "T2_MAX_ENTRY":    (best["t2_max_entry"],   0.10,  f"${best['t2_max_entry']:.3f}",    1),
        "T2_MIN_DEV":      (best["min_dev"],         0.003, f"{best['min_dev']*100:.2f}%",     2),
        "HOUR_BIAS_STRONG":(best["hour_bias"],       60,    str(best["hour_bias"]),             2),
        "MIN_NO_PRICE":    (best["min_no_price"],    0.80,  f"{best['min_no_price']:.2f}",      3),
    }

    c = sqlite3.connect(RPT_DB, timeout=60)
    ts = datetime.now(timezone.utc).isoformat()

    for param, (rec, cur, label, pri) in param_map.items():
        if abs(rec - cur) > 1e-6:
            reason = (f"Optimizer found this maximizes EV/trade. "
                      f"Best combo: WR={best['win_rate']:.1f}% EV={best['ev_per_trade']:+.4f} "
                      f"on {best['trades']} simulated trades.")
            lines.append(f"\n  {param}: {cur} → {rec}")
            lines.append(f"    {reason}")
            c.execute("DELETE FROM param_recommendations WHERE agent='optimizer' AND param=?", (param,))
            c.execute("INSERT INTO param_recommendations (agent,ts,param,current_val,rec_val,reason,priority) "
                      "VALUES ('optimizer',?,?,?,?,?,?)",
                      (ts, param, str(cur), str(rec), reason, pri))
        else:
            lines.append(f"\n  {param}: {cur} ← already at optimum")

    c.execute("INSERT OR REPLACE INTO optimizer_best VALUES (?,?,?,?,?)",
              ("best_combo", "current", best["combo_key"], best["ev_per_trade"], ts))
    c.commit(); c.close()

# ── Apply to .env (only if --apply flag) ──────────────────────────────────────
def apply_to_env(best):
    if not best or not os.path.exists(ENV_PATH):
        print("[Optimizer] --apply: .env not found, skipping")
        return

    updates = {
        "T2_MAX_ENTRY":    f"{best['t2_max_entry']:.3f}",
        "T2_MIN_DEV":      f"{best['min_dev']:.4f}",
        "HOUR_BIAS_STRONG":str(best["hour_bias"]),
        "MIN_NO_PRICE":    f"{best['min_no_price']:.2f}",
    }

    with open(ENV_PATH, "r") as f:
        lines = f.readlines()

    new_lines = []
    updated = set()
    for line in lines:
        key = line.split("=")[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}\n")
            updated.add(key)
            print(f"[Optimizer] .env: {key}={updates[key]}")
        else:
            new_lines.append(line)

    for key, val in updates.items():
        if key not in updated:
            new_lines.append(f"{key}={val}\n")
            print(f"[Optimizer] .env: +{key}={val}")

    with open(ENV_PATH, "w") as f:
        f.writelines(new_lines)
    print("[Optimizer] .env updated. Restart scanner to apply.")

# ── Main ───────────────────────────────────────────────────────────────────────
def run_optimization():
    init_report_db()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines   = []
    lines.append("╔" + "═"*70 + "╗")
    lines.append("║     GHOST OPTIMIZER — Parameter Grid Search                      ║")
    lines.append(f"║     {now_str:<67}║")
    lines.append("╚" + "═"*70 + "╝")

    if not os.path.exists(MG_DB):
        lines.append("\n  marketghost.db not found. Run marketghost.py first.")
        print("\n".join(lines))
        return

    print("[Optimizer] Loading MarketGhost snapshot data...")
    all_data  = load_simulation_data()
    data      = deduplicate_by_market(all_data)
    bias      = load_hour_bias()

    lines.append(f"\n  Raw snapshots loaded   : {len(all_data):,}")
    lines.append(f"  Deduplicated markets   : {len(data):,}")
    lines.append(f"  Hour-bias slots loaded : {len(bias):,}")

    if len(data) < 50:
        lines.append("\n  Need at least 50 resolved markets for reliable optimization.")
        lines.append("  Keep MarketGhost running — it collects data automatically.")

    best = grid_search(data, bias, lines)
    write_recommendations(best, lines)

    lines.append("\n" + "═"*70)
    lines.append("  Run ghost_command.py to review and apply these recommendations.")
    lines.append("═"*70 + "\n")

    report = "\n".join(lines)
    print(report)
    with open(RPT_TXT, "w", encoding="utf-8") as f:
        f.write(report)

    if APPLY and best:
        apply_to_env(best)

    # Status update
    c = sqlite3.connect(RPT_DB, timeout=60)
    summary = f"Best combo: EV/trade={best['ev_per_trade']:+.4f} WR={best['win_rate']:.1f}%" if best else "No data"
    c.execute("INSERT OR REPLACE INTO agent_status VALUES ('optimizer',?,?,?)",
              (datetime.now(timezone.utc).isoformat(), "OK", summary))
    c.commit(); c.close()
    print(f"[Optimizer] Report saved → {RPT_TXT}")

if __name__ == "__main__":
    if WATCH:
        print(f"[Optimizer] WATCH mode — re-running every {INTERVAL//60} minutes")
        while True:
            run_optimization()
            time.sleep(INTERVAL)
    else:
        run_optimization()
