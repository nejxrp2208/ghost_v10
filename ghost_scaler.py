"""
Ghost Scaler — CryptoGhost Risk & Position Sizing Agent
========================================================
Uses Kelly Criterion math and rolling win-rate estimates to recommend
optimal position sizes for each tier. Tracks regime changes, drawdown,
and when to scale up vs pull back.

Logic:
  - Computes rolling WR over last 10, 20, 50 trades
  - Applies fractional Kelly (1/4 Kelly for safety) per tier
  - Detects regime: HOT (WR improving), COLD (WR falling), NEUTRAL
  - Recommends: normal size, scale-up, scale-down, or pause
  - Tracks max drawdown and recovery

Run:
  python ghost_scaler.py           # single analysis, exit
  python ghost_scaler.py --watch   # re-run every 15 min
  python ghost_scaler.py --apply   # write size recommendations to .env
"""

import sqlite3, os, sys, time, math
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SD        = os.path.dirname(os.path.abspath(__file__))
TRADE_DB  = os.path.join(SD, "crypto_ghost_PAPER.db")
RPT_DB    = os.path.join(SD, "agent_reports.db")
ENV_PATH  = os.path.join(SD, ".env")
RPT_TXT   = os.path.join(SD, "ghost_scaler_report.txt")

WATCH     = "--watch" in sys.argv
APPLY     = "--apply" in sys.argv
INTERVAL  = 900  # 15 minutes

# Default sizes from scanner config
DEFAULT_SIZES = {1: 5.0, 2: 3.0, 3: 3.0, 4: 2.0}
BANKROLL      = 200.0  # PORTFOLIO_SIZE — can read from .env
KELLY_FRACTION = 0.25  # fractional Kelly — conservative
MIN_SIZE      = 1.0
MAX_SIZE      = {1: 10.0, 2: 5.0, 3: 5.0, 4: 3.0}

# ── DB helpers ──────────────────────────────────────────────────────────────────
def q(db, sql, params=()):
    if not os.path.exists(db): return []
    try:
        c = sqlite3.connect(db, timeout=60)
        r = c.execute(sql, params).fetchall()
        c.close(); return r
    except Exception: return []

def qone(db, sql, params=()):
    r = q(db, sql, params)
    return r[0][0] if r else 0

