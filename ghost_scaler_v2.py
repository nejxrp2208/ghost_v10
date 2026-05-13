"""
ghost_scaler_v2.py — Bayesian-Kelly Position Sizing
====================================================

Replaces the rule-based scaler with proper Bayesian inference on win rate.

Why this is better than v1:
- v1 uses point estimate of WR (e.g., "WR=10/100 = 10%") which is brittle on small samples
- v2 uses Beta posterior over WR — gives a CONFIDENCE INTERVAL
- Sizing uses the LOWER 5th percentile of true WR -> conservative Kelly
- Auto-shrinks position size when sample is small/uncertain
- Auto-expands when sample is large and edge is real
- Math is sound, no magic constants

Outputs (writes to agent_reports.db):
- param_recommendations: T1/T2/T3/T4 size adjustments
- scaler_recommendations: detailed log
- agent_status: heartbeat

Run: python ghost_scaler_v2.py [--watch] [--interval 900]
"""
import os, sys, time, json, argparse, sqlite3
from pathlib import Path
from datetime import datetime, timezone
from scipy import stats

HERE = Path(__file__).parent
TRADE_DB = HERE / 'crypto_ghost_PAPER.db'
RPT_DB   = HERE / 'agent_reports.db'

# --- CONFIG ------------------------------------------------------------------
BANKROLL = 200.0  # starting paper bankroll
KELLY_FRACTION = 0.25  # quarter-Kelly even at top — conservative
LOWER_PCTILE = 0.05  # use 5th percentile of WR posterior
MIN_TRADES_PER_TIER = 10  # below this, use minimum size
MAX_KELLY_FRACTION = 0.5  # never bet more than 50% of full Kelly
MIN_SIZE_USDC = 1.0
MAX_SIZE_USDC = 10.0  # paper-mode safety cap
PRIOR_ALPHA = 1.0  # Beta prior (uniform)
PRIOR_BETA  = 1.0

# --- HELPERS -----------------------------------------------------------------
def conn(path): return sqlite3.connect(str(path), timeout=60)

def heartbeat(status, summary):
    with conn(RPT_DB) as c:
        c.execute("""
            INSERT INTO agent_status (agent, last_run, status, summary)
            VALUES ('scaler_v2', ?, ?, ?)
            ON CONFLICT(agent) DO UPDATE SET
                last_run=excluded.last_run, status=excluded.status, summary=excluded.summary
        """, (datetime.now(timezone.utc).isoformat(), status, summary))
        c.commit()

def add_recommendation(param, current_val, rec_val, reason, priority=2):
    with conn(RPT_DB) as c:
        c.execute("""
            INSERT INTO param_recommendations (agent, ts, param, current_val, rec_val, reason, priority)
            VALUES ('scaler_v2', ?, ?, ?, ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), param,
              str(current_val), str(rec_val), reason, int(priority)))
        c.commit()

def fetch_tier_trades(tier):
    """Return (wins, losses, avg_entry_price) for a given tier — strategy='lottery' only."""
    with conn(TRADE_DB) as c:
        r = c.execute("""
            SELECT
                SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as w,
                SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) as l,
                AVG(entry_price) as avg_e
            FROM trades
            WHERE tier=? AND status IN ('won','lost') AND strategy='lottery'
        """, (tier,)).fetchone()
        if r is None:
            return 0, 0, None
        wins = r[0] or 0
        losses = r[1] or 0
        avg_entry = r[2]
        return wins, losses, avg_entry

def fetch_recent_trades(limit=50):
    """Recent trade outcomes for streak/regime detection."""
    with conn(TRADE_DB) as c:
        rows = c.execute("""
            SELECT status, pnl FROM trades
            WHERE status IN ('won','lost') AND strategy='lottery'
            ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    return rows

