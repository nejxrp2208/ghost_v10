"""
ghost_telegram_bot.py — Interactive Telegram command bot for Crypto Ghost.

Polls Telegram for /commands and replies with live data from your bot's DBs.
Mirrors Chris's command set (/today /24h /open /coin /help) plus 10+ extras
that leverage chris_ml, chris_analyst, and the agent stack.

Security: only responds to messages from your TELEGRAM_CHAT_ID. Random users
who find your bot can't query it.

Run:
  python ghost_telegram_bot.py          # poll forever
  python ghost_telegram_bot.py --once   # one update cycle, then exit (testing)

Commands implemented:
  /today      — today's P&L (UTC day)
  /24h        — last 24 hours
  /week       — last 7 days
  /all        — all-time stats
  /open       — currently open trades
  /coin       — split by coin (BTC vs ETH)
  /streak     — current streak with stats
  /last       — last 5 trades
  /ml         — chris_ml HOT/DEAD hour classifications
  /best       — best hours to fire UP (Wilson lower CI >= 25%)
  /dead       — dead hours (Wilson upper CI <= 5%)
  /hour HH    — drill into specific ET hour (e.g. /hour 8)
  /recs       — pending agent recommendations
  /agents     — agent health summary
  /scan       — scanner / signal status
  /help       — this menu
"""
import asyncio, aiohttp, sqlite3, os, sys, json, argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    SD = os.path.dirname(os.path.abspath(__file__))
    ep = os.path.join(SD, ".env")
    if os.path.exists(ep): load_dotenv(ep, override=True)
except ImportError:
    SD = os.path.dirname(os.path.abspath(__file__))

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID",   "").strip()
PAPER    = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true","1","yes")
TRADE_DB = os.path.join(SD, "crypto_ghost_PAPER.db" if PAPER else "crypto_ghost.db")
RPT_DB   = os.path.join(SD, "agent_reports.db")

API = f"https://api.telegram.org/bot{TG_TOKEN}"
ARGS = argparse.ArgumentParser()
ARGS.add_argument("--once", action="store_true", help="run one cycle then exit")
ARGS = ARGS.parse_args()


# ── DB helpers ────────────────────────────────────────────────────────────────
def db():
    if not os.path.exists(TRADE_DB):
        return None
    return sqlite3.connect(TRADE_DB, timeout=60)

def rdb():
    if not os.path.exists(RPT_DB):
        return None
    return sqlite3.connect(RPT_DB, timeout=60)


# ── Telegram I/O ──────────────────────────────────────────────────────────────
async def tg_get(session, method, **params):
    url = f"{API}/{method}"
    async with session.get(url, params=params,
                            timeout=aiohttp.ClientTimeout(total=35)) as r:
        if r.status == 200:
            return await r.json()
    return None

async def tg_post(session, method, **payload):
    url = f"{API}/{method}"
    async with session.post(url, json=payload,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
        if r.status == 200:
            return await r.json()
    return None

async def reply(session, chat_id, text, parse_mode="HTML"):
    """Send a message back to the user."""
    return await tg_post(session, "sendMessage",
                          chat_id=chat_id, text=text,
                          parse_mode=parse_mode,
                          disable_web_page_preview=True)


# ── Format helpers ────────────────────────────────────────────────────────────
def _fmt_pnl(pnl):
    sign = '+' if pnl >= 0 else '-'
    return f"${sign}{abs(pnl):,.2f}"

def _bar(top_emoji, title):
    return f"{top_emoji} <b>{title}</b>\n<code>━━━━━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"

def _et_hour(ts_str):
    try:
        d = datetime.fromisoformat(str(ts_str).replace('Z','+00:00'))
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return (d - timedelta(hours=4)).hour
    except:
        return None


# ── Command handlers ──────────────────────────────────────────────────────────
def cmd_today():
    c = db()
    if not c: return "❌ Trade DB not found."
    cur = c.cursor()
    today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    n, w, pnl, stake = cur.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
               COALESCE(SUM(pnl), 0),
               COALESCE(SUM(size_usdc), 0)
        FROM trades WHERE status IN ('won','lost') AND ts LIKE ?
    """, (today_utc + '%',)).fetchone()
    c.close()
    n = n or 0; w = w or 0
    wr = (w / n * 100) if n else 0
    out = _bar("📅", f"Today ({today_utc} UTC)")
    out += f"Closed: <b>{n}</b>  Wins: <b>{w}</b>  WR: <b>{wr:.1f}%</b>\n"
    out += f"Risked: <b>${stake:.2f}</b>\n"
    out += f"PnL:    <b>{_fmt_pnl(pnl)}</b>"
    return out


def cmd_window(hours, label):
    c = db()
    if not c: return "❌ Trade DB not found."
    cur = c.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    n, w, pnl, stake = cur.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
               COALESCE(SUM(pnl), 0),
               COALESCE(SUM(size_usdc), 0)
        FROM trades WHERE status IN ('won','lost') AND ts > ?
    """, (cutoff,)).fetchone()
    c.close()
    n = n or 0; w = w or 0
    wr = (w / n * 100) if n else 0
    out = _bar("⏱", f"Last {label}")
    out += f"Closed: <b>{n}</b>  Wins: <b>{w}</b>  WR: <b>{wr:.1f}%</b>\n"
    out += f"Risked: <b>${stake:.2f}</b>\n"
    out += f"PnL:    <b>{_fmt_pnl(pnl)}</b>"
    return out

