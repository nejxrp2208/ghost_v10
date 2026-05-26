"""
╔══════════════════════════════════════════════════════════════════╗
║   PROJECT EIDOLON  //  GHOST LATTICE Ψ-7  //  WRAITH PROTOCOL   ║
║   Oracle-Lag Arbitrage Engine v10 — Command & Control Center     ║
╚══════════════════════════════════════════════════════════════════╝
Classified: oracle-lag arb · Polygon/Polymarket · Chainlink edge
"""
import sqlite3, asyncio, aiohttp, os, sys, time, subprocess, signal, atexit
from datetime import datetime, timezone, timedelta

PROJECT_NAME    = "PROJECT EIDOLON"
PROJECT_CODENAME = "GHOST LATTICE Ψ-7  //  WRAITH PROTOCOL"
PROJECT_VERSION  = "v10"

# ── Background agent manager ──────────────────────────────────────────────────
_AGENTS: list[subprocess.Popen] = []

def _spawn_agents():
    sd  = os.path.dirname(os.path.abspath(__file__))
    py  = sys.executable
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"]       = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONWARNINGS"]   = "ignore"
    for script in ["GHOST_LATTICE.py", "GHOST_LATTICE_resolver.py", "GHOST_LATTICE_redeemer.py"]:
        path = os.path.join(sd, script)
        if not os.path.exists(path):
            continue
        if sys.platform == "win32":
            proc = subprocess.Popen(
                ["cmd.exe", "/k", py, "-u", "-X", "utf8", "-W", "ignore", path],
                cwd=sd, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            # Mac/Linux: redirect output to log files so Rich UI keeps the terminal
            log_path = os.path.join(sd, f"{os.path.splitext(script)[0]}.log")
            log_fh   = open(log_path, "a")
            proc = subprocess.Popen(
                [py, "-u", "-W", "ignore", path],
                cwd=sd, env=env, stdout=log_fh, stderr=log_fh,
            )
        _AGENTS.append(proc)

def _kill_agents():
    for p in _AGENTS:
        try: p.terminate()
        except Exception: pass

atexit.register(_kill_agents)

# ── Display constants ─────────────────────────────────────────────────────────
_TRADES_PAGE = 20
_ACT_PAGE    = 18
_LOG_PAGE    = 25

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.live import Live
    from rich import box as rbox
except ImportError:
    os.system(f"{sys.executable} -m pip install rich --break-system-packages -q")
    from rich.console import Console
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.live import Live
    from rich import box as rbox

try:
    from dotenv import load_dotenv
    sd = os.path.dirname(os.path.abspath(__file__))
    for ep in [os.path.join(sd, ".env")]:
        if os.path.exists(ep):
            load_dotenv(ep, override=True)
            break
except ImportError:
    pass

PAPER_TRADE      = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true","1","yes")
PORTFOLIO_SIZE   = float(os.getenv("PORTFOLIO_SIZE", "200.0"))
PRIVATE_KEY      = os.getenv("PRIVATE_KEY", "")
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "999999"))
STATS_SINCE      = os.getenv("STATS_SINCE", "2026-05-01")
POLYMARKET_HOST  = "https://clob.polymarket.com"
SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
DB_PATH          = os.path.join(SCRIPT_DIR, "GHOST_LATTICE_PAPER.db" if PAPER_TRADE else "GHOST_LATTICE.db")
REFRESH          = 5

# .env strategy config  (2-tier: T1=4hr contrarian, T2=hourly oracle-lag)
T1_SIZE   = float(os.getenv("T1_SIZE_USDC",  "10.0"))   # 4hr contrarian
T2_SIZE   = float(os.getenv("T2_SIZE_USDC",  "10.0"))   # hourly oracle-lag
T1_MIN    = float(os.getenv("T1_MIN_ENTRY",  "0.120"))  # 4hr contrarian floor
T1_MAX    = float(os.getenv("T1_MAX_ENTRY",  "0.200"))  # 4hr contrarian ceiling
T2_MIN    = float(os.getenv("T2_MIN_ENTRY",  "0.010"))  # hourly floor (1¢)
T2_MAX    = float(os.getenv("T2_MAX_ENTRY",  "0.080"))  # hourly ceiling
SKIP_COINS= os.getenv("SKIP_COINS", "")
VEL_MIN   = float(os.getenv("TREND_VELOCITY_MIN",  "0.0003"))
MOM_MIN   = float(os.getenv("TREND_MOMENTUM_MIN",  "0.05"))
TREND_ENH = os.getenv("TREND_ENHANCED", "true").lower() in ("true","1","yes")
CONF_MIN  = float(os.getenv("CONFIDENCE_MIN", "0.0"))
# Ghost Memory / HourBiasEngine deleted 2026-05-22 — simplicity pass

# ── Dashboard theme ────────────────────────────────────────────────────────────
DASHBOARD_THEME = os.getenv("DASHBOARD_THEME", "EIDOLON").upper()

