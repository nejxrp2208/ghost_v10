"""
Crypto Ghost Dashboard — Clean rewrite
"""
import sqlite3, asyncio, aiohttp, os, sys, time, re, json
try:
    from telegram_alerts import send_alert
except Exception:
    async def send_alert(msg): pass
from datetime import datetime, timezone

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
    for ep in [
        os.path.join(sd, ".env"),
        os.path.join(sd, "..", "UnifiedBot", ".env"),
        r"C:\Users\ramos\OneDrive\Desktop\UnifiedBot\.env",
    ]:
        if os.path.exists(ep):
            load_dotenv(ep, override=True)
            break
except ImportError:
    pass

PAPER_TRADE    = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true","1","yes")
FUNDER         = os.getenv("POLYMARKET_PROXY_ADDRESS", "N/A")
PORTFOLIO_SIZE = float(os.getenv("PORTFOLIO_SIZE", "200.0"))
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
DB_PATH        = os.path.join(SCRIPT_DIR, "crypto_ghost_PAPER.db" if PAPER_TRADE else "crypto_ghost.db")
REFRESH        = 5

console = Console()
state   = {
    "prices":       {},
    "activity":     [],
    "last_update":  "—",
    "btc_history":  [],   # list of (timestamp, price)
    "eth_history":  [],   # list of (timestamp, price)
}