def init_report_db():
    c = sqlite3.connect(RPT_DB, timeout=60)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS scaler_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, tier INTEGER,
            current_size REAL, recommended_size REAL,
            kelly_size REAL, regime TEXT, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_status (
            agent TEXT PRIMARY KEY, last_run TEXT, status TEXT, summary TEXT
        );
    """)
    c.commit(); c.close()

# ── Kelly Criterion ────────────────────────────────────────────────────────────
def kelly_size(win_rate, avg_payout_ratio, bankroll):
    """
    Kelly: f = (b*p - q) / b
    b = net odds (payout_ratio = (1-entry)/entry, i.e. what you win per $1 bet)
    p = win probability, q = 1-p
    Returns fractional Kelly bet size in dollars.
    """
    if win_rate <= 0 or avg_payout_ratio <= 0:
        return 0.0
    p = win_rate / 100
    q = 1 - p
    b = avg_payout_ratio
    kelly_f = (b * p - q) / b
    if kelly_f <= 0:
        return 0.0
    frac_kelly = kelly_f * KELLY_FRACTION * bankroll
    return round(max(MIN_SIZE, frac_kelly), 2)

# ── Regime detection ────────────────────────────────────────────────────────────
def detect_regime(wr10, wr20, wr50, loss_streak):
    """
    HOT    = short-term WR improving vs long-term
    COLD   = short-term WR falling vs long-term
    DANGER = loss streak threatening
    NEUTRAL = stable
    """
    if loss_streak >= 15:
        return "DANGER", "Loss streak ≥ 15 — scale back or pause"
    if wr10 >= wr20 * 1.15 and wr10 >= wr50 * 1.10:
        return "HOT", f"WR10={wr10:.1f}% trending up vs WR50={wr50:.1f}%"
    if wr10 <= wr20 * 0.85 and wr20 <= wr50 * 0.90:
        return "COLD", f"WR10={wr10:.1f}% falling vs WR50={wr50:.1f}%"
    return "NEUTRAL", f"Stable WR: 10={wr10:.1f}% 20={wr20:.1f}% 50={wr50:.1f}%"

# ── Size recommendation ─────────────────────────────────────────────────────────
def recommend_size(tier, wr, payout_ratio, regime, bankroll):
    kelly = kelly_size(wr, payout_ratio, bankroll)
    default = DEFAULT_SIZES[tier]
    max_s   = MAX_SIZE[tier]

    if regime == "DANGER":
        rec = MIN_SIZE
        reason = "Danger regime — minimum size to preserve capital"
    elif regime == "COLD":
        rec = max(MIN_SIZE, default * 0.7)
        reason = "Cold regime — reducing size 30%"
    elif regime == "HOT" and kelly > default:
        rec = min(max_s, kelly)
        reason = f"Hot regime — Kelly suggests ${kelly:.2f}"
    elif wr > 0:
        rec = max(MIN_SIZE, min(kelly if kelly > 0 else default, max_s))
        reason = f"Kelly fraction: ${kelly:.2f}"
    else:
        rec = default
        reason = "Insufficient data — using default"

    return round(rec, 2), kelly, reason

# ── Main analysis ───────────────────────────────────────────────────────────────
def run_scaling_analysis():
    init_report_db()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines   = []
    lines.append("╔" + "═"*70 + "╗")
    lines.append("║     GHOST SCALER — Position Sizing & Risk Management             ║")
    lines.append(f"║     {now_str:<67}║")
    lines.append("╚" + "═"*70 + "╝")

    if not os.path.exists(TRADE_DB):
        lines.append("\n  Trade DB not found. Start the bot first.")
        print("\n".join(lines))
        return

    # Overall stats — scoped to lottery strategy only (v2 has its own analyst)
    total_pnl  = qone(TRADE_DB, "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status IN ('won','lost') AND strategy='lottery'")
    portfolio  = BANKROLL + total_pnl
    spent      = qone(TRADE_DB, "SELECT COALESCE(SUM(size_usdc),0) FROM trades WHERE status='open' AND strategy='lottery'")
    avail      = portfolio - spent

    lines.append(f"\n  Starting bankroll   : ${BANKROLL:.2f}")
    lines.append(f"  Realized P&L        : ${total_pnl:+.2f}")
    lines.append(f"  Current portfolio   : ${portfolio:.2f}")
    lines.append(f"  Tied in open trades : ${spent:.2f}")
    lines.append(f"  Available capital   : ${avail:.2f}")

    # Drawdown
    equity_rows = q(TRADE_DB, """
        SELECT pnl FROM trades WHERE status IN ('won','lost') AND strategy='lottery' ORDER BY id ASC
    """)
    if equity_rows:
        running = BANKROLL
        peak    = BANKROLL
        max_dd  = 0.0
        for (pnl,) in equity_rows:
            running += (pnl or 0)
            peak     = max(peak, running)
            dd       = (peak - running) / peak * 100
            max_dd   = max(max_dd, dd)
        current_dd = (peak - running) / peak * 100 if peak > 0 else 0
        lines.append(f"\n  Max drawdown        : {max_dd:.1f}%")
        lines.append(f"  Current drawdown    : {current_dd:.1f}%")
        if current_dd >= 20:
            lines.append(f"  ⚠️  Drawdown > 20% — consider pausing or reducing sizes")

    # Rolling WRs and streaks
    recent = [r[0] for r in q(TRADE_DB,
        "SELECT status FROM trades WHERE status IN ('won','lost') AND strategy='lottery' ORDER BY id DESC LIMIT 50")]
    wr10 = (sum(1 for s in recent[:10] if s=="won") / min(10, len(recent[:10])) * 100) if recent else 0
    wr20 = (sum(1 for s in recent[:20] if s=="won") / min(20, len(recent[:20])) * 100) if recent else 0
    wr50 = (sum(1 for s in recent[:50] if s=="won") / min(50, len(recent[:50])) * 100) if recent else 0

    streak_type, streak_len = "—", 0
    if recent:
        streak_type = recent[0]
        for s in recent:
            if s == streak_type: streak_len += 1
            else: break

    regime, regime_desc = detect_regime(wr10, wr20, wr50,
                                        streak_len if streak_type == "lost" else 0)

    lines.append(f"\n  Rolling WR (10/20/50): {wr10:.1f}% / {wr20:.1f}% / {wr50:.1f}%")
    lines.append(f"  Current streak      : {streak_type} × {streak_len}")
    lines.append(f"  Regime              : {regime} — {regime_desc}")

    # Per-tier analysis
    lines.append(f"\n" + "═"*70)
    lines.append("  PER-TIER SIZING RECOMMENDATIONS")
    lines.append("═"*70)
    lines.append(f"\n  {'Tier':<6} {'Trades':>7} {'WR%':>7} {'AvgAsk':>8} {'Breakeven':>11} "
                 f"{'Kelly$':>8} {'Current$':>10} {'Rec$':>8} {'Regime':>10}")
    lines.append("  " + "-"*85)

    env_updates = {}
    all_rec_lines = []

    for tier in [1, 2, 3, 4]:
        tier_rows = q(TRADE_DB, """
            SELECT status, entry_price, size_usdc
            FROM trades WHERE tier=? AND status IN ('won','lost') AND strategy='lottery'
        """, (tier,))
        if not tier_rows:
            lines.append(f"  T{tier}    {'0':>7}  {'—':>7}  {'—':>8}  {'—':>11}  {'—':>8}  "
                         f"${DEFAULT_SIZES[tier]:.1f}{'':>7}  No data yet")
            continue

        n    = len(tier_rows)
        wins = sum(1 for s, _, _ in tier_rows if s == "won")
        wr   = wins / n * 100 if n > 0 else 0
        avg_entry = sum(e for _, e, _ in tier_rows if e) / n if n else 0.05
        avg_size  = sum(s for _, _, s in tier_rows if s) / n if n else DEFAULT_SIZES[tier]
        payout_ratio = (1 - avg_entry) / avg_entry if avg_entry else 0
        be   = round(avg_entry / (1 - avg_entry) * 100, 1) if avg_entry else 100

        rec_size, kelly, reason = recommend_size(tier, wr, payout_ratio, regime, avail)
        cur_size = DEFAULT_SIZES[tier]

        lines.append(f"  T{tier}    {n:>7,}  {wr:>6.1f}%  ${avg_entry:>6.4f}  {be:>9.1f}%  "
                     f"${kelly:>6.2f}  ${cur_size:>8.2f}  ${rec_size:>6.2f}  {regime:>10}")

        if abs(rec_size - cur_size) >= 0.25:
            change = "↑ scale up" if rec_size > cur_size else "↓ scale down"
            all_rec_lines.append(f"  T{tier}: ${cur_size:.2f} → ${rec_size:.2f}  {change}  ({reason})")
            env_updates[f"T{tier}_SIZE_USDC"] = str(rec_size)

        # Save to DB — both scaler-specific table and shared param_recommendations
        c = sqlite3.connect(RPT_DB, timeout=60)
        c.execute("DELETE FROM scaler_recommendations WHERE tier=?", (tier,))
        c.execute("""INSERT INTO scaler_recommendations
                     (ts, tier, current_size, recommended_size, kelly_size, regime, reason)
                     VALUES (?,?,?,?,?,?,?)""",
                  (datetime.now(timezone.utc).isoformat(), tier,
                   cur_size, rec_size, kelly, regime, reason))
        # Also publish to shared param_recommendations so Command picks it up
        param_name = f"T{tier}_SIZE_USDC"
        c.execute("DELETE FROM param_recommendations WHERE agent='scaler' AND param=?", (param_name,))
        if abs(rec_size - cur_size) >= 0.25:
            priority = 2 if abs(rec_size - cur_size) >= 1.0 else 3
            c.execute("INSERT INTO param_recommendations "
                      "(agent,ts,param,current_val,rec_val,reason,priority) "
                      "VALUES ('scaler',?,?,?,?,?,?)",
                      (datetime.now(timezone.utc).isoformat(), param_name,
                       f"{cur_size:.2f}", f"{rec_size:.2f}",
                       f"Kelly sizing: regime={regime}, WR={wr:.1f}%, payout={payout_ratio:.1f}x. {reason}",
                       priority))
        c.commit(); c.close()

    lines.append(f"\n  RECOMMENDED CHANGES:")
    if all_rec_lines:
        lines.extend(all_rec_lines)
    else:
        lines.append("  No size changes recommended — current sizing within Kelly bounds.")

    # Pause recommendation
    lines.append(f"\n  RISK CONTROLS:")
    if streak_len >= 15 and streak_type == "lost":
        lines.append(f"  ⛔ PAUSE: {streak_len} consecutive losses — something may have changed in the market")
    elif current_dd >= 25 if equity_rows else False:
        lines.append(f"  ⛔ PAUSE: Drawdown > 25% — protect remaining capital")
    elif wr10 < 2 and len(recent) >= 10:
        lines.append(f"  ⚠️  WARNING: WR10={wr10:.1f}% — extremely low short-term performance")
    else:
        lines.append(f"  ✅ Risk levels acceptable — continue trading")

    lines.append("\n" + "═"*70 + "\n")
    report = "\n".join(lines)
    print(report)
    with open(RPT_TXT, "w", encoding="utf-8") as f:
        f.write(report)

    if APPLY and env_updates and os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r") as f:
            env_lines = f.readlines()
        new_env = []
        updated = set()
        for line in env_lines:
            k = line.split("=")[0].strip()
            if k in env_updates:
                new_env.append(f"{k}={env_updates[k]}\n")
                updated.add(k)
                print(f"[Scaler] .env: {k}={env_updates[k]}")
            else:
                new_env.append(line)
        for k, v in env_updates.items():
            if k not in updated:
                new_env.append(f"{k}={v}\n")
        with open(ENV_PATH, "w") as f:
            f.writelines(new_env)
        print("[Scaler] .env updated. Restart scanner to apply.")

    c = sqlite3.connect(RPT_DB, timeout=60)
    c.execute("INSERT OR REPLACE INTO agent_status VALUES ('scaler',?,?,?)",
              (datetime.now(timezone.utc).isoformat(), "OK",
               f"Regime={regime} WR10={wr10:.1f}% WR50={wr50:.1f}%"))
    c.commit(); c.close()
    print(f"[Scaler] Report saved → {RPT_TXT}")

if __name__ == "__main__":
    if WATCH:
        print(f"[Scaler] WATCH mode — re-running every {INTERVAL//60} minutes")
        while True:
            run_scaling_analysis()
            time.sleep(INTERVAL)
    else:
        run_scaling_analysis()
