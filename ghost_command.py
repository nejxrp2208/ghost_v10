"""
Ghost Command — CryptoGhost Master Orchestrator
===============================================
Fully autonomous. Runs all five specialist agents, aggregates their
findings, and immediately applies ALL recommendations to .env —
no confirmation, no countdown, no permission needed.

Run:
  python ghost_command.py              # one cycle, auto-apply all recs
  python ghost_command.py --watch      # loop: run all agents every hour (default)
  python ghost_command.py --status     # just print current agent status
  python ghost_command.py --observe    # read-only mode (no .env changes)

What it does:
  1. Runs ghost_analyst   (deep data analysis)
  2. Runs ghost_optimizer (parameter grid search)
  3. Runs ghost_strategist (signal research)
  4. Runs ghost_scaler    (position sizing)
  5. Aggregates ALL recommendations (all priority levels)
  6. Immediately applies every recommendation to .env
  7. Prints unified command dashboard
"""

import subprocess, sqlite3, os, sys, time, json
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SD         = os.path.dirname(os.path.abspath(__file__))
RPT_DB     = os.path.join(SD, "agent_reports.db")
TRADE_DB   = os.path.join(SD, "crypto_ghost_PAPER.db")
ENV_PATH   = os.path.join(SD, ".env")
CMD_RPT    = os.path.join(SD, "ghost_command_report.txt")

OBSERVE    = "--observe" in sys.argv          # read-only mode — pass this to skip .env writes
AUTO       = not OBSERVE                      # always auto unless --observe is given
WATCH      = "--watch"  in sys.argv
STATUS     = "--status" in sys.argv
INTERVAL   = 3600  # run all agents every hour

AGENTS = [
    ("ghost_analyst.py",    "Analyst",    "Deep data analysis"),
    ("ghost_optimizer.py",  "Optimizer",  "Parameter grid search"),
    ("ghost_strategist.py", "Strategist", "Signal research"),
    ("ghost_scaler.py",     "Scaler",     "Position sizing"),
]

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

