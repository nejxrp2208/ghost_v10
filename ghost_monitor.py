"""
Ghost Monitor — CryptoGhost Real-Time Watchdog Agent + Supervisor
==================================================================
Polls the trade DB every 30 seconds. Tracks streaks, rolling WR,
daily PnL, scan health, and resolver health. Prints a live dashboard.
Alerts immediately when anything needs attention.

Now also supervises the bot processes (added 2026-05-04):
  - Detects missing processes → auto-restarts in new cmd window
  - Detects duplicates (race conditions, double-launches) → kills younger PIDs
  - Rate-limited: max 3 restarts per process per 30 min, then escalates to CRITICAL alert
  - Kill switch: create file `supervisor.disabled` in project dir to pause auto-restart

Alert thresholds:
  🔴 CRITICAL : Loss streak >= 20 | Daily loss >= $40 | Scanner stalled > 3 min |
                Process dead beyond restart limit
  🟡 WARNING  : Loss streak >= 10 | Daily loss >= $20 | Scan speed > 2000ms |
                Process restarted | Duplicate killed
  🟢 OK       : Everything normal
  ⭐ GREAT    : Win streak >= 3 | Rolling WR > break-even

Run modes:
  python ghost_monitor.py                # live dashboard + supervisor
  python ghost_monitor.py --once         # single check, print, exit
  python ghost_monitor.py --quiet        # suppress routine logs, only alerts
  python ghost_monitor.py --no-supervise # disable auto-restart (alerts only)
"""

import sqlite3, os, sys, time, json, subprocess
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SD       = os.path.dirname(os.path.abspath(__file__))
TRADE_DB = os.path.join(SD, "crypto_ghost_PAPER.db")
RPT_DB   = os.path.join(SD, "agent_reports.db")
ALERT_LOG = os.path.join(SD, "ghost_monitor_alerts.log")

ONCE         = "--once"         in sys.argv
QUIET        = "--quiet"        in sys.argv
NO_SUPERVISE = "--no-supervise" in sys.argv
INTERVAL     = 30  # seconds between polls

# ── Supervisor (auto-restart + dedup) ──────────────────────────────────────────
# Each entry: cmdline-match string (must be in process command line) → restart spec
# launch_cmd is the python command minus the cmd/k wrapper. set env vars first if needed.
# Set launch_cmd=None for processes that should NOT be auto-restarted (e.g., self).
# NOTE: On Linux VPS, the Windows restart mechanism (cmd.exe) won't fire.
#       This list is still useful for process detection + alerting on any platform.
SUPERVISED_PROCESSES = {
    'marketghost.py':              {'title': 'Market Ghost',                   'launch_cmd': 'python marketghost.py'},
    'crypto_ghost_scanner.py':     {'title': 'CryptoGhost Scanner 1 [PAPER]', 'launch_cmd': 'set PAPER_TRADE=true && python crypto_ghost_scanner.py'},
    'crypto_ghost_scanner2.py':    {'title': 'CryptoGhost Scanner 2 [PAPER]', 'launch_cmd': 'set PAPER_TRADE=true && python crypto_ghost_scanner2.py'},
    'ghost_analyst.py':            {'title': 'Ghost Analyst',                  'launch_cmd': 'python ghost_analyst.py --watch'},
    'ghost_optimizer.py':          {'title': 'Ghost Optimizer',                'launch_cmd': 'python ghost_optimizer.py --watch'},
    'ghost_strategist_v2.py':      {'title': 'Ghost Strategist V2',            'launch_cmd': 'python ghost_strategist_v2.py --watch --interval 3600'},
    'ghost_scaler_v2.py':          {'title': 'Ghost Scaler V2',                'launch_cmd': 'python ghost_scaler_v2.py --watch --interval 900'},
    'crypto_ghost_resolver.py':    {'title': 'CryptoGhost Resolver',           'launch_cmd': 'python crypto_ghost_resolver.py'},
    'ghost_command.py':            {'title': 'Ghost Command [OBSERVE]',        'launch_cmd': 'python ghost_command.py --observe --watch'},
    'ghost_telegram_bot.py':       {'title': 'Ghost Telegram Bot',             'launch_cmd': 'python ghost_telegram_bot.py'},
    'ghost_circuit_breaker.py':    {'title': 'Ghost Circuit Breaker',          'launch_cmd': 'python ghost_circuit_breaker.py'},
    'ghost_regime_sentinel.py':    {'title': 'Ghost Regime Sentinel',          'launch_cmd': 'python ghost_regime_sentinel.py'},
    'ghost_strategy_observer.py':  {'title': 'Ghost Strategy Observer',        'launch_cmd': 'python ghost_strategy_observer.py'},
    'ghost_monitor.py':            {'title': 'Ghost Monitor',                  'launch_cmd': None},  # never restart self
}