def cmd_24h():    return cmd_window(24, "24h")
def cmd_week():   return cmd_window(24*7, "7 days")


def cmd_all():
    c = db()
    if not c: return "❌ Trade DB not found."
    cur = c.cursor()
    n, w, l, pnl, stake = cur.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
               COALESCE(SUM(pnl), 0),
               COALESCE(SUM(size_usdc), 0)
        FROM trades WHERE status IN ('won','lost')
    """).fetchone()
    avg_w = cur.execute("SELECT COALESCE(AVG(pnl),0) FROM trades WHERE status='won'").fetchone()[0]
    avg_l = cur.execute("SELECT COALESCE(AVG(pnl),0) FROM trades WHERE status='lost'").fetchone()[0]
    c.close()
    n = n or 0; w = w or 0; l = l or 0
    wr = (w / n * 100) if n else 0
    roi = (pnl / stake * 100) if stake else 0
    out = _bar("📊", "All-Time Stats")
    out += f"Closed: <b>{n}</b>  W:<b>{w}</b>  L:<b>{l}</b>\n"
    out += f"Win Rate: <b>{wr:.1f}%</b>\n"
    out += f"Total Risked: <b>${stake:.2f}</b>\n"
    out += f"Total PnL:    <b>{_fmt_pnl(pnl)}</b>\n"
    out += f"ROI on stake: <b>{roi:.1f}%</b>\n"
    out += f"Avg win: <b>{_fmt_pnl(avg_w)}</b>  Avg loss: <b>{_fmt_pnl(avg_l)}</b>"
    return out


def cmd_open():
    c = db()
    if not c: return "❌ Trade DB not found."
    cur = c.cursor()
    rows = cur.execute("""
        SELECT id, ts, coin, outcome, entry_price, size_usdc, question
        FROM trades WHERE status='open'
        ORDER BY id DESC
    """).fetchall()
    c.close()
    if not rows:
        return _bar("🎯", "Open Positions") + "<i>No open trades right now.</i>"
    out = _bar("🎯", f"Open Positions ({len(rows)})")
    for r in rows[:10]:
        tid, ts, coin, outcome, entry, size, q = r
        try:
            d = datetime.fromisoformat(str(ts).replace('Z','+00:00'))
            if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
            age_min = int((datetime.now(timezone.utc) - d).total_seconds() / 60)
            age_str = f"{age_min}m ago"
        except:
            age_str = "—"
        potential = round(size * (1 - entry) / entry, 2) if entry > 0 else 0
        out += f"\n<b>#{tid}</b> {coin} <b>{outcome}</b> @${entry:.3f}\n"
        out += f"  size ${size:.2f} → +${potential:.0f} potential · {age_str}"
    if len(rows) > 10:
        out += f"\n\n<i>+{len(rows)-10} more</i>"
    return out


def cmd_coin():
    c = db()
    if not c: return "❌ Trade DB not found."
    cur = c.cursor()
    rows = cur.execute("""
        SELECT coin, COUNT(*),
               SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
               COALESCE(SUM(pnl),0)
        FROM trades WHERE status IN ('won','lost')
        GROUP BY coin ORDER BY 2 DESC
    """).fetchall()
    c.close()
    if not rows:
        return _bar("🪙", "By Coin") + "<i>No closed trades yet.</i>"
    out = _bar("🪙", "By Coin")
    for coin, n, w, pnl in rows:
        wr = (w/n*100) if n else 0
        out += f"\n<b>{coin}</b>: n=<b>{n}</b>  W=<b>{w}</b>  WR=<b>{wr:.1f}%</b>  PnL=<b>{_fmt_pnl(pnl)}</b>"
    return out


def cmd_streak():
    c = db()
    if not c: return "❌ Trade DB not found."
    cur = c.cursor()
    statuses = [r[0] for r in cur.execute("""
        SELECT status FROM trades WHERE status IN ('won','lost')
        ORDER BY id DESC LIMIT 1000
    """).fetchall()]
    c.close()
    if not statuses:
        return _bar("🔥", "Streak") + "<i>No closed trades yet.</i>"
    streak = 0
    first = statuses[0]
    for s in statuses:
        if s == first: streak += 1
        else: break
    emoji = "🟢" if first == 'won' else "🔴"
    sign = '+' if first == 'won' else '-'
    out = _bar("🔥", "Current Streak")
    out += f"\n{emoji} <b>{sign}{streak}</b> {first.upper()} in a row"
    if first == 'lost':
        out += f"\n\n<i>Asymmetric strategy — long loss streaks are normal.\n"
        out += f"One penny-tier win covers ~50-200 losses.</i>"
    return out


def cmd_last():
    c = db()
    if not c: return "❌ Trade DB not found."
    cur = c.cursor()
    rows = cur.execute("""
        SELECT id, ts, coin, outcome, status, entry_price, pnl
        FROM trades WHERE status IN ('won','lost','open')
        ORDER BY id DESC LIMIT 5
    """).fetchall()
    c.close()
    if not rows:
        return _bar("📜", "Last 5 Trades") + "<i>No trades yet.</i>"
    out = _bar("📜", "Last 5 Trades")
    for r in rows:
        tid, ts, coin, outcome, status, entry, pnl = r
        ts_short = str(ts)[11:19]
        if status == 'won':   icon = "✅"
        elif status == 'lost': icon = "❌"
        else:                  icon = "⏳"
        pnl_str = _fmt_pnl(pnl) if pnl is not None else "pending"
        out += f"\n{icon} <b>#{tid}</b> {ts_short} {coin} {outcome} @${entry:.3f}  <b>{pnl_str}</b>"
    return out


def cmd_ml():
    c = rdb()
    if not c: return "❌ ML data not found (chris_ml hasn't run yet)."
    cur = c.cursor()
    has_cls = cur.execute("""
        SELECT name FROM sqlite_master WHERE type='table' AND name='chris_ml_classifications'
    """).fetchone()
    if not has_cls:
        c.close()
        return _bar("🧠", "ML Classifications") + "<i>chris_ml hasn't run yet — wait ~10 min.</i>"
    rows = cur.execute("""
        SELECT et_hour, direction, n, wins, wr, classification
        FROM chris_ml_classifications
        WHERE ts = (SELECT MAX(ts) FROM chris_ml_classifications)
        ORDER BY et_hour, direction
    """).fetchall()
    bt = cur.execute("""
        SELECT actual_pnl, actual_wr, actual_n, if_hot_only_pnl, lift_pnl,
               hot_hours, dead_hours
        FROM chris_ml_backtest ORDER BY ts DESC LIMIT 1
    """).fetchone()
    c.close()
    if not rows:
        return _bar("🧠", "ML Classifications") + "<i>No data yet.</i>"
    out = _bar("🧠", "chris_ml Classifications (UP only)")
    for h, d, n, w, wr, cls in rows:
        if d != 'UP': continue
        if cls == 'HOT': emoji = "🟢"
        elif cls == 'WARM': emoji = "🔵"
        elif cls == 'COLD': emoji = "🟡"
        elif cls == 'DEAD': emoji = "🔴"
        else: emoji = "⚪"
        out += f"\n{emoji} ET <b>{h:02d}</b>  n={n} W={w}  WR={wr:.0f}%  <i>{cls}</i>"
    if bt:
        out += "\n\n<b>📈 Backtest:</b>\n"
        out += f"  Actual: {_fmt_pnl(bt[0])} ({bt[2]} trades)\n"
        if bt[3] is not None and bt[5]:
            out += f"  Hot-only: {_fmt_pnl(bt[3])} (hours [{bt[5]}])\n"
            out += f"  Lift: {_fmt_pnl(bt[4])}"
    return out


def cmd_best():
    c = rdb()
    if not c: return "❌ ML data not found."
    cur = c.cursor()
    has_cls = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chris_ml_classifications'").fetchone()
    if not has_cls:
        c.close()
        return _bar("⭐", "Best Hours to Fire UP") + "<i>chris_ml hasn't run yet.</i>"
    rows = cur.execute("""
        SELECT et_hour, n, wins, wr, lower_ci, upper_ci
        FROM chris_ml_classifications
        WHERE ts = (SELECT MAX(ts) FROM chris_ml_classifications)
          AND direction = 'UP'
          AND classification IN ('HOT','WARM')
        ORDER BY lower_ci DESC
    """).fetchall()
    c.close()
    if not rows:
        return _bar("⭐", "Best Hours to Fire UP") + "<i>No HOT/WARM hours yet (need more data).</i>"
    out = _bar("⭐", "Best Hours to Fire UP")
    for h, n, w, wr, lo, hi in rows:
        out += f"\n🟢 ET <b>{h:02d}:00</b>  n={n} WR={wr:.0f}%  CI [{lo*100:.0f}%-{hi*100:.0f}%]"
    return out


def cmd_dead():
    c = rdb()
    if not c: return "❌ ML data not found."
    cur = c.cursor()
    has_cls = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chris_ml_classifications'").fetchone()
    if not has_cls:
        c.close()
        return _bar("💀", "Dead Hours") + "<i>chris_ml hasn't run yet.</i>"
    rows = cur.execute("""
        SELECT et_hour, direction, n, wins, wr, lower_ci, upper_ci
        FROM chris_ml_classifications
        WHERE ts = (SELECT MAX(ts) FROM chris_ml_classifications)
          AND classification IN ('DEAD','COLD')
        ORDER BY et_hour
    """).fetchall()
    c.close()
    if not rows:
        return _bar("💀", "Dead Hours") + "<i>No DEAD/COLD hours yet (need more data).</i>"
    out = _bar("💀", "Dead Hours (don't fire here)")
    for h, d, n, w, wr, lo, hi in rows:
        out += f"\n🔴 ET <b>{h:02d}</b> {d}  n={n}  WR={wr:.0f}%  CI [≤{hi*100:.0f}%]"
    return out


def cmd_hour(args):
    if not args:
        return "Usage: <code>/hour HH</code> — e.g. <code>/hour 8</code>"
    try: target = int(args[0])
    except: return "Invalid hour. Use 0-23 (ET)."
    c = db()
    if not c: return "❌ Trade DB not found."
    cur = c.cursor()
    rows = cur.execute("""
        SELECT ts, coin, outcome, status, pnl FROM trades
        WHERE status IN ('won','lost')
    """).fetchall()
    c.close()
    n = w = 0; pnl = 0.0
    up_n = up_w = down_n = down_w = 0
    for ts, coin, outcome, st, p in rows:
        if _et_hour(ts) != target: continue
        n += 1
        if st == 'won': w += 1
        pnl += float(p or 0)
        if outcome == 'UP':
            up_n += 1
            if st == 'won': up_w += 1
        elif outcome == 'DOWN':
            down_n += 1
            if st == 'won': down_w += 1
    out = _bar("🕐", f"ET {target:02d}:00 detail")
    if n == 0:
        out += "<i>No trades at this hour yet.</i>"
        return out
    wr = w/n*100
    up_wr = up_w/up_n*100 if up_n else 0
    dn_wr = down_w/down_n*100 if down_n else 0
    out += f"Total: n=<b>{n}</b>  W=<b>{w}</b>  WR=<b>{wr:.1f}%</b>\n"
    out += f"PnL: <b>{_fmt_pnl(pnl)}</b>\n\n"
    out += f"UP:   {up_w}/{up_n} ({up_wr:.0f}%)\n"
    out += f"DOWN: {down_w}/{down_n} ({dn_wr:.0f}%)"
    return out


def cmd_recs():
    c = rdb()
    if not c: return "❌ Reports DB not found."
    cur = c.cursor()
    has = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='param_recommendations'").fetchone()
    if not has:
        c.close()
        return _bar("💡", "Pending Recommendations") + "<i>None yet.</i>"
    rows = cur.execute("""
        SELECT agent, param, current_val, rec_val, reason, MAX(ts) AS latest
        FROM param_recommendations
        GROUP BY agent, param
        ORDER BY priority DESC, latest DESC
        LIMIT 10
    """).fetchall()
    c.close()
    if not rows:
        return _bar("💡", "Pending Recommendations") + "<i>No recommendations yet.</i>"
    out = _bar("💡", "Pending Recommendations")
    for agent, param, cv, rv, reason, ts in rows:
        out += f"\n<b>[{agent}]</b> {param}\n  {cv} → <b>{rv}</b>\n  <i>{(reason or '')[:80]}</i>\n"
    return out


def cmd_agents():
    c = rdb()
    if not c: return "❌ Reports DB not found."
    cur = c.cursor()
    rows = cur.execute("""
        SELECT agent, last_run, status, summary FROM agent_status ORDER BY agent
    """).fetchall()
    c.close()
    if not rows:
        return _bar("🤖", "Agent Health") + "<i>No agent data yet.</i>"
    now = datetime.now(timezone.utc)
    out = _bar("🤖", "Agent Health")
    fresh = stale = 0
    for agent, last_run, status, summary in rows:
        try:
            d = datetime.fromisoformat(last_run.replace('Z','+00:00'))
            if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
            age_min = int((now - d).total_seconds() / 60)
        except:
            age_min = -1
        if age_min < 60: emoji = "🟢"; fresh += 1
        elif age_min < 240: emoji = "🟡"
        else: emoji = "🔴"; stale += 1
        out += f"\n{emoji} <b>{agent}</b> ({age_min}m)  <i>{(summary or '')[:50]}</i>"
    out += f"\n\n<b>{fresh}/{len(rows)}</b> fresh, {stale} stale"
    return out


def cmd_scan():
    c = db()
    if not c: return "❌ Trade DB not found."
    cur = c.cursor()
    last = cur.execute("""
        SELECT ts, markets_found, signals_passed, scan_ms
        FROM scan_stats ORDER BY id DESC LIMIT 1
    """).fetchone()
    n_1m = cur.execute("""
        SELECT COUNT(*) FROM scan_stats WHERE ts > datetime('now','-1 minute')
    """).fetchone()[0]
    c.close()
    out = _bar("🔍", "Scanner Status")
    if last:
        ts, m, s, ms = last
        try:
            d = datetime.fromisoformat(str(ts).replace('Z','+00:00'))
            if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
            age = int((datetime.now(timezone.utc) - d).total_seconds())
        except: age = -1
        out += f"Last scan: <b>{age}s ago</b>\n"
        out += f"Markets: <b>{m}</b>  Signals: <b>{s}</b>  Time: <b>{ms}ms</b>\n"
        out += f"Rate: <b>{n_1m}/min</b>"
    else:
        out += "<i>No scans yet.</i>"
    return out


def cmd_help():
    return ("📚 <b>Crypto Ghost Bot — Commands</b>\n"
            "<code>━━━━━━━━━━━━━━━━━━━━━━━━━━━━</code>\n\n"
            "<b>P&amp;L</b>\n"
            "/today — today's P&amp;L\n"
            "/24h — last 24 hours\n"
            "/week — last 7 days\n"
            "/all — all-time stats\n\n"
            "<b>Positions &amp; Trades</b>\n"
            "/open — currently open trades\n"
            "/last — last 5 trades\n"
            "/streak — current streak\n"
            "/coin — split by coin\n\n"
            "<b>ML &amp; Hours</b>\n"
            "/ml — chris_ml classifications\n"
            "/best — best UP hours\n"
            "/dead — dead hours (don't trade)\n"
            "/hour HH — drill into specific ET hour\n\n"
            "<b>Bot Health</b>\n"
            "/agents — agent health summary\n"
            "/scan — scanner status\n"
            "/recs — pending recommendations\n\n"
            "/help — this menu")


# ── Command dispatcher ────────────────────────────────────────────────────────
COMMANDS = {
    'today':   lambda a: cmd_today(),
    '24h':     lambda a: cmd_24h(),
    'week':    lambda a: cmd_week(),
    'all':     lambda a: cmd_all(),
    'open':    lambda a: cmd_open(),
    'coin':    lambda a: cmd_coin(),
    'streak':  lambda a: cmd_streak(),
    'last':    lambda a: cmd_last(),
    'ml':      lambda a: cmd_ml(),
    'best':    lambda a: cmd_best(),
    'dead':    lambda a: cmd_dead(),
    'hour':    lambda a: cmd_hour(a),
    'recs':    lambda a: cmd_recs(),
    'agents':  lambda a: cmd_agents(),
    'scan':    lambda a: cmd_scan(),
    'help':    lambda a: cmd_help(),
    'start':   lambda a: cmd_help(),  # Telegram default
}


async def handle_command(session, chat_id, text):
    parts = text.strip().split()
    if not parts: return
    cmd = parts[0].lstrip('/').lower().split('@')[0]  # strip @bot suffix
    args = parts[1:]
    handler = COMMANDS.get(cmd)
    if handler is None:
        await reply(session, chat_id,
                     f"Unknown command: <code>/{cmd}</code>. Try /help.")
        return
    try:
        out = handler(args)
    except Exception as e:
        out = f"❌ Error executing /{cmd}: {e}"
    await reply(session, chat_id, out)


async def handle_update(session, update):
    msg = update.get('message')
    if not msg: return
    user_id = str(msg.get('from', {}).get('id', ''))
    chat_id = str(msg.get('chat', {}).get('id', ''))
    text = msg.get('text', '')

    # Security — only respond to the configured chat ID
    if TG_CHAT and chat_id != TG_CHAT and user_id != TG_CHAT:
        print(f"[bot] ignored msg from unauthorized chat_id={chat_id} user_id={user_id}")
        return

    if text.startswith('/'):
        print(f"[bot] {text}")
        await handle_command(session, chat_id, text)


# ── Main poll loop ────────────────────────────────────────────────────────────
async def main():
    if not TG_TOKEN or not TG_CHAT:
        print("ERROR: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID required in .env")
        sys.exit(1)

    print(f"Crypto Ghost Telegram bot started — chat_id={TG_CHAT}")
    print(f"Trade DB: {TRADE_DB}")
    print(f"Reports DB: {RPT_DB}")
    print(f"Commands: {', '.join(sorted(COMMANDS))}")

    offset = 0
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Send startup notification
        await reply(session, TG_CHAT,
                     "🤖 <b>Ghost bot online.</b>\n<i>Send /help for commands.</i>")

        while True:
            try:
                resp = await tg_get(session, "getUpdates",
                                     offset=offset, timeout=30, allowed_updates=json.dumps(['message']))
                if resp and resp.get('ok'):
                    for upd in resp.get('result', []):
                        offset = upd['update_id'] + 1
                        try:
                            await handle_update(session, upd)
                        except Exception as e:
                            print(f"[bot] handle err: {e!r}")
                if ARGS.once:
                    break
            except asyncio.TimeoutError:
                pass  # long-poll timeout, normal
            except Exception as e:
                print(f"[bot] poll err: {e!r}")
                await asyncio.sleep(5)


if __name__ == '__main__':
    asyncio.run(main())