# --- BAYESIAN KELLY MATH -----------------------------------------------------
def bayesian_wr(wins, losses, prior_a=PRIOR_ALPHA, prior_b=PRIOR_BETA):
    """
    Returns (mean_wr, lower_5pct_wr, upper_95pct_wr) of Beta posterior.

    Uniform prior Beta(1,1) means with 0 data, mean=0.5, CI very wide.
    With 100 trades and 5 wins, posterior Beta(6, 96) — much tighter.
    """
    a = prior_a + wins
    b = prior_b + losses
    mean = a / (a + b)
    lower = stats.beta.ppf(LOWER_PCTILE, a, b)
    upper = stats.beta.ppf(1 - LOWER_PCTILE, a, b)
    return mean, lower, upper

def kelly_size(bankroll, p_lower, avg_entry):
    """
    Compute conservative position size.

    p_lower = lower bound of WR (we use 5th percentile to be safe)
    avg_entry = avg entry price (e.g., 0.03 = 3 cents = ~33x payout)

    Full Kelly: f* = (b*p - q) / b
    where b = (1/avg_entry - 1) is payout multiple
          p = win prob (we use p_lower)
          q = 1 - p
    """
    if avg_entry is None or avg_entry <= 0 or avg_entry >= 1:
        return MIN_SIZE_USDC, "no entry data"
    b = (1.0 / avg_entry) - 1.0  # payout multiple
    p = max(p_lower, 0.001)
    q = 1.0 - p
    f_star = (b * p - q) / b if b > 0 else 0
    if f_star <= 0:
        return MIN_SIZE_USDC, f"f*={f_star:.4f} <= 0 (negative EV at lower bound)"
    f_use = min(MAX_KELLY_FRACTION, max(0.0, f_star * KELLY_FRACTION))
    raw = bankroll * f_use
    sized = max(MIN_SIZE_USDC, min(MAX_SIZE_USDC, raw))
    return sized, f"Kelly f*={f_star:.4f} * frac={KELLY_FRACTION} = {f_use:.4f} -> ${raw:.2f} (clamped {sized:.2f})"

def regime_label(wins, losses):
    """Quick regime tag based on rolling stats."""
    n = wins + losses
    if n < 10:
        return "INSUFFICIENT", "Too few trades for confidence"
    recent = fetch_recent_trades(20)
    if not recent: return "NO_DATA", "no recent trades"
    streak = 0
    s0 = recent[0][0]
    for s,_ in recent:
        if s == s0: streak += 1
        else: break
    if s0 == 'lost' and streak >= 15:
        return "DANGER", f"loss streak {streak}"
    wr10 = sum(1 for s,_ in recent[:10] if s=='won') / min(10, len(recent[:10])) * 100
    if wr10 == 0:
        return "COLD", "no wins in last 10"
    if wr10 >= 15:
        return "HOT", f"recent WR10={wr10:.1f}%"
    return "NORMAL", f"WR10={wr10:.1f}%"