KILL_SWITCH_FILE       = os.path.join(SD, 'supervisor.disabled')
RESTART_COOLDOWN_SECS  = 60     # min seconds between restarts of the same process
RESTART_BURST_LIMIT    = 3      # max restarts in 30 min before escalating to CRITICAL
RESTART_BURST_WINDOW   = 1800   # 30 min sliding window for burst counting
SUPERVISOR_GRACE_SECS  = 120    # don't supervise for first N sec — lets start_*.bat finish
                                # spawning all windows without supervisor racing it

_restart_history = {}  # script_name -> list of restart timestamps
_SUPERVISOR_BOOT_TS = None  # set on first supervise_processes() call

# ── Thresholds ──────────────────────────────────────────────────────────────────
LOSS_STREAK_WARN     = 10
LOSS_STREAK_CRIT     = 20
DAILY_LOSS_WARN      = 20.0
DAILY_LOSS_CRIT      = 40.0
STALL_WARN_SECS      = 120
STALL_CRIT_SECS      = 180
SCAN_SPEED_WARN      = 2000
WIN_STREAK_CELEBRATE = 3

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
    if not os.path.exists(RPT_DB):
        c = sqlite3.connect(RPT_DB, timeout=60)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS agent_status (
                agent TEXT PRIMARY KEY, last_run TEXT, status TEXT, summary TEXT
            );
            CREATE TABLE IF NOT EXISTS monitor_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, level TEXT, category TEXT, message TEXT
            );
            CREATE TABLE IF NOT EXISTS monitor_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, total_trades INTEGER, wins INTEGER, losses INTEGER,
                open_pos INTEGER, streak TEXT, streak_len INTEGER,
                rolling_wr_10 REAL, rolling_wr_20 REAL, rolling_wr_50 REAL,
                daily_pnl REAL, total_pnl REAL, last_scan_ms INTEGER,
                scanner_age_secs INTEGER, resolver_age_secs INTEGER
            );
        """)
        c.commit(); c.close()
    else:
        c = sqlite3.connect(RPT_DB, timeout=60)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS monitor_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, level TEXT, category TEXT, message TEXT
            );
            CREATE TABLE IF NOT EXISTS monitor_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, total_trades INTEGER, wins INTEGER, losses INTEGER,
                open_pos INTEGER, streak TEXT, streak_len INTEGER,
                rolling_wr_10 REAL, rolling_wr_20 REAL, rolling_wr_50 REAL,
                daily_pnl REAL, total_pnl REAL, last_scan_ms INTEGER,
                scanner_age_secs INTEGER, resolver_age_secs INTEGER
            );
        """)
        c.commit(); c.close()

# ── Alert system ────────────────────────────────────────────────────────────────
_last_alerts = {}

