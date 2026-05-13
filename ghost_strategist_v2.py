"""
ghost_strategist_v2.py — Backtester on Real Trade History
==========================================================

Replaces v1's rule-based A/B with a proper backtester that uses the bot's
ACTUAL trade history (PM-truth resolved) to find which filter combinations
would have been the most profitable.

Why trades, not marketghost:
- marketghost only snapshots every ~1 min; misses the brief liquid windows
- the lottery bot fills at $0.01-$0.03 in moments that don't show up in snapshots
- the trades table has 100% PM-truth resolution and real entry prices
- this is what we ACTUALLY traded — backtest reflects reality

Output:
- Lists the top N filter combinations by historical PnL
- Statistical significance test (binomial vs breakeven)
- Cross-validation: does the edge hold across time-folds?
- Recommendation written to param_recommendations IF an edge survives CV

Run: python ghost_strategist_v2.py [--watch] [--interval 3600]
"""
import os, sys, json, argparse, sqlite3, time, random
from pathlib import Path
from datetime import datetime, timezone
from itertools import product

HERE = Path(__file__).parent
TRADE_DB = HERE / 'crypto_ghost_PAPER.db'
RPT_DB   = HERE / 'agent_reports.db'

# ─── PARAM SEARCH SPACE ──────────────────────────────────────────────────────
# Realistic for the lottery strategy (entries at $0.01-$0.07)
PARAM_GRID = {
    'entry_max': [0.02, 0.03, 0.04, 0.05, 0.07],
    'entry_min': [0.005, 0.01, 0.015, 0.02],
    'side_filter': ['ANY', 'UP_ONLY', 'DOWN_ONLY'],
    'coins': [['BTC'], ['ETH'], ['BTC','ETH'],
              ['BTC','ETH','SOL','XRP','BNB']],
    'hours_filter': ['ALL', '07-11_UTC', '12-16_UTC', '17-21_UTC',
                     '22-02_UTC', '03-06_UTC'],
}

MIN_TRADES_FOR_SIGNIFICANCE = 20

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def conn(p): return sqlite3.connect(str(p), timeout=60)

def heartbeat(status, summary):
    with conn(RPT_DB) as c:
        c.execute("""
            INSERT INTO agent_status (agent, last_run, status, summary)
            VALUES ('strategist_v2', ?, ?, ?)
            ON CONFLICT(agent) DO UPDATE SET
                last_run=excluded.last_run, status=excluded.status, summary=excluded.summary
        """, (datetime.now(timezone.utc).isoformat(), status, summary))
        c.commit()

def log_finding(test_name, variant, samples, win_rate, ev, notes):
    with conn(RPT_DB) as c:
        c.execute("""
            INSERT INTO strategy_findings (ts, test_name, variant, samples, win_rate, ev, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), test_name, variant,
              int(samples), float(win_rate), float(ev), notes[:200]))
        c.commit()

def add_recommendation(param, current_val, rec_val, reason, priority=2):
    with conn(RPT_DB) as c:
        c.execute("""
            INSERT INTO param_recommendations (agent, ts, param, current_val, rec_val, reason, priority)
            VALUES ('strategist_v2', ?, ?, ?, ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), param,
              str(current_val), str(rec_val), reason, int(priority)))
        c.commit()

# ─── DATA LOAD ───────────────────────────────────────────────────────────────
def load_trades():
    """Pull every PM-truth-resolved lottery trade with all features."""
    with conn(TRADE_DB) as c:
        rows = c.execute("""
            SELECT id, ts, tier, coin, outcome, entry_price, size_usdc,
                   status, pnl, brain_state
            FROM trades
            WHERE status IN ('won','lost')
              AND strategy='lottery'
              AND (resolution_source='polymarket' OR resolution_source IS NULL)
            ORDER BY id ASC
        """).fetchall()
    out = []
    for r in rows:
        try:
            utc_hour = int(r[1][11:13]) if r[1] and len(r[1]) >= 13 else None
        except Exception:
            utc_hour = None
        out.append({
            'id': r[0], 'ts': r[1], 'tier': r[2], 'coin': r[3], 'side': r[4],
            'entry': float(r[5] or 0), 'size': float(r[6] or 0),
            'status': r[7], 'pnl': float(r[8] or 0),
            'brain': r[9], 'utc_hour': utc_hour,
        })
    return out