# --- MAIN --------------------------------------------------------------------
def run_once():
    started = datetime.now(timezone.utc).isoformat()
    log_lines = []
    log_lines.append("=" * 70)
    log_lines.append(f"  GHOST SCALER v2 (Bayesian-Kelly)  {started}")
    log_lines.append("=" * 70)

    # Realized PnL -> bankroll
    with conn(TRADE_DB) as c:
        pnl = c.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status IN ('won','lost') AND strategy='lottery'").fetchone()[0]
        spent = c.execute("SELECT COALESCE(SUM(size_usdc),0) FROM trades WHERE status='open' AND strategy='lottery'").fetchone()[0]
    portfolio = BANKROLL + pnl
    available = portfolio - spent
    log_lines.append(f"  Bankroll start:    ${BANKROLL:.2f}")
    log_lines.append(f"  Realized P&L:      ${pnl:+.2f}")
    log_lines.append(f"  Portfolio now:     ${portfolio:.2f}")
    log_lines.append(f"  Open positions:    ${spent:.2f}")
    log_lines.append(f"  Available:         ${available:.2f}")

    # Regime
    total_w = total_l = 0
    for t in [1,2,3,4]:
        w,l,_ = fetch_tier_trades(t)
        total_w += w; total_l += l
    regime, reason = regime_label(total_w, total_l)
    log_lines.append(f"\n  Regime: {regime}  ({reason})")

    log_lines.append("\n" + "-" * 70)
    log_lines.append("  PER-TIER BAYESIAN-KELLY SIZING")
    log_lines.append("-" * 70)
    log_lines.append(f"  {'tier':<5} {'n':>5} {'wins':>5} {'WR_mean':>8} {'WR_lo':>7} {'WR_hi':>7} {'recommended':>12}")

    recommendations = []
    for tier in [1, 2, 3, 4]:
        wins, losses, avg_entry = fetch_tier_trades(tier)
        n = wins + losses

        if n < MIN_TRADES_PER_TIER:
            new_size = MIN_SIZE_USDC
            note = f"only {n} trades — using min size"
            log_lines.append(f"  T{tier:<4} {n:>5} {wins:>5} {'  -':>7}% {'  -':>6}% {'  -':>6}%  ${new_size:>9.2f}  {note}")
            recommendations.append((tier, new_size, note))
            continue

        mean_wr, lower_wr, upper_wr = bayesian_wr(wins, losses)
        new_size, sizing_note = kelly_size(portfolio, lower_wr, avg_entry)

        # If regime is DANGER, halve the recommendation
        if regime == "DANGER":
            new_size = max(MIN_SIZE_USDC, new_size * 0.5)
            sizing_note += "; regime=DANGER halved"
        # If regime is COLD, use 75%
        elif regime == "COLD":
            new_size = max(MIN_SIZE_USDC, new_size * 0.75)
            sizing_note += "; regime=COLD x0.75"

        log_lines.append(f"  T{tier:<4} {n:>5} {wins:>5} {mean_wr*100:>7.2f}% {lower_wr*100:>6.2f}% {upper_wr*100:>6.2f}%  ${new_size:>9.2f}")
        log_lines.append(f"        avg_entry=${avg_entry:.3f}  {sizing_note}")
        recommendations.append((tier, new_size, sizing_note))

    # Write recommendations
    log_lines.append("\n" + "-" * 70)
    log_lines.append("  WRITING RECOMMENDATIONS")
    log_lines.append("-" * 70)
    for tier, size, note in recommendations:
        param = f"T{tier}_SIZE_USDC"
        # Get current from .env
        current = "?"
        try:
            for line in open(HERE / '.env', encoding='utf-8', errors='ignore'):
                if line.strip().startswith(f"{param}="):
                    current = line.strip().split('=',1)[1].strip()
                    break
        except: pass
        # Only recommend if change is meaningful (>10% diff or going to/from min)
        try:
            cur_f = float(current)
        except:
            cur_f = 0
        diff_pct = abs(size - cur_f) / max(cur_f, 1) * 100 if cur_f else 100
        if diff_pct > 10 or cur_f < 0.5:
            add_recommendation(param, current, f"{size:.2f}", note[:100], priority=2)
            log_lines.append(f"  REC: {param}: {current} -> {size:.2f}  ({note[:60]})")
        else:
            log_lines.append(f"  skip: {param}: {current} -> {size:.2f}  (diff only {diff_pct:.0f}%, not actionable)")

    # Write detailed log to scaler_recommendations
    with conn(RPT_DB) as c:
        for tier, size, note in recommendations:
            try:
                c.execute("""
                    INSERT INTO scaler_recommendations
                    (ts, tier, current_size, recommended_size, kelly_size, regime, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (datetime.now(timezone.utc).isoformat(), tier, 0, size, size, regime, note[:200]))
            except Exception as e:
                pass
        c.commit()

    summary_msg = f"v2 Bayesian | Regime={regime} | recs={len(recommendations)}"
    heartbeat("OK", summary_msg)
    print("\n".join(log_lines))
    print(f"\n[heartbeat] {summary_msg}")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--watch", action="store_true")
    p.add_argument("--interval", type=int, default=900, help="seconds between cycles")
    args = p.parse_args()

    if not args.watch:
        run_once()
        return

    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[ERROR] {e!r}")
            heartbeat("ERROR", str(e)[:200])
        time.sleep(max(60, args.interval))

if __name__ == '__main__':
    main()