def fire_alert(level, category, message, cooldown=300):
    """Fire an alert, deduplicating within cooldown seconds."""
    key = f"{level}_{category}"
    now = time.time()
    if key in _last_alerts and now - _last_alerts[key] < cooldown:
        return

    _last_alerts[key] = now
    ts = datetime.now().strftime("%H:%M:%S")
    icons = {"CRITICAL": "🔴", "WARNING": "🟡", "OK": "🟢", "GREAT": "⭐"}
    icon  = icons.get(level, "ℹ️")
    msg   = f"[{ts}] {icon} {level} | {category}: {message}"
    print(msg)

    with open(ALERT_LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

    try:
        c = sqlite3.connect(RPT_DB, timeout=60)
        c.execute("INSERT INTO monitor_alerts (ts, level, category, message) VALUES (?,?,?,?)",
                  (datetime.now(timezone.utc).isoformat(), level, category, message))
        c.execute("DELETE FROM monitor_alerts WHERE id NOT IN "
                  "(SELECT id FROM monitor_alerts ORDER BY id DESC LIMIT 500)")
        c.commit(); c.close()
    except Exception:
        pass

# ── Rolling WR calculator ────────────────────────────────────────────────────────
def rolling_wr(last_n):
    """Given list of statuses ['won','lost',...] latest-first, compute WR%."""
    if not last_n: return 0.0
    wins = sum(1 for s in last_n if s == "won")
    return round(wins / len(last_n) * 100, 1)

def breakeven_wr(avg_entry):
    """WR% needed to break even at this avg entry price."""
    if not avg_entry or avg_entry <= 0: return 100.0
    return round(avg_entry / (1.0 - avg_entry) * 100, 1)

# ── Collect metrics ──────────────────────────────────────────────────────────────
def collect_metrics():
    if not os.path.exists(TRADE_DB):
        return None

    today = datetime.now().strftime("%Y-%m-%d")

    # Basics
    wins       = qone(TRADE_DB, "SELECT COUNT(*) FROM trades WHERE status='won'")
    losses     = qone(TRADE_DB, "SELECT COUNT(*) FROM trades WHERE status='lost'")
    open_pos   = qone(TRADE_DB, "SELECT COUNT(*) FROM trades WHERE status='open'")
    total_pnl  = qone(TRADE_DB, "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status IN ('won','lost')")
    daily_pnl  = qone(TRADE_DB, f"SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts LIKE '{today}%' AND status IN ('won','lost')")
    avg_entry  = qone(TRADE_DB, "SELECT AVG(entry_price) FROM trades WHERE status IN ('won','lost') LIMIT 100")

    # Streak — pull enough rows to handle very long streaks without capping.
    # LIMIT was 50 which silently capped streak_len at 50. Rolling WR slices
    # below ([:10], [:20], [:50]) still work correctly with a larger pool.
    recent_statuses = [r[0] for r in q(TRADE_DB,
        "SELECT status FROM trades WHERE status IN ('won','lost') ORDER BY id DESC LIMIT 1000")]
    streak_type, streak_len = "—", 0
    if recent_statuses:
        streak_type = recent_statuses[0]
        for s in recent_statuses:
            if s == streak_type: streak_len += 1
            else: break

    # Rolling WR
    last10  = recent_statuses[:10]
    last20  = recent_statuses[:20]
    last50  = recent_statuses[:50]
    wr10  = rolling_wr(last10)
    wr20  = rolling_wr(last20)
    wr50  = rolling_wr(last50)
    total = wins + losses
    wr_all = round(wins / total * 100, 1) if total > 0 else 0.0

    # Scan stats
    scan_row = q(TRADE_DB,
        "SELECT ts, scan_ms FROM scan_stats ORDER BY id DESC LIMIT 1")
    last_scan_ts  = None
    last_scan_ms  = 0
    scanner_age   = 9999
    if scan_row:
        last_scan_ts = scan_row[0][0]
        last_scan_ms = scan_row[0][1] or 0
        try:
            ts_dt = datetime.fromisoformat(last_scan_ts.replace("Z", "+00:00"))
            if ts_dt.tzinfo is None: ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            scanner_age = int((datetime.now(timezone.utc) - ts_dt).total_seconds())
        except Exception:
            pass

    # Resolver activity (last closed trade)
    resolver_row = q(TRADE_DB,
        "SELECT closed_at FROM trades WHERE status IN ('won','lost') AND closed_at IS NOT NULL ORDER BY id DESC LIMIT 1")
    resolver_age = 9999
    if resolver_row and resolver_row[0][0]:
        try:
            ts_dt = datetime.fromisoformat(resolver_row[0][0].replace("Z", "+00:00"))
            if ts_dt.tzinfo is None: ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            resolver_age = int((datetime.now(timezone.utc) - ts_dt).total_seconds())
        except Exception:
            pass

    # Open position details
    open_rows = q(TRADE_DB,
        "SELECT coin, outcome, entry_price, ts FROM trades WHERE status='open' ORDER BY id DESC LIMIT 5")

    # Age of OLDEST open trade — used to suppress false-alarm "no resolver activity" warnings
    # when a brand-new trade just opened after a quiet stretch
    oldest_open_age = 0
    try:
        row = q(TRADE_DB, "SELECT ts FROM trades WHERE status='open' ORDER BY id ASC LIMIT 1")
        if row:
            ts_str = str(row[0][0]).replace("Z", "+00:00")
            ts_dt  = datetime.fromisoformat(ts_str)
            if ts_dt.tzinfo is None: ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            oldest_open_age = int((datetime.now(timezone.utc) - ts_dt).total_seconds())
    except Exception:
        pass

    return {
        "wins": wins, "losses": losses, "total": total, "open": open_pos,
        "total_pnl": total_pnl, "daily_pnl": daily_pnl, "avg_entry": avg_entry,
        "wr_all": wr_all, "wr10": wr10, "wr20": wr20, "wr50": wr50,
        "streak_type": streak_type, "streak_len": streak_len,
        "breakeven": breakeven_wr(avg_entry),
        "last_scan_ms": last_scan_ms, "scanner_age": scanner_age,
        "resolver_age": resolver_age, "oldest_open_age": oldest_open_age,
        "open_rows": open_rows,
    }

# ── Check alerts ──────────────────────────────────────────────────────────────────
def check_alerts(m):
    # Scanner health
    if m["scanner_age"] >= STALL_CRIT_SECS:
        fire_alert("CRITICAL", "SCANNER", f"No scan in {m['scanner_age']}s — scanner may have crashed!")
    elif m["scanner_age"] >= STALL_WARN_SECS:
        fire_alert("WARNING", "SCANNER", f"No scan in {m['scanner_age']}s — scanner may be stalled")

    # Scan speed
    if m["last_scan_ms"] > SCAN_SPEED_WARN and m["scanner_age"] < 60:
        fire_alert("WARNING", "SPEED", f"Scan speed {m['last_scan_ms']}ms — high latency may miss entries")

    # Resolver health — alert only if a real settlement-stuck condition exists.
    # Suppress the warning when:
    #   (a) the OLDEST open trade is < 5 min old (settlement hasn't had time)
    #   (b) the long resolver_age is just because there's been nothing to settle
    #       (i.e. resolver_age >> oldest_open_age means the trade just opened
    #       after a quiet stretch — not a stuck position)
    if m["open"] > 0 and m["resolver_age"] > 900:
        oldest = m.get("oldest_open_age", 0)
        if oldest < 300:
            pass  # trade just opened — give settlement time
        elif m["resolver_age"] - oldest > 600:
            pass  # resolver was idle BEFORE this trade opened — not a real stall
        else:
            fire_alert("WARNING", "RESOLVER",
                       f"Open position stuck {oldest//60}min — settlement not closing it")

    # Loss streak
    if m["streak_type"] == "lost":
        if m["streak_len"] >= LOSS_STREAK_CRIT:
            fire_alert("CRITICAL", "STREAK", f"{m['streak_len']} consecutive losses — consider pausing", cooldown=900)
        elif m["streak_len"] >= LOSS_STREAK_WARN:
            fire_alert("WARNING", "STREAK", f"{m['streak_len']} consecutive losses", cooldown=600)

    # Daily loss
    if m["daily_pnl"] <= -DAILY_LOSS_CRIT:
        fire_alert("CRITICAL", "DAILY_LOSS", f"Daily PnL: ${m['daily_pnl']:.2f} — approaching kill switch", cooldown=600)
    elif m["daily_pnl"] <= -DAILY_LOSS_WARN:
        fire_alert("WARNING", "DAILY_LOSS", f"Daily PnL: ${m['daily_pnl']:.2f}", cooldown=300)

    # Win streak celebration
    if m["streak_type"] == "won" and m["streak_len"] >= WIN_STREAK_CELEBRATE:
        fire_alert("GREAT", "STREAK", f"Win streak: {m['streak_len']} in a row! WR10={m['wr10']:.1f}%", cooldown=300)

    # WR near or above break-even
    if m["total"] >= 20 and m["wr20"] >= m["breakeven"]:
        fire_alert("GREAT", "PERFORMANCE", f"Rolling WR20={m['wr20']:.1f}% >= break-even {m['breakeven']:.1f}%!", cooldown=1800)

# ── Dashboard display ─────────────────────────────────────────────────────────────
def print_dashboard(m, check_num):
    ts = datetime.now().strftime("%H:%M:%S")
    total = m["total"]
    wr    = m["wr_all"]
    be    = m["breakeven"]
    daily = m["daily_pnl"]

    # Scanner status
    if m["scanner_age"] < 30: scan_icon = "🟢"
    elif m["scanner_age"] < 120: scan_icon = "🟡"
    else: scan_icon = "🔴"

    # Resolver status
    if m["resolver_age"] < 600: res_icon = "🟢"
    elif m["resolver_age"] < 1800: res_icon = "🟡"
    else: res_icon = "🔴"

    # Streak display
    if m["streak_type"] == "won":
        streak_display = f"⭐ +{m['streak_len']} wins"
    elif m["streak_type"] == "lost":
        streak_display = f"💀 -{m['streak_len']} losses"
    else:
        streak_display = "— no trades"

    # PnL colors (text only)
    pnl_str = f"${m['total_pnl']:+.2f}"
    day_str = f"${daily:+.2f}"

    os.system("cls" if os.name == "nt" else "clear")
    print("╔" + "═"*68 + "╗")
    print("║  👻 GHOST MONITOR — Live Bot Watchdog" + " "*31 + "║")
    print(f"║  {ts}  Check #{check_num}   Poll: every {INTERVAL}s" + " "*27 + "║")
    print("╠" + "═"*68 + "╣")
    print(f"║  WIN RATE   : {wr:>6.1f}%  (break-even: {be:.1f}%)   Gap: {wr-be:+.1f}%" + " "*11 + "║")
    print(f"║  RECORD     : {m['wins']}W / {m['losses']}L  ({total} closed)   Open: {m['open']}" + " "*20 + "║")
    print(f"║  STREAK     : {streak_display:<30}" + " "*22 + "║")
    print("╠" + "═"*68 + "╣")
    print(f"║  ROLLING WR :  Last 10={m['wr10']:>5.1f}%   Last 20={m['wr20']:>5.1f}%   Last 50={m['wr50']:>5.1f}%" + " "*6 + "║")
    print("╠" + "═"*68 + "╣")
    print(f"║  TOTAL P&L  : {pnl_str:<12}   TODAY: {day_str:<12}" + " "*23 + "║")
    print("╠" + "═"*68 + "╣")
    print(f"║  {scan_icon} SCANNER : last scan {m['scanner_age']:>4}s ago   speed={m['last_scan_ms']}ms" + " "*20 + "║")
    print(f"║  {res_icon} RESOLVER: last close {m['resolver_age']:>4}s ago" + " "*37 + "║")
    print("╠" + "═"*68 + "╣")

    if m["open"] > 0 and m["open_rows"]:
        print("║  OPEN POSITIONS:" + " "*51 + "║")
        for coin, outcome, entry, ts_open in m["open_rows"][:5]:
            ago = ""
            try:
                dt = datetime.fromisoformat(ts_open.replace("Z","+00:00"))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                ago = f"{int((datetime.now(timezone.utc)-dt).total_seconds()//60)}m ago"
            except Exception:
                pass
            print(f"║    {coin:<5} {outcome:<5} @ ${entry:.3f}   {ago:<12}" + " "*29 + "║")
    else:
        print("║  No open positions" + " "*49 + "║")

    print("╠" + "═"*68 + "╣")

    # Quick assessment
    if total < 20:
        assessment = f"Building sample ({total}/20 closed — too early to judge)"
    elif wr >= be * 1.1:
        assessment = f"POSITIVE EV ZONE — WR {wr:.1f}% beats break-even {be:.1f}%"
    elif wr >= be * 0.9:
        assessment = f"Near break-even ({wr:.1f}% vs {be:.1f}%) — keep monitoring"
    else:
        assessment = f"Negative EV ({wr:.1f}% vs {be:.1f}% needed) — run ghost_optimizer"

    print(f"║  STATUS: {assessment[:60]:<60}║")
    print("╚" + "═"*68 + "╝")

    # Recent alert replay
    try:
        c = sqlite3.connect(RPT_DB, timeout=60)
        recent_alerts = c.execute(
            "SELECT ts, level, category, message FROM monitor_alerts ORDER BY id DESC LIMIT 5"
        ).fetchall()
        c.close()
        if recent_alerts:
            print("\n  Recent alerts:")
            for ats, lv, cat, msg in recent_alerts:
                icons = {"CRITICAL":"🔴","WARNING":"🟡","OK":"🟢","GREAT":"⭐"}
                print(f"    {icons.get(lv,'ℹ️')} {ats[:19]}  {lv:<10} {cat}: {msg[:50]}")
    except Exception:
        pass

# ── Supervisor helpers ─────────────────────────────────────────────────────────────
# CREATE_NO_WINDOW prevents PowerShell from flashing a visible window every cycle.
# 0x08000000 is the Windows API constant; harmless on non-Windows (subprocess ignores).
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

def list_python_processes():
    """Returns list of dicts with ProcessId, CreationDate, CommandLine for python.exe procs.
    Uses PowerShell + Win32_Process for cmdline access (tasklist alone doesn't show cmdline).
    Window-suppressed: no visible PS flash every cycle."""
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Select-Object ProcessId, ParentProcessId, "
            "@{N='CreationDate';E={$_.CreationDate.ToString('o')}}, CommandLine | "
            "ConvertTo-Json -Compress -Depth 2"
        )
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_cmd],
            capture_output=True, text=True, timeout=15,
            creationflags=_CREATE_NO_WINDOW
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            data = [data]
        return data
    except Exception as e:
        # Don't crash on transient powershell errors — log and continue
        return []