def init_db():
    c = sqlite3.connect(RPT_DB, timeout=60)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS agent_status (
            agent TEXT PRIMARY KEY, last_run TEXT, status TEXT, summary TEXT
        );
        CREATE TABLE IF NOT EXISTS param_recommendations (
            agent TEXT, ts TEXT, param TEXT,
            current_val TEXT, rec_val TEXT, reason TEXT, priority INTEGER DEFAULT 5
        );
        CREATE TABLE IF NOT EXISTS command_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, action TEXT, detail TEXT
        );
    """)
    c.commit(); c.close()

def log_cmd(action, detail):
    c = sqlite3.connect(RPT_DB, timeout=60)
    c.execute("INSERT INTO command_log (ts, action, detail) VALUES (?,?,?)",
              (datetime.now(timezone.utc).isoformat(), action, detail))
    c.execute("DELETE FROM command_log WHERE id NOT IN (SELECT id FROM command_log ORDER BY id DESC LIMIT 200)")
    c.commit(); c.close()

# ── Run individual agent ────────────────────────────────────────────────────────
def run_agent(script, name, desc):
    path = os.path.join(SD, script)
    if not os.path.exists(path):
        print(f"  [{name}] Script not found: {script}")
        return False

    print(f"  [{name}] Running {desc}...")
    start = time.time()
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        result = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, timeout=300,
            cwd=SD, encoding="utf-8", errors="replace", env=env
        )
        elapsed = int(time.time() - start)
        if result.returncode == 0:
            print(f"  [{name}] Done ({elapsed}s)")
            return True
        else:
            print(f"  [{name}] Error (exit {result.returncode})")
            if result.stderr:
                for line in result.stderr.strip().split("\n")[-5:]:
                    print(f"           {line}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  [{name}] Timed out after 300s")
        return False
    except Exception as e:
        print(f"  [{name}] {e}")
        return False

# ── Gather all recommendations ──────────────────────────────────────────────────
def gather_recommendations():
    """Read all param recommendations from agent_reports.db, sorted by priority."""
    rows = q(RPT_DB, """
        SELECT agent, param, current_val, rec_val, reason, priority, ts
        FROM param_recommendations
        ORDER BY priority ASC, ts DESC
    """)
    # Deduplicate: keep highest-priority rec per param
    seen = {}
    for agent, param, cur, rec, reason, pri, ts in rows:
        if param not in seen or pri < seen[param][5]:
            seen[param] = (agent, param, cur, rec, reason, pri, ts)
    return list(seen.values())

# ── Apply recommendations to .env ───────────────────────────────────────────────
def apply_recommendations(recs, auto=False):
    if not recs:
        print("\n  No recommendations to apply.")
        return

    print(f"\n  {'='*68}")
    print(f"  APPLYING {len(recs)} RECOMMENDATIONS (autonomous mode):")
    for agent, param, cur, rec, reason, pri, ts in recs:
        pri_label = "CRIT" if pri <= 1 else ("HIGH" if pri <= 2 else "MED")
        print(f"    [{agent:>12}] [{pri_label}] {param}: {cur} → {rec}")
        print(f"               {reason[:70]}")
    print(f"  {'='*68}")

    if not auto:
        print("\n  Run without --observe flag to apply these changes automatically.")
        return

    if not os.path.exists(ENV_PATH):
        print(f"\n  .env not found at {ENV_PATH} — cannot apply changes.")
        return

    print("\n  Applying immediately...")
    updates = {r[1]: r[3] for r in recs}

    with open(ENV_PATH, "r") as f:
        lines = f.readlines()
    new_lines = []
    applied = set()
    for line in lines:
        key = line.split("=")[0].strip()
        if key in updates:
            old_val = line.strip().split("=", 1)[1] if "=" in line else "?"
            new_lines.append(f"{key}={updates[key]}\n")
            applied.add(key)
            print(f"  {key}: {old_val} → {updates[key]}")
            log_cmd("apply_param", f"{key}: {old_val} → {updates[key]}")
        else:
            new_lines.append(line)
    for k, v in updates.items():
        if k not in applied:
            new_lines.append(f"{k}={v}\n")
            print(f"  {k}: (new) = {v}")
            log_cmd("apply_param", f"{k}: new = {v}")

    with open(ENV_PATH, "w") as f:
        f.writelines(new_lines)
    print(f"\n  .env updated autonomously. {len(updates)} parameter(s) changed.")
    print(f"  Restart the scanner to apply changes.")

# ── Print unified dashboard ──────────────────────────────────────────────────────
def print_command_dashboard(recs):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n")
    print("╔" + "═"*70 + "╗")
    print("║     GHOST COMMAND — Unified Agent Intelligence Dashboard           ║")
    print(f"║     {ts:<67}║")
    print("╠" + "═"*70 + "╣")

    # Agent statuses
    print("║  AGENT STATUS:                                                       ║")
    agent_names = ["analyst", "optimizer", "strategist", "scaler", "monitor"]
    status_rows = q(RPT_DB, """
        SELECT agent, last_run, status, summary FROM agent_status
        ORDER BY agent
    """)
    status_map = {r[0]: r for r in status_rows}
    for name in agent_names:
        row = status_map.get(name)
        if row:
            _, last_run, status, summary = row
            icon = "OK" if status == "OK" else "!!"
            age_str = ""
            try:
                dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                age = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
                age_str = f"{age}m ago"
            except Exception:
                age_str = "unknown"
            summ = (summary or "")[:42]
            print(f"║    [{icon}] {name:<12} {age_str:<10} {summ:<38}║")
        else:
            print(f"║    [  ] {name:<12} {'not run yet':<10} {'':>38}║")

    print("╠" + "═"*70 + "╣")

    # Bot stats
    total  = qone(TRADE_DB, "SELECT COUNT(*) FROM trades WHERE status IN ('won','lost')")
    wins   = qone(TRADE_DB, "SELECT COUNT(*) FROM trades WHERE status='won'")
    pnl    = qone(TRADE_DB, "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status IN ('won','lost')")
    wr     = round(wins/total*100, 1) if total else 0

    print(f"║  BOT PERFORMANCE:  {wins}W/{total-wins}L  WR={wr:.1f}%  P&L=${pnl:+.2f}" +
          " "*20 + "║")

    print("╠" + "═"*70 + "╣")
    print("║  RECOMMENDATIONS:                                                    ║")
    if recs:
        for agent, param, cur, rec, reason, pri, ts_r in recs[:8]:
            pri_label = "[CRIT]" if pri <= 1 else ("[HIGH]" if pri <= 2 else "[MED ]")
            line = f"  {pri_label} {param:<22} {str(cur):<8} → {str(rec):<8}"
            print(f"║{line:<70}║")
    else:
        print("║    No recommendations yet — run agents first                        ║")

    print("╠" + "═"*70 + "╣")
    mode_str = "OBSERVE (read-only — pass no flag to auto-apply)" if OBSERVE else "AUTO (applying all recommendations)"
    print(f"║  MODE: {mode_str:<63}║")
    print("╚" + "═"*70 + "╝\n")

    # Write report
    lines_out = [
        "Ghost Command Report",
        f"Generated: {ts}",
        f"Mode: {mode_str}",
        f"Bot: {wins}W/{total-wins}L  WR={wr:.1f}%  P&L=${pnl:+.2f}",
        "",
        "Recommendations:"
    ]
    for agent, param, cur, rec, reason, pri, ts_r in recs:
        lines_out.append(f"  [{agent}] {param}: {cur} → {rec}  (priority {pri})")
        lines_out.append(f"    {reason}")
    with open(CMD_RPT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out))

# ── Status-only mode ─────────────────────────────────────────────────────────────
def print_status_only():
    init_db()
    rows = q(RPT_DB, "SELECT agent, last_run, status, summary FROM agent_status ORDER BY agent")
    print("\n  GHOST AGENT TEAM STATUS:")
    print("  " + "-"*60)
    for agent, last_run, status, summary in rows:
        age_str = "unknown"
        try:
            dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            age = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
            age_str = f"{age}m ago"
        except Exception:
            pass
        icon = "[OK]" if status == "OK" else "[!!]"
        print(f"  {icon} {agent:<15} {age_str:<12} {summary[:45]}")
    print()

    # Recent alerts
    alerts = q(RPT_DB, "SELECT ts, level, category, message FROM monitor_alerts ORDER BY id DESC LIMIT 5")
    if alerts:
        print("  RECENT ALERTS:")
        for ats, lv, cat, msg in alerts:
            print(f"  [{lv:<8}] {ats[:16]} {cat}: {msg[:50]}")
        print()

# ── Main run cycle ───────────────────────────────────────────────────────────────
def run_all():
    init_db()
    print("\n" + "═"*72)
    print("  GHOST COMMAND — Starting agent run cycle")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  Mode: {'AUTO' if AUTO else 'OBSERVE'}")
    print("═"*72)

    log_cmd("cycle_start", f"mode={'auto' if AUTO else 'observe'}")

    success_count = 0
    for script, name, desc in AGENTS:
        if run_agent(script, name, desc):
            success_count += 1

    print(f"\n  Agents completed: {success_count}/{len(AGENTS)}")

    recs = gather_recommendations()
    print_command_dashboard(recs)
    apply_recommendations(recs, auto=AUTO)

    log_cmd("cycle_end", f"recs={len(recs)} applied={AUTO}")
    print(f"\n  Report saved → {CMD_RPT}")

    # Write own status so the dashboard's CMD node shows OK
    try:
        c = sqlite3.connect(RPT_DB, timeout=60)
        c.execute("CREATE TABLE IF NOT EXISTS agent_status "
                  "(agent TEXT PRIMARY KEY, last_run TEXT, status TEXT, summary TEXT)")
        mode = "OBSERVE" if OBSERVE else "AUTO"
        summary = f"{success_count}/{len(AGENTS)} agents OK | {len(recs)} recs | mode={mode}"
        c.execute("INSERT OR REPLACE INTO agent_status VALUES ('command',?,?,?)",
                  (datetime.now(timezone.utc).isoformat(), "OK", summary[:120]))
        c.commit(); c.close()
    except Exception as e:
        print(f"[Command] status write failed: {e}")

if __name__ == "__main__":
    if STATUS:
        print_status_only()
    elif WATCH:
        print(f"[Command] WATCH mode — running all agents every {INTERVAL//60} minutes")
        while True:
            run_all()
            print(f"\n  Next run in {INTERVAL//60} minutes. Ctrl+C to stop.")
            try:
                time.sleep(INTERVAL)
            except KeyboardInterrupt:
                print("\n[Command] Stopped.")
                break
    else:
        run_all()