# ─── BACKTEST SIMULATION ─────────────────────────────────────────────────────
def hour_match(utc_hour, hf):
    if hf == 'ALL': return True
    if utc_hour is None: return False
    if hf == '07-11_UTC': return 7 <= utc_hour <= 11
    if hf == '12-16_UTC': return 12 <= utc_hour <= 16
    if hf == '17-21_UTC': return 17 <= utc_hour <= 21
    if hf == '22-02_UTC': return utc_hour >= 22 or utc_hour <= 2
    if hf == '03-06_UTC': return 3 <= utc_hour <= 6
    return True

def passes_filter(t, p):
    if t['coin'] not in p['coins']: return False
    if t['entry'] < p['entry_min'] or t['entry'] > p['entry_max']: return False
    if not hour_match(t['utc_hour'], p['hours_filter']): return False
    if p['side_filter'] == 'UP_ONLY' and t['side'] != 'UP': return False
    if p['side_filter'] == 'DOWN_ONLY' and t['side'] != 'DOWN': return False
    return True

def simulate(trades, params):
    """Returns (n, wins, total_pnl, ev_per_dollar)."""
    n = 0; w = 0; total_pnl = 0.0; total_stake = 0.0
    for t in trades:
        if not passes_filter(t, params): continue
        n += 1
        if t['status'] == 'won': w += 1
        total_pnl += t['pnl']
        total_stake += t['size']
    if n == 0:
        return 0, 0, 0.0, 0.0
    ev = total_pnl / total_stake if total_stake else 0
    return n, w, total_pnl, ev

def binomial_p(wins, n, breakeven_wr):
    """Probability of seeing wins or more out of n trials assuming WR=breakeven."""
    if n == 0: return 1.0
    try:
        from scipy.stats import binomtest
        return binomtest(wins, n, breakeven_wr, alternative='greater').pvalue
    except Exception:
        return 1.0

# ─── PARAMETER SEARCH ────────────────────────────────────────────────────────
def grid_search(trades, max_combos=2000):
    keys = list(PARAM_GRID.keys())
    grids = [PARAM_GRID[k] for k in keys]
    total = 1
    for g in grids: total *= len(g)
    print(f"[search] full grid = {total} combos")

    if total <= max_combos:
        combos = list(product(*grids))
    else:
        random.seed(42)
        combos = [tuple(random.choice(g) for g in grids) for _ in range(max_combos)]

    results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        if params['entry_min'] >= params['entry_max']: continue
        n, w, pnl, ev = simulate(trades, params)
        if n < MIN_TRADES_FOR_SIGNIFICANCE: continue
        # Breakeven WR ≈ avg entry price (simplified)
        avg_entry = (params['entry_min'] + params['entry_max']) / 2
        p_val = binomial_p(w, n, avg_entry)
        results.append({
            'params': params, 'n': n, 'wins': w, 'wr': w/n*100,
            'pnl': pnl, 'ev_per_dollar': ev, 'p_value': p_val,
        })
    return results

def cross_validate(trades, top_results, n_folds=3):
    """Time-split into folds; check edge consistency."""
    sorted_trades = sorted(trades, key=lambda t: t['id'])
    fold_size = len(sorted_trades) // n_folds
    folds = [sorted_trades[i*fold_size:(i+1)*fold_size] for i in range(n_folds)]
    cv = []
    for r in top_results:
        per_fold = []
        for fold in folds:
            n, w, pnl, ev = simulate(fold, r['params'])
            per_fold.append({'n': n, 'wins': w,
                             'wr': (w/n*100 if n else 0), 'pnl': pnl, 'ev': ev})
        # "Consistent" = all folds have n>=5 AND ev>=0
        positive = sum(1 for f in per_fold if f['n'] >= 5 and f['ev'] >= 0)
        cv.append({
            'params': r['params'], 'overall': r,
            'per_fold': per_fold, 'positive_folds': positive,
            'consistent': positive >= n_folds - 1,  # tolerate 1 weak fold
        })
    return cv

# ─── MAIN ────────────────────────────────────────────────────────────────────
def fmt_params(p):
    coins_s = ','.join(p['coins'])
    return (f"e=[{p['entry_min']},{p['entry_max']}] "
            f"side={p['side_filter']} {coins_s} {p['hours_filter']}")