def can_restart(script):
    """Rate limit check: cooldown + burst limit per process."""
    history = _restart_history.get(script, [])
    now = time.time()
    # Drop entries outside burst window
    history = [t for t in history if now - t < RESTART_BURST_WINDOW]
    _restart_history[script] = history

    if not history:
        return True
    # Cooldown — too soon since last restart?
    if now - history[-1] < RESTART_COOLDOWN_SECS:
        return False
    # Burst limit — too many restarts recently?
    if len(history) >= RESTART_BURST_LIMIT:
        return False
    return True

def restart_process(script, info):
    """Spawn process in a new visible cmd window. Matches start_PAPER.bat / start_AGENTS.bat pattern."""
    title = info['title']
    cmd   = info['launch_cmd']
    if cmd is None:
        return False
    try:
        # Use start command to spawn a NEW visible cmd window with title.
        # cmd /k keeps the window open (doesn't auto-close), matching launcher behavior.
        full = f'start "{title}" cmd /k "cd /d {SD} && {cmd}"'
        subprocess.Popen(full, shell=True)
        _restart_history.setdefault(script, []).append(time.time())
        return True
    except Exception as e:
        fire_alert("CRITICAL", "SUPERVISOR",
                   f"Failed to restart {title}: {type(e).__name__}: {e}")
        return False