_THEMES = {
    # ── EIDOLON: default cyan/white/multicolor ────────────────────
    "EIDOLON": {
        "border":       "white",
        "border_live":  "cyan",
        "border_paper": "green",
        "sec":          "bold cyan",
        "tier":         {1:"yellow", 2:"green", 3:"cyan", 4:"magenta"},
        "win":          "bold green",
        "loss":         "yellow",
        "header_name":  "bold cyan",
        "formula":      ("magenta", "blue", "cyan", "white"),
    },
    # ── BLOODMOON: red/gold aggressive combat ─────────────────────
    "BLOODMOON": {
        "border":       "bright_red",
        "border_live":  "bright_red",
        "border_paper": "yellow",
        "sec":          "bold yellow",
        "tier":         {1:"yellow", 2:"bright_red", 3:"red", 4:"yellow"},
        "win":          "bold yellow",
        "loss":         "bright_red",
        "header_name":  "bold yellow",
        "formula":      ("bright_red", "yellow", "red", "yellow"),
    },
    # ── PHANTOM: magenta/violet ghostly ──────────────────────────
    "PHANTOM": {
        "border":       "bright_magenta",
        "border_live":  "bright_magenta",
        "border_paper": "blue",
        "sec":          "bold bright_magenta",
        "tier":         {1:"blue", 2:"bright_magenta", 3:"magenta", 4:"cyan"},
        "win":          "bold bright_green",
        "loss":         "magenta",
        "header_name":  "bold magenta",
        "formula":      ("bright_magenta", "blue", "magenta", "cyan"),
    },
    # ── MATRIX: pure terminal green ───────────────────────────────
    "MATRIX": {
        "border":       "green",
        "border_live":  "bright_green",
        "border_paper": "green",
        "sec":          "bold bright_green",
        "tier":         {1:"bright_green", 2:"green", 3:"bright_green", 4:"dim green"},
        "win":          "bold bright_green",
        "loss":         "dim green",
        "header_name":  "bold bright_green",
        "formula":      ("bright_green", "green", "bright_green", "green"),
    },
}

_TH = _THEMES.get(DASHBOARD_THEME, _THEMES["EIDOLON"])

console = Console()
state   = {
    "prices":       {},
    "activity":     [],
    "last_update":  "—",
    "live_balance": None,
    "balance_src":  "—",
    "btc_history":  [],
    "eth_history":  [],
    "sol_history":  [],
    "xrp_history":  [],
    "bnb_history":  [],
}

# ── CLOB client ───────────────────────────────────────────────────────────────
_clob_client = None

def _init_clob_client():
    global _clob_client
    if PAPER_TRADE or not PRIVATE_KEY:
        return
    try:
        from py_clob_client.client import ClobClient
        bare  = ClobClient(host=POLYMARKET_HOST, key=PRIVATE_KEY, chain_id=137, signature_type=0)
        creds = bare.derive_api_key()
        _clob_client = ClobClient(host=POLYMARKET_HOST, key=PRIVATE_KEY,
                                   chain_id=137, signature_type=0, creds=creds)
    except Exception:
        _clob_client = None