def run_once():
    started = datetime.now(timezone.utc).isoformat()
    print("=" * 78)
    print(f"  GHOST STRATEGIST v2 (Backtest on Real Trades)  {started}")
    print("=" * 78)

    trades = load_trades()
    print(f"\n[load] {len(trades)} closed lottery trades from PM-truth data")
    if len(trades) < 50:
        msg = f"only {len(trades)} trades — need >=50 for backtest"
        print(f"  {msg}")
        heartbeat("INSUFFICIENT_DATA", msg)
        return

    # Baseline: no filter
    n_b, w_b, pnl_b, ev_b = simulate(trades, {
        'coins': ['BTC','ETH','SOL','XRP','BNB'],
        'entry_min': 0.0, 'entry_max': 1.0,
        'side_filter': 'ANY', 'hours_filter': 'ALL'
    })
    print(f"\n[baseline] no filter: n={n_b} WR={w_b/n_b*100:.1f}% PnL=${pnl_b:.2f} EV/$={ev_b:+.3f}")

    print(f"\n[search] running grid search...")
    started_search = time.time()
    results = grid_search(trades, max_combos=1500)
    print(f"[search] {len(results)} valid combos in {time.time()-started_search:.0f}s")

    if not results:
        msg = f"no combos passed min trade threshold of {MIN_TRADES_FOR_SIGNIFICANCE}"
        print(f"  {msg}")
        heartbeat("NO_RESULTS", msg)
        return

    # Top by PnL
    results.sort(key=lambda r: r['pnl'], reverse=True)
    print(f"\n[top 10 by PnL]")
    print(f"  {'rank':<5} {'n':>4} {'WR%':>6} {'PnL':>9} {'EV/$':>8} {'p-val':>7}  params")
    for i, r in enumerate(results[:10]):
        sig = '*' if r['p_value'] < 0.05 else ' '
        print(f"  #{i+1:<4}{sig} {r['n']:>4} {r['wr']:>5.1f}% ${r['pnl']:>7.2f} {r['ev_per_dollar']:>+7.3f} {r['p_value']:>6.3f}  {fmt_params(r['params'])}")

    # Cross-validation
    print(f"\n[cross-validation] testing top 5 across 3 time-folds...")
    cv_results = cross_validate(trades, results[:5], n_folds=3)
    print(f"  {'rank':<5} {'consistent':<11} {'pos/3':<6}  per-fold (n, WR%, EV/$)")
    for i, cv in enumerate(cv_results):
        cons = 'YES' if cv['consistent'] else 'NO'
        fold_summary = '  '.join(f"({f['n']},{f['wr']:.0f}%,{f['ev']:+.2f})" for f in cv['per_fold'])
        print(f"  #{i+1:<4} {cons:<11} {cv['positive_folds']}/3    {fold_summary}")

    # Pick the best CV-survivor (must have positive overall PnL)
    best = None
    for cv in cv_results:
        if cv['consistent'] and cv['overall']['pnl'] > 0:
            best = cv
            break

    if not best:
        msg = "no parameter combo had consistent edge across time folds"
        print(f"\n  RESULT: {msg}")
        log_finding('combined', 'no_consistent_winner', 0, 0, 0, msg)
        heartbeat("NO_CONSISTENT_EDGE", msg)
        return

    p = best['params']
    o = best['overall']
    print(f"\n  WINNER (passes CV): {fmt_params(p)}")
    print(f"  Stats: n={o['n']} WR={o['wr']:.1f}% PnL=${o['pnl']:.2f} EV/$={o['ev_per_dollar']:+.3f} p={o['p_value']:.3f}")

    log_finding('combined', f"WINNER: {fmt_params(p)}",
                o['n'], o['wr'], o['ev_per_dollar'],
                f"PnL=${o['pnl']:.2f} CV-consistent")

    # Recommendations IF the winner is meaningfully better than baseline
    if o['ev_per_dollar'] > ev_b * 1.5:  # 50% improvement vs baseline
        print(f"\n  Edge is meaningfully better than baseline (EV/$={o['ev_per_dollar']:+.3f} vs {ev_b:+.3f})")
        # Could write recs here... but leaving as informational for now
        # since we don't want to override the lottery scanner's filters automatically
        # without user review
        log_finding('combined', 'ACTIONABLE_EDGE_FOUND',
                    o['n'], o['wr'], o['ev_per_dollar'],
                    f"WINNER beats baseline: {fmt_params(p)}")
    else:
        print(f"\n  Edge marginal — not generating filter changes (baseline EV/$={ev_b:+.3f} already comparable)")

    summary = f"v2 | n={o['n']} top_pnl=${o['pnl']:.2f} top_ev={o['ev_per_dollar']:+.3f}/$ {fmt_params(p)[:40]}"
    heartbeat("OK", summary[:150])

def main():
    pa = argparse.ArgumentParser()
    pa.add_argument("--watch", action="store_true")
    pa.add_argument("--interval", type=int, default=3600)
    args = pa.parse_args()

    if not args.watch:
        run_once()
        return

    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[ERROR] {e!r}")
            heartbeat("ERROR", str(e)[:200])
        time.sleep(max(300, args.interval))

if __name__ == '__main__':
    main()