def _group_by_parent(matches):
    """Group matches by their non-python ancestor ProcessId.

    Python 3.14's `py` launcher creates a 2-python tower per script:
        cmd.exe (X)
          └─ python.exe shim (Y, parent=X)   — has script name in cmdline
                └─ python.exe interpreter (Z, parent=Y) — has script name in cmdline

    Both pythons MATCH the script name. Naive parent grouping treats them as
    2 separate logical instances, triggering false-duplicate detection that
    kills the inner interpreter and crashes the bot. Fix: walk UP the parent
    chain, skipping any python ancestor that's itself in the match set, until
    we land on the real anchor (the cmd.exe PID). Both pythons collapse into
    a single group keyed by cmd.exe.
    """
    by_pid = {p.get('ProcessId'): p for p in matches if p.get('ProcessId')}
    by_anchor = {}
    for p in matches:
        anchor = p.get('ParentProcessId') or 0
        # Climb the chain — if parent is another matched python, ascend further.
        # Cap at 8 hops as a guard against pathological loops.
        hops = 0
        while anchor in by_pid and hops < 8:
            anchor = by_pid[anchor].get('ParentProcessId') or 0
            hops += 1
        by_anchor.setdefault(anchor, []).append(p)
    return by_anchor

def kill_pid(pid):
    """Kill a single PID via taskkill, suppressed window."""
    try:
        r = subprocess.run(
            ['taskkill', '/PID', str(pid), '/F'],
            capture_output=True, text=True, timeout=5,
            creationflags=_CREATE_NO_WINDOW
        )
        return r.returncode == 0
    except Exception:
        return False