# ── DB helpers ────────────────────────────────────────────────────────────────
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, tier INTEGER, coin TEXT,
        market_id TEXT, token_id TEXT, question TEXT, entry_price REAL,
        size_usdc REAL, order_id TEXT, status TEXT DEFAULT 'open',
        pnl REAL DEFAULT 0, closed_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, icon TEXT, msg TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS scan_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
        markets_found INTEGER DEFAULT 0, signals_passed INTEGER DEFAULT 0,
        skip_ask INTEGER DEFAULT 0, skip_liq INTEGER DEFAULT 0,
        skip_certainty INTEGER DEFAULT 0, skip_window INTEGER DEFAULT 0,
        scan_ms INTEGER DEFAULT 0)""")
    conn.commit()
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN pm_result TEXT DEFAULT 'pending'")
        conn.commit()
    except Exception:
        pass
    return conn

def safe_query(sql, params=()):
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = db_connect()
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows
    except Exception:
        return []

def safe_one(sql, params=()):
    rows = safe_query(sql, params)
    return rows[0][0] if rows else 0

# ── Window parser ─────────────────────────────────────────────────────────────
def parse_window(question):
    m = re.search(r'(\d{1,2}:\d{2}(?:AM|PM)?)\s[-–]\s(\d{1,2}:\d{2}(?:AM|PM)?)', str(question))
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return "—"

# ── PM Oracle ─────────────────────────────────────────────────────────────────
async def _query_pm_oracle(session, market_id, our_outcome):
    if not market_id or not str(market_id).isdigit():
        return None
    try:
        url = f"https://gamma-api.polymarket.com/markets/{market_id}"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            data = await r.json()
        outcome_prices = json.loads(data.get("outcomePrices", "[]"))
        outcomes       = json.loads(data.get("outcomes", "[]"))
        if not outcome_prices or not outcomes:
            return None
        prices = [float(p) for p in outcome_prices]
        if max(prices) < 0.99:
            return None  # not settled yet
        winner_idx = prices.index(max(prices))
        winner     = str(outcomes[winner_idx]).upper()
        our        = (our_outcome or "").upper()
        return "won" if winner == our else "lost"
    except Exception:
        return None

async def pm_oracle_checker():
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            await asyncio.sleep(30)
            try:
                rows = safe_query("""
                    SELECT id, market_id, outcome, status, size_usdc, entry_price, question
                    FROM trades
                    WHERE status IN ('won','lost')
                      AND COALESCE(pm_result,'pending') = 'pending'
                      AND closed_at > datetime('now','-3 hours')
                """)
                for tid, market_id, outcome, status, size_usdc, entry_price, question in rows:
                    pm = await _query_pm_oracle(session, market_id, outcome)
                    if pm is None:
                        continue
                    if pm != status:
                        old_pnl = size_usdc if status == "won" else -size_usdc
                        pnl = (round(size_usdc / entry_price - size_usdc, 2)
                               if pm == "won" and entry_price else -size_usdc)
                        try:
                            conn = db_connect()
                            conn.execute(
                                "UPDATE trades SET status=?, pnl=?, pm_result=? WHERE id=?",
                                (pm, pnl, pm, tid))
                            conn.commit()
                            conn.close()
                        except Exception:
                            pass
                        direction = (outcome or "").upper()
                        old_emoji = "✅" if status == "won" else "❌"
                        new_emoji = "✅" if pm == "won" else "❌"
                        msg = (
                            f"⚠️ <b>Oracle Correction — Trade #{tid}</b>\n"
                            f"{direction} {market_id and '' or ''}{question or 'N/A'}\n\n"
                            f"Entry: <b>${size_usdc:.2f}</b> @ ask <b>${entry_price:.3f}</b>\n\n"
                            f"Resolver: {old_emoji} {status.upper()}\n"
                            f"Oracle:   {new_emoji} {pm.upper()}\n\n"
                            f"P&amp;L: <b>${old_pnl:+.2f}</b> → <b>${pnl:+.2f}</b>"
                        )
                        await send_alert(msg)
                    else:
                        try:
                            conn = db_connect()
                            conn.execute(
                                "UPDATE trades SET pm_result=? WHERE id=?", (pm, tid))
                            conn.commit()
                            conn.close()
                        except Exception:
                            pass
            except Exception:
                pass

# ── Stats ─────────────────────────────────────────────────────────────────────
def get_stats():
    today = datetime.now().strftime("%Y-%m-%d")
    wins      = safe_one("SELECT COUNT(*) FROM trades WHERE status='won'")
    losses    = safe_one("SELECT COUNT(*) FROM trades WHERE status='lost'")
    open_     = safe_one("SELECT COUNT(*) FROM trades WHERE status='open'")
    total_pnl = safe_one("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status IN ('won','lost')")
    daily_pnl = safe_one("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts LIKE ? AND status IN ('won','lost')", (f"{today}%",))
    spent     = safe_one("SELECT COALESCE(SUM(size_usdc),0) FROM trades WHERE status='open'")
    volume    = safe_one("SELECT COALESCE(SUM(size_usdc),0) FROM trades")
    total     = wins + losses
    wr        = (wins / total * 100) if total > 0 else 0.0
    cash      = round(PORTFOLIO_SIZE + total_pnl - spent, 2)
    fees      = round(volume * 0.02, 4)
    # Drawdown
    pnls = safe_query("SELECT pnl FROM trades WHERE status IN ('won','lost') ORDER BY id")
    peak = cur = dd = 0.0
    for (p,) in pnls:
        cur += (p or 0)
        if cur > peak: peak = cur
        dd = max(dd, peak - cur)
    dd_pct  = (dd / PORTFOLIO_SIZE * 100) if PORTFOLIO_SIZE > 0 else 0.0
    dd_left = max(0, PORTFOLIO_SIZE * 0.25 - dd)
    return dict(wins=wins, losses=losses, open=open_, total_pnl=total_pnl,
                daily_pnl=daily_pnl, win_rate=wr, volume=volume, fees=fees,
                cash=cash, drawdown_pct=dd_pct, dd_left=dd_left)

# ── Async price fetcher ───────────────────────────────────────────────────────
async def fetch_prices(session):
    sym_map = {"BTCUSDT":"BTC","ETHUSDT":"ETH","SOLUSDT":"SOL","XRPUSDT":"XRP","BNBUSDT":"BNB"}
    for base in ["https://api.binance.com","https://api.binance.us"]:
        try:
            async with session.get(f"{base}/api/v3/ticker/price",
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data   = await r.json()
                    prices = {sym_map[i["symbol"]]: float(i["price"])
                              for i in data if i["symbol"] in sym_map}
                    if prices: return prices
        except Exception:
            continue
    return {}

async def data_loop():
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                prices = await fetch_prices(session)
                if prices:
                    state["prices"] = prices
                    now = datetime.now()
                    if "BTC" in prices:
                        state["btc_history"].append((now, prices["BTC"]))
                        state["btc_history"] = state["btc_history"][-120:]  # 10 min of data
                    if "ETH" in prices:
                        state["eth_history"].append((now, prices["ETH"]))
                        state["eth_history"] = state["eth_history"][-120:]
                state["last_update"] = datetime.now().strftime("%H:%M:%S")
            except Exception:
                pass
            await asyncio.sleep(REFRESH)

# ── Color helpers ─────────────────────────────────────────────────────────────
def g(v):   return f"[green]{v}[/]"
def r(v):   return f"[red]{v}[/]"
def c(v):   return f"[cyan]{v}[/]"
def dim(v): return f"[dim]{v}[/]"

def pnl_str(v):
    if not v: return dim("+$0.00 USDC")
    if v > 0: return g(f"+${v:.2f} USDC")
    return r(f"-${abs(v):.2f} USDC")

# ── Render: Statistics ────────────────────────────────────────────────────────
def render_statistics():
    s      = get_stats()
    prices = state["prices"]

    wr   = s.get("win_rate", 0)
    wrc  = "green" if wr >= 50 else "red"
    mode = "[bold green]PAPER[/]" if PAPER_TRADE else "[bold red]LIVE[/]"
    dd   = s.get("drawdown_pct", 0)
    ddl  = s.get("dd_left", 0)

    t = Table.grid(padding=(0,2))
    t.add_column(style="cyan",  width=13, no_wrap=True)
    t.add_column(style="white", width=18, no_wrap=True)

    t.add_row("Portfolio",  Text.from_markup(c(f"${PORTFOLIO_SIZE:.2f} USDC")))
    t.add_row("Cash",       Text.from_markup(c(f"${s.get('cash', PORTFOLIO_SIZE):.2f} USDC")))
    t.add_row("Daily P&L",  Text.from_markup(pnl_str(s.get("daily_pnl", 0))))
    t.add_row("Total P&L",  Text.from_markup(pnl_str(s.get("total_pnl", 0))))
    t.add_row("Wins",       Text.from_markup(g(str(s.get("wins", 0)))))
    t.add_row("Losses",     Text.from_markup(r(str(s.get("losses", 0))) if s.get("losses") else dim("0")))
    t.add_row("Win Rate",   Text.from_markup(f"[{wrc}]{wr:.1f}%[/]"))
    t.add_row("Est. Fees",  Text.from_markup(dim(f"${s.get('fees', 0):.4f}")))
    t.add_row("Drawdown",   Text.from_markup(f"[{'red' if dd>5 else 'green'}]{dd:.1f}% / 25%[/]"))
    t.add_row("DD Left",    Text.from_markup(f"[{'red' if ddl<10 else 'green'}]${ddl:.2f}[/]"))
    t.add_row("Open",       f"{s.get('open', 0)}/20")
    t.add_row("Kill Switch",Text.from_markup(g("OK")))
    t.add_row("Mode",       Text.from_markup(mode))
    t.add_row("", "")
    t.add_row(dim("T1 Strike"),  Text.from_markup(c("$5 → ~$495")))
    t.add_row(dim("T2 Hourly"),  Text.from_markup(c("$3 → ~$95")))
    t.add_row(dim("T3 4hr"),     Text.from_markup(c("$3 → ~$45")))
    t.add_row(dim("T4 Daily"),   Text.from_markup(c("$2 → ~$25")))
    t.add_row("", "")
    t.add_row("BTC", Text.from_markup(c(f"${prices.get('BTC',0):,.2f}")))
    t.add_row("ETH", Text.from_markup(c(f"${prices.get('ETH',0):,.2f}")))
    t.add_row("SOL", Text.from_markup(c(f"${prices.get('SOL',0):,.2f}")))
    t.add_row("XRP", Text.from_markup(c(f"${prices.get('XRP',0):,.4f}")))
    t.add_row("BNB", Text.from_markup(c(f"${prices.get('BNB',0):,.2f}")))

    return Panel(t, title="[bold white]Statistics[/]",
                 border_style="white", box=rbox.SQUARE, padding=(1,1))

# ── Render: Open Positions ────────────────────────────────────────────────────
def render_open_positions():
    rows = safe_query("""
        SELECT id, tier, coin, question, entry_price, size_usdc, ts
        FROM trades WHERE status='open' ORDER BY id DESC LIMIT 12
    """)
    t = Table(box=rbox.SIMPLE_HEAD, border_style="white",
              show_header=True, header_style="bold white",
              expand=True, padding=(0,1))
    t.add_column("ID",    width=4,  style="dim white")
    t.add_column("Market",min_width=38)
    t.add_column("Out",   width=8)
    t.add_column("Side",  width=4)
    t.add_column("Our $", width=6,  justify="right")
    t.add_column("Entry", width=7,  justify="right")
    t.add_column("Edge%", width=7,  justify="right")
    t.add_column("Time",  width=8,  style="dim")

    colors = {1:"yellow", 2:"green", 3:"cyan", 4:"magenta",
              6:"bright_black", 7:"bright_blue", 8:"bright_magenta"}
    for rid, tier, coin, q, entry, size, ts in rows:
        col  = colors.get(tier, "white")
        edge = round((1/entry - 1)*100, 0) if entry else 0
        tstr = ts[11:19] if ts else "?"
        t.add_row(str(rid),
                  Text(str(q)[:44], overflow="ellipsis"),
                  Text.from_markup(f"[{col}]T{tier} {coin}[/]"),
                  Text.from_markup("[green]BUY[/]"),
                  f"${size:.2f}", f"{entry:.3f}",
                  f"{edge:.0f}%", tstr)

    return Panel(t, title=f"[bold white]Open Positions ({len(rows)})[/]",
                 border_style="white", box=rbox.SQUARE, padding=(0,0))

# ── Render: Price Charts + Scanner Stats ─────────────────────────────────────
def render_scanner_stats():
    """Live BTC/ETH price chart with scanner stats below."""

    def make_chart(history, coin, color, width=28, height=8):
        """Draw an ASCII sparkline chart."""
        if len(history) < 2:
            lines = [f"[dim]Waiting for {coin} data...[/]"]
            return lines

        prices = [p for _, p in history]
        lo, hi = min(prices), max(prices)
        rng    = hi - lo if hi != lo else 1

        # Build chart rows top to bottom
        rows = []
        for row in range(height - 1, -1, -1):
            threshold = lo + (row / (height - 1)) * rng
            line      = ""
            step      = max(1, len(prices) // width)
            for i in range(0, min(len(prices), width * step), step):
                p = prices[i]
                if p >= threshold - rng * 0.06:
                    line += f"[{color}]▄[/]"
                else:
                    line += " "
            rows.append(line)

        # Price labels
        last  = prices[-1]
        first = prices[0]
        chg   = last - first
        chg_p = chg / first * 100 if first else 0
        sign  = "+" if chg >= 0 else ""
        chg_c = "green" if chg >= 0 else "red"

        header = (f"[bold {color}]{coin}[/] "
                  f"[white]${last:,.2f}[/]  "
                  f"[{chg_c}]{sign}{chg_p:.2f}%[/]")
        return [header] + rows

    t = Table.grid(padding=(0, 0))
    t.add_column(min_width=28)

    btc_lines = make_chart(state["btc_history"], "BTC", "yellow")
    eth_lines = make_chart(state["eth_history"], "ETH", "cyan")

    for line in btc_lines:
        t.add_row(Text.from_markup(line) if line else Text(""))
    t.add_row(Text(""))
    for line in eth_lines:
        t.add_row(Text.from_markup(line) if line else Text(""))

    # Scanner stats below chart
    t.add_row(Text(""))
    rows_db = safe_query("""
        SELECT markets_found, signals_passed, skip_ask, scan_ms
        FROM scan_stats ORDER BY id DESC LIMIT 100
    """)

    if rows_db:
        avg_ms     = int(sum(r[3] for r in rows_db) / len(rows_db))
        total_sig  = sum(r[1] for r in rows_db)
        skip_ask   = sum(r[2] for r in rows_db)
        sig_rate   = total_sig / len(rows_db) * 100

        speed_c = "green" if avg_ms < 3000 else "yellow"
        sig_c   = "green" if sig_rate > 1  else "dim white"

        t.add_row(Text.from_markup(f"[dim]Scans:[/] [white]{len(rows_db)}[/]  "
                                   f"[dim]Speed:[/] [{speed_c}]{avg_ms}ms[/]"))
        t.add_row(Text.from_markup(f"[dim]Signals:[/] [{sig_c}]{total_sig}[/]  "
                                   f"[dim]Skip:[/] [white]{skip_ask}[/]"))
    else:
        t.add_row(Text.from_markup("[dim]Waiting for scan data...[/]"))

    t.add_row(Text.from_markup(f"[dim]Entry T2:[/] [white]{os.getenv('T2_MAX_ENTRY','0.03')}[/]  "
                               f"[dim]Cert:[/] [white]{os.getenv('MIN_NO_PRICE','0.97')}[/]"))
    t.add_row(Text.from_markup(f"[dim]Window:[/] [white]{os.getenv('T2_WINDOW_MIN','3')}min[/]  "
                               f"[dim]Liq:[/] [white]${os.getenv('MIN_LIQUIDITY','0.5')}[/]"))

    return Panel(t, title="[bold white]📈 Live Price Chart[/]",
                 border_style="white", box=rbox.SQUARE, padding=(0, 1))

# ── Render: Last Trades ───────────────────────────────────────────────────────
def render_last_trades():
    rows = safe_query("""
        SELECT id, ts, tier, coin, question, size_usdc, pnl, status,
               COALESCE(outcome,'?') as outcome,
               COALESCE(pm_result,'pending') as pm_result,
               market_id, entry_price
        FROM trades ORDER BY id DESC LIMIT 10
    """)
    t = Table(box=rbox.SIMPLE_HEAD, border_style="white",
              show_header=True, header_style="bold white",
              expand=True, padding=(0,1))
    t.add_column("ID",     width=4,  style="dim white")
    t.add_column("Time",   width=8,  style="dim white")
    t.add_column("Market", min_width=24)
    t.add_column("Dir",    width=4)
    t.add_column("Window", width=13, style="dim white")
    t.add_column("Oracle", width=8)
    t.add_column("Out",    width=5)
    t.add_column("$",      width=6,  justify="right")
    t.add_column("P&L",    width=8,  justify="right")
    t.add_column("Status", width=6)

    colors = {1:"yellow", 2:"green", 3:"cyan", 4:"magenta",
              6:"bright_black", 7:"bright_blue", 8:"bright_magenta"}
    for rid, ts, tier, coin, q, size, pnl, status, outcome, pm_result, market_id, entry_price in rows:
        tstr = ts[11:19] if ts else "?"
        pstr = f"+${pnl:.2f}" if (pnl and pnl>0) else (f"-${abs(pnl):.2f}" if pnl else "—")
        pc   = "green" if (pnl and pnl>0) else ("red" if (pnl and pnl<0) else "dim white")
        col  = colors.get(tier, "white")
        stxt = (Text.from_markup("[bold green]WON[/]")  if status=="won"  else
                Text.from_markup("[bold red]LOST[/]")   if status=="lost" else
                Text.from_markup("[green]OPEN[/]"))
        direction = (outcome or "").upper()
        dtxt = (Text.from_markup("[bold green]UP[/]")   if direction == "UP"   else
                Text.from_markup("[bold red]DOWN[/]")   if direction == "DOWN" else
                Text("—", style="dim white"))
        if pm_result == "won":
            otxt = Text.from_markup("[bold green]WON ✓[/]")
        elif pm_result == "lost":
            otxt = Text.from_markup("[bold red]LOST ✗[/]")
        else:
            otxt = Text("pending", style="dim white")
        t.add_row(str(rid), tstr,
                  Text(str(q), overflow="fold"),
                  dtxt,
                  parse_window(q),
                  otxt,
                  Text.from_markup(f"[{col}]T{tier}[/]"),
                  f"${size:.2f}",
                  Text.from_markup(f"[{pc}]{pstr}[/]"),
                  stxt)

    return Panel(t, title="[bold white]Last 10 Trades[/]",
                 border_style="white", box=rbox.SQUARE, padding=(0,0))

# ── Render: Activity ──────────────────────────────────────────────────────────
def render_activity():
    rows = safe_query("SELECT ts, icon, msg FROM events ORDER BY id DESC LIMIT 20")

    t = Table.grid(padding=(0,1))
    t.add_column(style="dim white", width=8,  no_wrap=True)
    t.add_column(style="white",     width=3,  no_wrap=True)
    t.add_column(min_width=34, overflow="fold")

    if rows:
        for ts, icon, msg in rows:
            mc = ("green"  if icon in ("✅",) or "WON" in str(msg)
                  else "red"    if icon in ("✗",)  or "LOST" in str(msg)
                  else "cyan"   if icon in ("🎯",)
                  else "yellow" if icon in ("📋",)
                  else "white")
            t.add_row(str(ts), str(icon),
                      Text.from_markup(f"[{mc}]{str(msg)}[/]"))
    else:
        for ts, msg in reversed(state["activity"][-15:]):
            t.add_row(str(ts), "", str(msg))
        if not state["activity"]:
            t.add_row("", "", Text.from_markup(dim("Waiting for activity...")))

    return Panel(t,
                 title=f"[bold white]Activity[/]  [dim]{state['last_update']}[/]",
                 border_style="white", box=rbox.SQUARE, padding=(1,1))

def log_activity(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    state["activity"].append((ts, str(msg)))
    if len(state["activity"]) > 60:
        state["activity"] = state["activity"][-60:]

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    log_activity("Crypto Ghost Scanner started")
    log_activity(f"Mode: {'PAPER' if PAPER_TRADE else 'LIVE'}")
    log_activity(f"Portfolio: ${PORTFOLIO_SIZE:.2f}")
    log_activity("Tracking BTC/ETH/XRP/SOL markets")

    asyncio.create_task(data_loop())
    asyncio.create_task(pm_oracle_checker())

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="top",    ratio=3),
        Layout(name="bottom", ratio=2),
    )
    layout["top"].split_row(
        Layout(name="stats",     ratio=1),
        Layout(name="positions", ratio=2),
        Layout(name="odds",      ratio=1),
    )
    layout["bottom"].split_row(
        Layout(name="trades",   ratio=2),
        Layout(name="activity", ratio=1),
    )

    prev_open = 0
    prev_wins = 0

    PANELS = [
        ("stats",     render_statistics),
        ("positions", render_open_positions),
        ("odds",      render_scanner_stats),
        ("trades",    render_last_trades),
        ("activity",  render_activity),
    ]

    with Live(layout, console=console, refresh_per_second=1, screen=True):
        while True:
            try:
                now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
                mode = "[bold green]PAPER TRADING[/]" if PAPER_TRADE else "[bold red]LIVE TRADING[/]"

                hdr = Table.grid(expand=True)
                hdr.add_column(justify="center")
                hdr.add_row(Text.from_markup(
                    f"[bold white]POLYMARKET CRYPTO GHOST BOT[/]   {mode}   [dim]{now}[/]"
                ))
                layout["header"].update(
                    Panel(hdr, border_style="white", box=rbox.HORIZONTALS, padding=(0,1))
                )

                for name, fn in PANELS:
                    try:
                        layout[name].update(fn())
                    except Exception as ex:
                        try:
                            layout[name].update(Panel(
                                Text(f"Error in {name}: {str(ex)[:80]}"),
                                border_style="red"
                            ))
                        except Exception:
                            pass

                s = get_stats()
                cur_open = s.get("open", 0)
                cur_wins = s.get("wins", 0)
                if cur_open > prev_open:
                    log_activity(f"New position opened (open: {cur_open})")
                if cur_wins > prev_wins:
                    log_activity(f"Trade resolved WIN (wins: {cur_wins})")
                prev_open = cur_open
                prev_wins = cur_wins

            except Exception:
                pass

            await asyncio.sleep(REFRESH)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[white]Dashboard closed.[/]")