# ── DB helpers ────────────────────────────────────────────────────────────────
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, tier INTEGER, coin TEXT,
        market_id TEXT, token_id TEXT, question TEXT, entry_price REAL,
        size_usdc REAL, order_id TEXT, outcome TEXT, status TEXT DEFAULT 'open',
        pnl REAL DEFAULT 0, closed_at TEXT, price_start REAL, price_end REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, icon TEXT, msg TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS scan_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
        markets_found INTEGER DEFAULT 0, signals_passed INTEGER DEFAULT 0,
        skip_ask INTEGER DEFAULT 0, skip_liq INTEGER DEFAULT 0,
        skip_certainty INTEGER DEFAULT 0, skip_window INTEGER DEFAULT 0,
        scan_ms INTEGER DEFAULT 0)""")
    conn.commit()
    return conn

def safe_query(sql, params=()):
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn  = db_connect()
        rows  = conn.execute(sql, params).fetchall()
        conn.close()
        return rows
    except Exception:
        return []

def safe_one(sql, params=()):
    rows = safe_query(sql, params)
    return rows[0][0] if rows else 0

def _since_clause(col="ts"):
    if STATS_SINCE:
        return f"{col} >= ?", (STATS_SINCE,)
    return "", ()

# ── Color helpers ─────────────────────────────────────────────────────────────
def g(v):   return f"[green]{v}[/]"
def r(v):   return f"[red]{v}[/]"
def c(v):   return f"[cyan]{v}[/]"
def dim(v): return f"[dim]{v}[/]"
def y(v):   return f"[yellow]{v}[/]"
def b(v):   return f"[bold]{v}[/]"
def mag(v): return f"[magenta]{v}[/]"

def pnl_str(v, compact=False):
    if not v: return dim("+$0.00")
    s = f"+${v:.2f}" if v > 0 else f"-${abs(v):.2f}"
    return (g(s) if v > 0 else r(s))

def _calc_streak():
    rows = safe_query("SELECT status FROM trades WHERE status IN ('won','lost') ORDER BY id DESC LIMIT 200")
    if not rows: return (0, "-")
    first = rows[0][0]
    count = 0
    for (s,) in rows:
        if s == first: count += 1
        else: break
    return (count, "W" if first == "won" else "L")

# ── Stats engine ──────────────────────────────────────────────────────────────
def get_stats():
    # Compute UTC window for "today" in local time — DB stores UTC timestamps.
    # Without this, wins after midnight UTC (8 PM ET) are invisible to the today filter.
    _now_local   = datetime.now()
    _utc_offset  = round((datetime.now() - datetime.utcnow()).total_seconds() / 3600)
    _local_midnight = _now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    _utc_day_start  = (_local_midnight - timedelta(hours=_utc_offset)).strftime("%Y-%m-%dT%H:%M:%S")
    _utc_day_end    = (_local_midnight - timedelta(hours=_utc_offset) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")

    _sc, _sp = _since_clause()
    _sw      = (f" AND {_sc}" if _sc else "")
    _base    = (_sp if _sp else ())

    wins      = safe_one(f"SELECT COUNT(*) FROM trades WHERE status='won'{_sw}",  _base)
    losses    = safe_one(f"SELECT COUNT(*) FROM trades WHERE status='lost'{_sw}", _base)
    open_     = safe_one("SELECT COUNT(*) FROM trades WHERE status='open'")
    total_pnl = safe_one(f"SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status IN ('won','lost'){_sw}", _base)
    daily_pnl = safe_one(
        "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts >= ? AND ts < ? AND status IN ('won','lost')",
        (_utc_day_start, _utc_day_end))
    spent     = safe_one("SELECT COALESCE(SUM(size_usdc),0) FROM trades WHERE status='open'")
    volume    = safe_one(f"SELECT COALESCE(SUM(size_usdc),0) FROM trades{(' WHERE ' + _sc) if _sc else ''}", _base)

    total = wins + losses
    wr    = (wins / total * 100) if total > 0 else 0.0
    cash  = round(PORTFOLIO_SIZE + total_pnl - spent, 2)
    fees  = round(volume * 0.02, 4)

    pnls  = safe_query("SELECT pnl FROM trades WHERE status IN ('won','lost') ORDER BY id")
    peak = cur = dd = 0.0
    for (p,) in pnls:
        cur += (p or 0)
        if cur > peak: peak = cur
        dd   = max(dd, peak - cur)
    bal_base = state.get("live_balance") or PORTFOLIO_SIZE
    dd_pct   = (dd / bal_base * 100) if bal_base > 0 else 0.0
    dd_left  = max(0, DAILY_LOSS_LIMIT - abs(min(0, daily_pnl)))

    avg_win  = safe_one(f"SELECT COALESCE(AVG(pnl),0)  FROM trades WHERE status='won'{_sw}",  _base)
    avg_loss = safe_one(f"SELECT COALESCE(AVG(pnl),0)  FROM trades WHERE status='lost'{_sw}", _base)
    best_win = safe_one(f"SELECT COALESCE(MAX(pnl),0)  FROM trades WHERE status='won'{_sw}",  _base)
    ev       = (total_pnl / total) if total > 0 else 0.0

    # Avg trades per day
    days = safe_one(f"SELECT COUNT(DISTINCT DATE(ts)) FROM trades WHERE status IN ('won','lost'){_sw}", _base)
    avg_day = round(total / days, 1) if days > 0 else 0.0

    streak = _calc_streak()
    return dict(wins=wins, losses=losses, open=open_, total_pnl=total_pnl,
                daily_pnl=daily_pnl, win_rate=wr, volume=volume, fees=fees,
                cash=cash, drawdown_pct=dd_pct, dd_left=dd_left,
                avg_win=avg_win, avg_loss=avg_loss, best_win=best_win,
                ev=ev, streak=streak, avg_per_day=avg_day, days=days)

# ── Per-coin stats ────────────────────────────────────────────────────────────
def get_coin_stats():
    _sc, _sp = _since_clause()
    _sw      = (f" AND {_sc}" if _sc else "")
    _base    = (_sp if _sp else ())
    rows = safe_query(
        f"SELECT coin, COUNT(*) n, SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) w, "
        f"ROUND(SUM(pnl),2) pnl, ROUND(AVG(entry_price),4) avg_e "
        f"FROM trades WHERE status IN ('won','lost'){_sw} GROUP BY coin ORDER BY pnl DESC",
        _base)
    return rows  # (coin, n, w, pnl, avg_e)

# ── Per-tier stats ────────────────────────────────────────────────────────────
def get_tier_stats():
    _sc, _sp = _since_clause()
    _sw      = (f" AND {_sc}" if _sc else "")
    _base    = (_sp if _sp else ())
    rows = safe_query(
        f"SELECT tier, COUNT(*) n, SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) w, "
        f"ROUND(SUM(pnl),2) pnl "
        f"FROM trades WHERE status IN ('won','lost'){_sw} GROUP BY tier ORDER BY tier",
        _base)
    return rows  # (tier, n, w, pnl)

# ── Async price fetcher ───────────────────────────────────────────────────────
async def fetch_prices(session):
    sym_map = {"BTCUSDT":"BTC","ETHUSDT":"ETH","SOLUSDT":"SOL","XRPUSDT":"XRP","BNBUSDT":"BNB"}
    for base in ["https://api.binance.com","https://api.binance.us"]:
        try:
            async with session.get(f"{base}/api/v3/ticker/price",
                                   timeout=aiohttp.ClientTimeout(total=5)) as rr:
                if rr.status == 200:
                    data   = await rr.json()
                    prices = {sym_map[i["symbol"]]: float(i["price"])
                              for i in data if i["symbol"] in sym_map}
                    if prices: return prices
        except Exception:
            continue
    return {}

async def fetch_live_balance():
    if PAPER_TRADE or not PRIVATE_KEY:
        return None, "paper"
    global _clob_client
    if _clob_client is None:
        await asyncio.get_event_loop().run_in_executor(None, _init_clob_client)
    if _clob_client is None:
        return None, "no-client"
    def _do():
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            bal = _clob_client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            return int(bal["balance"]) / 1e6, "CLOB"
        except Exception:
            return None, "err"
    return await asyncio.get_event_loop().run_in_executor(None, _do)

async def data_loop():
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                prices = await fetch_prices(session)
                if prices:
                    state["prices"] = prices
                    now = datetime.now()
                    for coin, key in [("BTC","btc_history"),("ETH","eth_history"),
                                      ("SOL","sol_history"),("XRP","xrp_history"),
                                      ("BNB","bnb_history")]:
                        if coin in prices:
                            state[key].append((now, prices[coin]))
                            state[key] = state[key][-120:]
                state["last_update"] = datetime.now().strftime("%H:%M:%S")
                bal, src = await fetch_live_balance()
                if bal is not None:
                    state["live_balance"] = bal
                    state["balance_src"]  = src
            except Exception:
                pass
            await asyncio.sleep(REFRESH)

# ══════════════════════════════════════════════════════════════════
#   PANEL 1 — EIDOLON COMMAND: Statistics + Filter Health + Formula
# ══════════════════════════════════════════════════════════════════
def render_statistics():
    s      = get_stats()
    prices = state["prices"]

    wr   = s.get("win_rate", 0)
    wrc  = "green" if wr >= 10 else ("yellow" if wr >= 5 else "red")
    mode = "[bold green]PAPER[/]" if PAPER_TRADE else "[bold red]LIVE[/]"
    dd   = s.get("drawdown_pct", 0)
    ddl  = s.get("dd_left", 0)

    live_bal = state.get("live_balance")
    bal_src  = state.get("balance_src", "--")
    if live_bal is not None:
        bal_str = f"[bold green]${live_bal:,.2f}[/] [dim]USDC ({bal_src})[/]"
    elif PAPER_TRADE:
        bal_str = f"[bold cyan]${s.get('cash', PORTFOLIO_SIZE):,.2f}[/] [dim]USDC (paper)[/]"
    else:
        bal_str = "[dim]fetching...[/]"

    t = Table.grid(padding=(0,1))
    t.add_column(style="dim white", width=14, no_wrap=True)
    t.add_column(style="white",     width=26, no_wrap=True)

    # ── WALLET ────────────────────────────────────────────────────
    t.add_row(Text.from_markup(f"[{_TH['sec']}]EIDOLON WALLET[/]"), Text(""))
    t.add_row("Balance",    Text.from_markup(bal_str))
    t.add_row("Today",      Text.from_markup(pnl_str(s.get("daily_pnl", 0))))
    t.add_row("All-time",   Text.from_markup(pnl_str(s.get("total_pnl", 0))))

    # ── COMBAT RECORD ─────────────────────────────────────────────
    t.add_row(Text.from_markup(f"[{_TH['sec']}]COMBAT RECORD[/]"), Text(""))
    total = s.get("wins",0) + s.get("losses",0)
    t.add_row("Record",    Text.from_markup(
        f"[green]{s.get('wins',0)}W[/] [dim]/[/] [red]{s.get('losses',0)}L[/]  [dim]({total})[/]"))
    t.add_row("Win Rate",  Text.from_markup(f"[{wrc}]{wr:.1f}%[/] [dim](break-even ~5.5%)[/]"))
    ev  = s.get("ev", 0)
    evc = "green" if ev >= 0 else "red"
    t.add_row("EV/trade",  Text.from_markup(f"[{evc}]{'+' if ev>=0 else ''}{ev:.2f}[/]"))
    t.add_row("Avg Win",   Text.from_markup(g(f"+${s.get('avg_win',0):.2f}") if s.get('avg_win') else dim("--")))
    t.add_row("Avg Loss",  Text.from_markup(r(f"-${abs(s.get('avg_loss',0)):.2f}") if s.get('avg_loss') else dim("--")))
    t.add_row("Best Win",  Text.from_markup(g(f"+${s.get('best_win',0):.2f}") if s.get('best_win') else dim("--")))
    sc, sl = s.get("streak", (0, "-"))
    t.add_row("Streak",    Text.from_markup(f"[{'green' if sl=='W' else 'red'}]{sc}{sl}[/]" if sc else dim("--")))
    t.add_row("Open",      Text.from_markup(f"[cyan]{s.get('open',0)}[/] [dim]pos[/]"))
    t.add_row("Avg/day",   Text.from_markup(f"[white]{s.get('avg_per_day',0):.1f}[/] [dim]trades ({s.get('days',0)} days)[/]"))

    # ── RISK ──────────────────────────────────────────────────────
    t.add_row(Text.from_markup(f"[{_TH['sec']}]RISK[/]"), Text(""))
    ddc  = "red" if dd > 15 else ("yellow" if dd > 8 else "green")
    ddlc = "red" if ddl < 10 else "green"
    t.add_row("Drawdown",  Text.from_markup(f"[{ddc}]{dd:.1f}%[/]"))
    t.add_row("DD Budget", Text.from_markup(f"[{ddlc}]${ddl:.2f}[/]"))
    ks = abs(min(0.0, s.get("daily_pnl",0.0))) >= DAILY_LOSS_LIMIT and DAILY_LOSS_LIMIT < 999998
    t.add_row("Kill Sw.",  Text.from_markup("[bold red]TRIGGERED[/]" if ks else g("OK")))
    t.add_row("Mode",      Text.from_markup(mode))

    # ── PER-COIN QUICK VIEW ───────────────────────────────────────
    t.add_row(Text.from_markup(f"[{_TH['sec']}]COINS[/]"), Text(""))
    coin_rows = get_coin_stats()
    SKIP = [x.strip().upper() for x in SKIP_COINS.split(",") if x.strip()]
    for row in coin_rows:
        if len(row) < 5: continue
        coin, n, w, pnl, avg_e = row
        wr_c = round(w / n * 100, 1) if n > 0 else 0
        pc   = "green" if pnl >= 0 else "red"
        skipped = coin.upper() in SKIP
        flag = " [dim][SKIP][/]" if skipped else ""
        t.add_row(
            Text.from_markup(f"[{'dim' if skipped else 'white'}]{coin}[/]{flag}"),
            Text.from_markup(
                f"[{pc}]{'+' if pnl>=0 else ''}{pnl:.0f}$[/] "
                f"[dim]{w}W/{n} ({wr_c:.0f}%)[/]")
        )

    # ── TIER SNAPSHOT ─────────────────────────────────────────────
    t.add_row(Text.from_markup(f"[{_TH['sec']}]TIERS[/]"), Text(""))
    tier_names = {1: "T1·WRAITH", 2: "T2·PHANTOM"}
    tier_cols  = {1: _TH["tier"][1], 2: _TH["tier"][2]}
    tier_rows  = get_tier_stats()
    for row in tier_rows:
        if len(row) < 4: continue
        tier, n, w, pnl = row
        pc  = "green" if pnl >= 0 else "red"
        wr_c = round(w / n * 100, 1) if n > 0 else 0
        tn  = tier_names.get(tier, f"T{tier}")
        tc  = tier_cols.get(tier, "white")
        t.add_row(
            Text.from_markup(f"[{tc}]{tn}[/]"),
            Text.from_markup(f"[{pc}]{'+' if pnl>=0 else ''}{pnl:.0f}$[/] [dim]{w}W/{n} ({wr_c:.0f}%)[/]")
        )

    # ── GHOST LATTICE FORMULA (weights at a glance) ───────────────
    t.add_row(Text.from_markup(f"[{_TH['sec']}]FORMULA Ψ[/]"), Text(""))
    _f = _TH["formula"]
    t.add_row(Text.from_markup("[dim]mismatch[/]"), Text.from_markup(f"[bold {_f[0]}]35%[/] [dim]← oracle edge[/]"))
    t.add_row(Text.from_markup("[dim]dev[/]"),      Text.from_markup(f"[bold {_f[1]}]20%[/]  [dim]← Binance move[/]"))
    t.add_row(Text.from_markup("[dim]liq+time[/]"), Text.from_markup(f"[bold {_f[2]}]30%[/]  [dim]← book+clock[/]"))
    t.add_row(Text.from_markup("[dim]price+mom[/]"),Text.from_markup(f"[{_f[3]}]15%[/]  [dim]← cheap+tick[/]"))

    # ── FILTER HEALTH ─────────────────────────────────────────────
    t.add_row(Text.from_markup(f"[{_TH['sec']}]FILTERS[/]"), Text(""))
    def fstat(val, label, good, warn_val=None):
        if warn_val is not None and val == warn_val:
            return Text.from_markup(f"[yellow]{label}[/] [dim](warn)[/]")
        return Text.from_markup(f"[{'green' if good else 'red'}]{label}[/]")

    t.add_row(Text.from_markup("[dim]TREND[/]"),  fstat(TREND_ENH, "ON" if TREND_ENH else "OFF!", TREND_ENH))
    t.add_row(Text.from_markup("[dim]VEL[/]"),    Text.from_markup(f"[{'green' if VEL_MIN >= 0.0002 else 'yellow'}]{VEL_MIN:.4f}[/]"))
    t.add_row(Text.from_markup("[dim]MOM[/]"),    Text.from_markup(f"[{'green' if MOM_MIN >= 0.03 else 'yellow'}]{MOM_MIN:.2f}[/]"))
    t.add_row(Text.from_markup("[dim]CONF_MIN[/]"),Text.from_markup(f"[{'yellow' if CONF_MIN == 0.0 else 'green'}]{CONF_MIN:.2f}[/] [dim]{'(off)' if CONF_MIN == 0 else ''}[/]"))
    skip_lbl = SKIP_COINS if SKIP_COINS else "[dim]none[/]"
    t.add_row(Text.from_markup("[dim]SKIP[/]"),   Text.from_markup(f"[{'red' if not SKIP_COINS else 'green'}]{skip_lbl}[/]"))

    # ── ENTRY BAND ────────────────────────────────────────────────
    t.add_row(Text.from_markup(f"[{_TH['sec']}]ENTRY BAND[/]"), Text(""))
    band = T2_MAX - T2_MIN
    bc   = "green" if band >= 0.05 else ("yellow" if band > 0.01 else "red")
    t.add_row(Text.from_markup("[dim]T2 floor[/]"),   Text.from_markup(f"[cyan]${T2_MIN:.3f}[/] [dim](min 3c)[/]"))
    t.add_row(Text.from_markup("[dim]T2 ceiling[/]"), Text.from_markup(f"[cyan]${T2_MAX:.3f}[/]"))
    t.add_row(Text.from_markup("[dim]band width[/]"), Text.from_markup(f"[{bc}]{band*100:.1f}¢[/] [dim]({int(1/T2_MAX)}x→{int(1/T2_MIN)}x pay)[/]"))

    # ── PRICES ────────────────────────────────────────────────────
    t.add_row(Text.from_markup(f"[{_TH['sec']}]PRICES[/]"), Text(""))
    for coin, fmt in [("BTC","${:,.2f}"),("ETH","${:,.2f}"),
                      ("SOL","${:,.2f}"),("XRP","${:,.4f}"),("BNB","${:,.2f}")]:
        px = prices.get(coin, 0)
        t.add_row(coin, Text.from_markup(c(fmt.format(px)) if px else dim("--")))

    title_mode = "PAPER" if PAPER_TRADE else "LIVE"
    return Panel(t,
                 title=f"[bold white]{PROJECT_NAME}[/] [dim]// {title_mode}[/]",
                 border_style=_TH["border_live"] if not PAPER_TRADE else _TH["border_paper"],
                 box=rbox.SQUARE, padding=(1,1))

# ══════════════════════════════════════════════════════════════════
#   PANEL 2 — GHOST POSITIONS: Open trades
# ══════════════════════════════════════════════════════════════════
def render_open_positions():
    rows = safe_query("""
        SELECT id, tier, coin, question, entry_price, size_usdc, ts, outcome
        FROM trades WHERE status='open' ORDER BY id DESC LIMIT 12
    """)
    t = Table(box=rbox.SIMPLE_HEAD, border_style="white",
              show_header=True, header_style="bold white",
              expand=True, padding=(0,1))
    t.add_column("ID",    width=5,  style="dim")
    t.add_column("Market",min_width=36, overflow="ellipsis")
    t.add_column("Tier",  width=10)
    t.add_column("Side",  width=5)
    t.add_column("$",     width=6,  justify="right")
    t.add_column("Entry", width=7,  justify="right")
    t.add_column("Pay",   width=6,  justify="right")
    t.add_column("Time",  width=8,  style="dim")

    tier_cols  = _TH["tier"]
    tier_names = {1:"WRAITH", 2:"PHANTOM"}
    for rid, tier, coin, q, entry, size, ts, outcome in rows:
        col  = tier_cols.get(tier, "white")
        tn   = tier_names.get(tier, f"T{tier}")
        pay  = f"{int(1/entry)}x" if entry and entry > 0 else "?"
        tstr = ts[11:16] if ts else "?"
        side = str(outcome).upper() if outcome else "?"
        side_t = (Text.from_markup("[bold green]UP[/]")   if side == "UP"   else
                  Text.from_markup("[bold red]DOWN[/]")   if side == "DOWN" else
                  Text.from_markup("[dim]?[/]"))
        t.add_row(str(rid),
                  Text(str(q)[:60], overflow="ellipsis"),
                  Text.from_markup(f"[{col}]T{tier}·{tn} {coin}[/]"),
                  side_t, f"${size:.2f}", f"${entry:.3f}", pay, tstr)

    return Panel(t,
                 title=f"[bold white]Ghost Positions ({len(rows)} open)[/]",
                 border_style=_TH["border"], box=rbox.SQUARE, padding=(0,0))

# ══════════════════════════════════════════════════════════════════
#   PANEL 3 — WRAITH RADAR: Prices + Scanner + Speed
# ══════════════════════════════════════════════════════════════════
def render_scanner_stats():
    COINS   = [("BTC","btc_history","yellow","${:,.2f}"),
               ("ETH","eth_history","cyan","${:,.2f}"),
               ("SOL","sol_history","magenta","${:,.2f}"),
               ("XRP","xrp_history","green","${:,.4f}"),
               ("BNB","bnb_history","blue","${:,.2f}")]
    SPARK_W = 16
    CHARS   = "._.--=-+*#"
    SKIP    = [x.strip().upper() for x in SKIP_COINS.split(",") if x.strip()]

    def mini_spark(history, color):
        if len(history) < 2: return f"[dim]{'-'*SPARK_W}[/]"
        vals = [p for _, p in history]
        lo, hi = min(vals), max(vals)
        rng    = hi - lo if hi != lo else 1
        step   = max(1, len(vals) // SPARK_W)
        bar    = ""
        for i in range(0, min(len(vals), SPARK_W*step), step):
            idx = min(len(CHARS)-1, int((vals[i]-lo)/rng*len(CHARS)))
            bar += f"[{color}]{CHARS[idx]}[/]"
        return bar

    def _sec(label):
        return (Text(""), Text.from_markup(f"[dim bold]{label}[/]"), Text(""), Text(""))

    t = Table.grid(padding=(0,1))
    t.add_column(width=10, no_wrap=True)
    t.add_column(width=18, no_wrap=True)
    t.add_column(width=9,  no_wrap=True)
    t.add_column(width=18, no_wrap=True, overflow="crop")

    # ── Sparklines ────────────────────────────────────────────────
    for coin, hk, color, fmt in COINS:
        history = state.get(hk, [])
        skipped = coin in SKIP
        dim_c   = "dim" if skipped else color
        if len(history) < 2:
            t.add_row(Text.from_markup(f"[{dim_c}]{coin}[/]"),
                      Text.from_markup("[dim]--[/]"),
                      Text.from_markup("[dim]--[/]"),
                      Text.from_markup(f"[dim]{'SKIP' if skipped else '--'}[/]"))
            continue
        vals  = [p for _, p in history]
        last, first = vals[-1], vals[0]
        chg_p = (last - first) / first * 100 if first else 0
        chg_c = "green" if chg_p >= 0 else "red"
        sign  = "+" if chg_p >= 0 else ""
        skip_tag = "[dim] SKIP[/]" if skipped else ""
        t.add_row(
            Text.from_markup(f"[bold {dim_c}]{coin}[/]{skip_tag}"),
            Text.from_markup(f"[{'dim' if skipped else 'white'}]{fmt.format(last)}[/]"),
            Text.from_markup(f"[{chg_c}]{sign}{chg_p:.2f}%[/]"),
            Text.from_markup(mini_spark(history, dim_c)),
        )

    # ── 10-min range ──────────────────────────────────────────────
    t.add_row(*_sec("─ 10-min range ─"))
    for coin, hk, color, fmt in COINS:
        history = state.get(hk, [])
        if len(history) < 2:
            t.add_row(Text.from_markup(f"[dim]{coin}[/]"), Text.from_markup("[dim]...[/]"), Text(""), Text(""))
            continue
        vals    = [p for _, p in history]
        hi, lo  = max(vals), min(vals)
        last    = vals[-1]
        rng_pct = (hi-lo)/lo*100 if lo else 0
        hi_d    = (hi-last)/last*100 if last else 0
        lo_d    = (lo-last)/last*100 if last else 0
        rng_c   = "white" if rng_pct > 0.05 else "dim white"
        t.add_row(Text.from_markup(f"[dim]{coin}[/]"),
                  Text.from_markup(f"[green]+{hi_d:.2f}%[/]"),
                  Text.from_markup(f"[red]{lo_d:.2f}%[/]"),
                  Text.from_markup(f"[dim]rng[/] [{rng_c}]{rng_pct:.3f}%[/]"))

    # ── Scanner telemetry ─────────────────────────────────────────
    t.add_row(*_sec("─ Wraith Radar ──"))
    rows_db = safe_query("""
        SELECT markets_found, signals_passed, skip_ask, skip_liq,
               skip_certainty, skip_window, scan_ms,
               COALESCE(skip_vel, 0)
        FROM scan_stats ORDER BY id DESC LIMIT 100""")

    if rows_db:
        n         = len(rows_db)
        avg_ms    = int(sum(rr[6] for rr in rows_db) / n)
        total_mkt = sum(rr[0] for rr in rows_db)
        total_sig = sum(rr[1] for rr in rows_db)
        skip_ask  = sum(rr[2] for rr in rows_db)
        skip_liq  = sum(rr[3] for rr in rows_db)
        skip_cert = sum(rr[4] for rr in rows_db)
        skip_win  = sum(rr[5] for rr in rows_db)
        skip_vel  = sum(rr[7] for rr in rows_db)
        speed_c   = "green" if avg_ms < 1000 else ("yellow" if avg_ms < 3000 else "red")
        sig_c     = "green" if total_sig > 0 else "dim white"
        vel_c     = "bright_red" if skip_vel > skip_cert else "white"
        t.add_row(Text.from_markup("[dim]Scans[/]"),
                  Text.from_markup(f"[white]{n}[/] [dim]({total_mkt}m)[/]"),
                  Text.from_markup(f"[{speed_c}]{avg_ms}ms[/]"),
                  Text.from_markup(f"[{sig_c}]{total_sig} sig{'s' if total_sig!=1 else ''}[/]"))
        t.add_row(Text.from_markup("[dim]Skip[/]"),
                  Text.from_markup(f"[dim]ask[/] [white]{skip_ask}[/] [dim]liq[/] [white]{skip_liq}[/]"),
                  Text.from_markup(f"[dim]vel[/] [{vel_c}]{skip_vel}[/]"),
                  Text.from_markup(f"[dim]c:[/][white]{skip_cert}[/] [dim]w:[/][white]{skip_win}[/]"))
    else:
        t.add_row(Text(""), Text.from_markup("[dim]waiting for scanner...[/]"), Text(""), Text(""))
        t.add_row(Text(""), Text(""), Text(""), Text(""))

    # ── Tier config + sweet spot ───────────────────────────────────
    t.add_row(*_sec("─ Tier Config ───"))
    tier_data = [
        ("T1·WRAITH",  "cyan",    T1_SIZE, T1_MIN, T1_MAX),   # unified contrarian (all markets)
    ]
    for tn, col, sz, e_min, e_max in tier_data:
        if e_max == 0.0:
            t.add_row(Text.from_markup(f"[{col}]{tn}[/]"),
                      Text.from_markup("[dim]disabled[/]"),
                      Text(""), Text(""))
            continue
        band = e_max - e_min
        bc   = "green" if band >= 0.05 else ("yellow" if band > 0.005 else "red")
        t.add_row(Text.from_markup(f"[{col}]{tn}[/]"),
                  Text.from_markup(f"[white]${sz:.0f}[/] [dim]fl[/][cyan]${e_min:.3f}[/]"),
                  Text.from_markup(f"[dim]→[/][white]${e_max:.3f}[/]"),
                  Text.from_markup(f"[{bc}]{band*100:.1f}¢ band[/]"))

    # Sweet spot indicator
    t.add_row(*_sec("─ Sweet Spot ────"))
    t.add_row(Text.from_markup("[dim]zone[/]"),
              Text.from_markup("[bold green]$0.03–$0.05[/]"),
              Text.from_markup("[green]3.7% WR[/]"),
              Text.from_markup("[green]+$65 live[/]"))
    t.add_row(Text.from_markup("[dim]dead zone[/]"),
              Text.from_markup("[bold red]< $0.03[/]"),
              Text.from_markup("[red]0.0% WR[/]"),
              Text.from_markup("[red]-$319 (59t)[/]"))
    t.add_row(Text.from_markup("[dim]ETH status[/]"),
              Text.from_markup("[red]-$308 loss[/]" if "ETH" not in SKIP else "[green]BLOCKED[/]"),
              Text.from_markup("[dim]5.2% WR[/]"),
              Text.from_markup("[red]80% drawdown[/]" if "ETH" not in SKIP else "[green]protected[/]"))

    return Panel(t, title=f"[bold white]Wraith Radar[/]",
                 border_style=_TH["border"], box=rbox.SQUARE, padding=(0,1))

# ══════════════════════════════════════════════════════════════════
#   PANEL 4 — SPECTER LOG: Trade history with payout + bucket
# ══════════════════════════════════════════════════════════════════
def render_last_trades():
    total = safe_one("SELECT COUNT(*) FROM trades")
    rows  = safe_query(f"""
        SELECT id, ts, tier, coin, question, size_usdc, pnl, status,
               COALESCE(outcome,'') as outcome, entry_price
        FROM trades ORDER BY id DESC LIMIT {_TRADES_PAGE}
    """)

    t = Table(box=rbox.SIMPLE_HEAD, border_style="white",
              show_header=True, header_style="bold white",
              expand=True, padding=(0,1))
    t.add_column("ID",     width=5,  style="dim")
    t.add_column("Time",   width=8,  style="dim")
    t.add_column("Tier",   width=10)
    t.add_column("Side",   width=5)
    t.add_column("$",      width=6,  justify="right")
    t.add_column("P&L",    width=9,  justify="right")
    t.add_column("Pay",    width=5,  justify="right")
    t.add_column("Result", width=19)

    tier_cols  = _TH["tier"]
    tier_names = {1:"WRAITH", 2:"PHANTOM"}

    for row in rows:
        rid, ts, tier, coin, q, size, pnl, status, outcome, entry = row
        tstr   = ts[11:16] if ts else "?"
        col    = tier_cols.get(tier, "white")
        tn     = tier_names.get(tier, f"T{tier}")
        side   = str(outcome).upper() if outcome else "?"
        side_t = (Text.from_markup("[bold green]UP[/]")   if side == "UP"   else
                  Text.from_markup("[bold red]DOWN[/]")   if side == "DOWN" else
                  Text.from_markup("[dim]?[/]"))
        pay_x  = f"{int(1/entry)}x" if entry and entry > 0 else "--"
        bet_s  = f"${size:.2f}" if size else "--"

        if status == "won":
            pnl_t   = Text.from_markup(f"[{_TH['win']}]+${pnl:.2f}[/]" if pnl else f"[{_TH['win']}]+$?[/]")
            result  = Text.from_markup(f"[{_TH['win']}]WINNER![/]")
        elif status == "lost":
            pnl_t   = Text.from_markup(f"[red]-${size:.2f}[/]" if size else "[red]-$?[/]")
            result  = Text.from_markup(f"[{_TH['loss']}]SORRY NOT A WINNER[/]")
        else:
            pnl_t   = Text.from_markup("[dim]open[/]")
            result  = Text.from_markup("[cyan]active[/]")

        t.add_row(str(rid), tstr,
                  Text.from_markup(f"[{col}]T{tier}·{tn} {str(coin).upper()}[/]"),
                  side_t, bet_s, pnl_t, pay_x, result)

    return Panel(t,
                 title=f"[bold white]Specter Log[/]  [dim]{total} total trades[/]",
                 border_style=_TH["border"], box=rbox.SQUARE, padding=(0,0))

# ══════════════════════════════════════════════════════════════════
#   PANEL 5 — EIDOLON FEED: Activity events
# ══════════════════════════════════════════════════════════════════
_ICON_MAP = {
    "✅":"WIN","🎯":"SIG","📋":"LOG","✗":"ERR","❌":"ERR",
    "⚠":"WRN","💰":"PAY","🔄":"RLD","📊":"DAT",
}
_ICON_COLOR = {
    "WIN":"bold green","SIG":"bold cyan","LOG":"dim white",
    "ERR":"bold red",  "WRN":"bold yellow","PAY":"green",
    "RLD":"cyan","DAT":"white",
}

def render_activity():
    act_total = safe_one("SELECT COUNT(*) FROM events")
    t = Table(box=None, show_header=False, expand=True,
              padding=(0,1), show_edge=False)
    t.add_column(width=6,  no_wrap=True, style="dim")
    t.add_column(width=5,  no_wrap=True)
    t.add_column(no_wrap=False, overflow="fold")

    if act_total > 0:
        rows = safe_query(f"SELECT ts, icon, msg FROM events ORDER BY id DESC LIMIT {_ACT_PAGE}")
        for ts, icon, msg in rows:
            raw  = str(icon) if icon else ""
            tag  = _ICON_MAP.get(raw, raw[:3].upper() if raw and ord(raw[0])<128 else "---")
            tcol = _ICON_COLOR.get(tag, "dim white")
            msg_s = str(msg)
            if tag in ("WIN","PAY") or "won" in msg_s.lower() or "+$" in msg_s:
                mc = "green"
            elif tag in ("ERR",) or "lost" in msg_s.lower() or "error" in msg_s.lower():
                mc = "red"
            elif tag in ("SIG",) or "signal" in msg_s.lower() or "buy" in msg_s.lower():
                mc = "cyan"
            elif tag in ("WRN","LOG"):
                mc = "yellow"
            else:
                mc = "white"
            t_disp = str(ts)[11:16] if ts and len(str(ts))>15 else str(ts)[:5]
            t.add_row(t_disp,
                      Text.from_markup(f"[{tcol}][{tag}][/]"),
                      Text.from_markup(f"[{mc}]{msg_s}[/]"))
    else:
        mem = list(reversed(state["activity"]))
        for ts, msg in mem[:_ACT_PAGE]:
            t.add_row(str(ts)[:5], Text.from_markup("[dim]---[/]"), str(msg))
        if not state["activity"]:
            t.add_row("", "", Text.from_markup(dim("Wraith feed initializing...")))

    return Panel(t,
                 title=f"[bold white]Eidolon Feed[/]  [dim]{state['last_update']}[/]",
                 border_style=_TH["border"], box=rbox.SQUARE, padding=(1,1))

def log_activity(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    state["activity"].append((ts, str(msg)))
    if len(state["activity"]) > 80:
        state["activity"] = state["activity"][-80:]

# ══════════════════════════════════════════════════════════════════
#   MAIN
# ══════════════════════════════════════════════════════════════════
async def main():
    # Auto-spawn disabled — PM2 manages scanner/resolver/redeemer processes.
    # Running this dashboard alongside PM2 with spawn enabled would create
    # duplicate processes writing to the same DB (double-fires, SQLite contention).
    # _spawn_agents()

    log_activity(f"{PROJECT_NAME} // {PROJECT_CODENAME} — online")
    log_activity(f"Mode: {'PAPER' if PAPER_TRADE else 'LIVE'} | Filter: VEL={VEL_MIN} MOM={MOM_MIN}")
    log_activity(f"Band: ${T2_MIN:.3f}–${T2_MAX:.3f} | Skip: {SKIP_COINS or 'none'}")

    asyncio.create_task(data_loop())

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="top",    ratio=3),
        Layout(name="bottom", ratio=2),
    )
    layout["top"].split_row(
        Layout(name="stats",     ratio=2),
        Layout(name="positions", ratio=4),
        Layout(name="odds",      ratio=3),
    )
    layout["bottom"].split_row(
        Layout(name="trades",   ratio=2),
        Layout(name="activity", ratio=1),
    )

    PANELS = [
        ("stats",     render_statistics),
        ("positions", render_open_positions),
        ("odds",      render_scanner_stats),
        ("trades",    render_last_trades),
        ("activity",  render_activity),
    ]

    prev_open = prev_wins = 0

    with Live(layout, console=console, refresh_per_second=1, screen=False):
        while True:
            try:
                now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
                mode = "[bold green]PAPER TRADING[/]" if PAPER_TRADE else "[bold red]*** LIVE TRADING ***[/]"

                hdr = Table.grid(expand=True)
                hdr.add_column(justify="center")
                hdr.add_row(Text.from_markup(
                    f"[{_TH['header_name']}]{PROJECT_NAME}[/] [dim]//[/] [white]{PROJECT_CODENAME}[/]"
                    f"   {mode}   [dim]{now}[/]"
                ))
                layout["header"].update(
                    Panel(hdr, border_style=_TH["border_live"] if not PAPER_TRADE else _TH["border_paper"],
                          box=rbox.SQUARE, padding=(0,1))
                )

                for name, fn in PANELS:
                    try:
                        layout[name].update(fn())
                    except Exception as ex:
                        try:
                            layout[name].update(Panel(
                                Text(f"Error in {name}: {str(ex)[:80]}"),
                                border_style="red"))
                        except Exception:
                            pass

                s = get_stats()
                cur_open = s.get("open", 0)
                cur_wins = s.get("wins", 0)
                if cur_open > prev_open:
                    log_activity(f"Ghost position opened — {cur_open} active")
                if cur_wins > prev_wins:
                    log_activity(f"PHANTOM WIN confirmed — {cur_wins} total wins")
                prev_open = cur_open
                prev_wins = cur_wins

            except Exception:
                pass
            await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("[cyan]" + PROJECT_NAME + "[/] [dim]// signal lost — dashboard closed[/]")