def kill_duplicate_groups(matches):
    """Group matches by ParentProcessId. Keep oldest GROUP, kill all PIDs in younger groups.
    Each parent group = one logical instance. Returns count of LOGICAL instances killed."""
    groups = _group_by_parent(matches)
    if len(groups) <= 1:
        return 0  # only one logical instance — nothing to dedup

    # Find oldest creation time per group
    def group_oldest(parent_pid):
        ps = groups[parent_pid]
        return min((p.get('CreationDate') or '9999') for p in ps)

    sorted_parents = sorted(groups.keys(), key=group_oldest)
    keep_parent = sorted_parents[0]
    kill_parents = sorted_parents[1:]

    instances_killed = 0
    for pp in kill_parents:
        any_killed = False
        for p in groups[pp]:
            if kill_pid(p.get('ProcessId')):
                any_killed = True
        if any_killed:
            instances_killed += 1
    return instances_killed

def supervise_processes():
    """One supervisor cycle. Called inside main loop alongside check_alerts."""
    global _SUPERVISOR_BOOT_TS

    # Kill switch: file exists OR --no-supervise flag
    if NO_SUPERVISE:
        return
    if os.path.exists(KILL_SWITCH_FILE):
        return  # supervisor paused via file flag

    # Startup grace period: when ghost_monitor.py first launches, start_AGENTS.bat
    # is still in the middle of spawning its 11 windows. If we don't wait, we race
    # with the .bat and spawn duplicates of every agent. 120s is plenty.
    now = time.time()
    if _SUPERVISOR_BOOT_TS is None:
        _SUPERVISOR_BOOT_TS = now
    if now - _SUPERVISOR_BOOT_TS < SUPERVISOR_GRACE_SECS:
        remaining = int(SUPERVISOR_GRACE_SECS - (now - _SUPERVISOR_BOOT_TS))
        print(f"[Supervisor] grace period — sleeping ({remaining}s left before first cycle)")
        return

    procs = list_python_processes()

    for script, info in SUPERVISED_PROCESSES.items():
        # Find all running processes whose cmdline contains this script name
        matches = []
        for p in procs:
            cmdline = (p.get('CommandLine') or '')
            if script in cmdline:
                matches.append(p)

        # Group by parent — each parent group = ONE logical instance
        # (parent cmd.exe + python child + sometimes asyncio worker child)
        groups = _group_by_parent(matches)
        n_instances = len(groups)

        # Self (ghost_monitor.py): we ARE one of these instances
        if script == 'ghost_monitor.py':
            if n_instances > 1:
                # Find our parent group; protect it; kill others
                my_pid = os.getpid()
                my_parent_group = None
                for pp, ps in groups.items():
                    if any(p.get('ProcessId') == my_pid for p in ps):
                        my_parent_group = pp
                        break
                if my_parent_group is not None:
                    others_groups = {pp: ps for pp, ps in groups.items() if pp != my_parent_group}
                    others_matches = [p for ps in others_groups.values() for p in ps]
                    killed = kill_duplicate_groups(
                        [{'ProcessId': my_pid, 'ParentProcessId': my_parent_group, 'CreationDate': '0000'}]
                        + others_matches
                    )
                    if killed > 0:
                        fire_alert("WARNING", "SUPERVISOR",
                                   f"Killed {killed} duplicate ghost_monitor.py instance(s)",
                                   cooldown=300)
            continue

        # Missing logical instance → try to restart
        if n_instances == 0:
            if can_restart(script):
                if restart_process(script, info):
                    fire_alert("WARNING", "SUPERVISOR",
                               f"Auto-restarted {info['title']} (was missing)",
                               cooldown=60)
            else:
                hist = _restart_history.get(script, [])
                fire_alert("CRITICAL", "SUPERVISOR",
                           f"{info['title']} dead; {len(hist)} restarts in 30min — manual fix needed",
                           cooldown=600)

        # Multiple logical instances → dedup (keep oldest GROUP, kill all in younger groups)
        elif n_instances > 1:
            killed = kill_duplicate_groups(matches)
            if killed > 0:
                fire_alert("WARNING", "SUPERVISOR",
                           f"Killed {killed} duplicate instance(s) of {info['title']} (kept oldest)",
                           cooldown=300)

# ── Save snapshot ──────────────────────────────────────────────────────────────────
def save_snapshot(m):
    try:
        c = sqlite3.connect(RPT_DB, timeout=60)
        c.execute("""
            INSERT INTO monitor_snapshots
            (ts, total_trades, wins, losses, open_pos, streak, streak_len,
             rolling_wr_10, rolling_wr_20, rolling_wr_50, daily_pnl, total_pnl,
             last_scan_ms, scanner_age_secs, resolver_age_secs)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (datetime.now(timezone.utc).isoformat(),
              m["total"], m["wins"], m["losses"], m["open"],
              m["streak_type"], m["streak_len"],
              m["wr10"], m["wr20"], m["wr50"],
              m["daily_pnl"], m["total_pnl"],
              m["last_scan_ms"], m["scanner_age"], m["resolver_age"]))
        c.execute("DELETE FROM monitor_snapshots WHERE id NOT IN "
                  "(SELECT id FROM monitor_snapshots ORDER BY id DESC LIMIT 2880)")
        c.execute("INSERT OR REPLACE INTO agent_status VALUES ('monitor',?,?,?)",
                  (datetime.now(timezone.utc).isoformat(), "OK",
                   f"WR={m['wr_all']:.1f}% Streak={m['streak_type']}{m['streak_len']}"))
        c.commit(); c.close()
    except Exception as e:
        pass

# ── Main ──────────────────────────────────────────────────────────────────────────
def main():
    init_report_db()
    check_num = 0

    if not os.path.exists(TRADE_DB):
        print("[Monitor] Trade DB not found. Start the bot first.")
        return

    sup_status = "DISABLED (--no-supervise)" if NO_SUPERVISE else (
        "DISABLED (supervisor.disabled file present)" if os.path.exists(KILL_SWITCH_FILE) else "ENABLED"
    )
    print(f"[Monitor] Starting live watchdog... Supervisor: {sup_status}")
    if not NO_SUPERVISE:
        print(f"[Monitor] Watching {len(SUPERVISED_PROCESSES)-1} processes for restart/dedup. "
              f"Drop file '{os.path.basename(KILL_SWITCH_FILE)}' to pause auto-restart.")
        print(f"[Monitor] Supervisor grace period: {SUPERVISOR_GRACE_SECS}s "
              f"(prevents racing start_*.bat spawn).")

    while True:
        check_num += 1
        m = collect_metrics()

        if m is None:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] DB not ready yet...")
            time.sleep(INTERVAL)
            continue

        check_alerts(m)

        # Supervisor: detect missing processes + dedup duplicates
        try:
            supervise_processes()
        except Exception as e:
            # Never let supervisor errors crash the monitor
            print(f"[{datetime.now().strftime('%H:%M:%S')}] supervisor cycle err: {type(e).__name__}: {e}")

        if not QUIET:
            print_dashboard(m, check_num)

        save_snapshot(m)

        if ONCE:
            break

        time.sleep(INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Monitor] Stopped.")
