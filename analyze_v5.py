"""
Fresh Analysis — v6 Resolver Verified Trades (auto-detects schema)
"""
import sqlite3, os, sys
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

PAPER_TRADE = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true","1","yes")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
            "crypto_ghost_PAPER.db" if PAPER_TRADE else "crypto_ghost.db")

if not os.path.exists(DB_PATH):
    print("No DB found.")
    sys.exit(0)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
print(f"[INFO] Available columns: {cols}\n")

has_verified   = 'price_start' in cols and 'price_end' in cols
has_pm_result  = 'pm_result' in cols
has_res_code   = 'resolution_verified' in cols

base_cols = ['id', 'coin', 'outcome', 'status', 'pnl']
if 'entry_price' in cols: base_cols.append('entry_price')
if 'size_usdc'   in cols: base_cols.append('size_usdc')
if 'question'    in cols: base_cols.append('question')
if 'ts'          in cols: base_cols.append('ts')
if has_verified:
    base_cols.extend(['price_start', 'price_end'])
    if 'resolution_verified' in cols:
        base_cols.append('resolution_verified')
if has_pm_result:
    base_cols.append('pm_result')

sel = ", ".join(base_cols)
rows = conn.execute(f"""
    SELECT {sel}
    FROM trades
    WHERE status IN ('won','lost')
    ORDER BY id ASC
""").fetchall()

if not rows:
    print("No closed trades yet.")
    sys.exit(0)

total  = len(rows)
wins   = sum(1 for r in rows if r['status'] == 'won')
losses = sum(1 for r in rows if r['status'] == 'lost')
win_rate  = (wins / total * 100) if total else 0
total_pnl = sum(r['pnl'] or 0 for r in rows)
avg_win   = sum(r['pnl'] or 0 for r in rows if r['status'] == 'won')  / wins   if wins   else 0
avg_loss  = sum(r['pnl'] or 0 for r in rows if r['status'] == 'lost') / losses if losses else 0

longest_win = longest_loss = current_win = current_loss = 0
for r in rows:
    if r['status'] == 'won':
        current_win += 1; current_loss = 0
        longest_win = max(longest_win, current_win)
    else:
        current_loss += 1; current_win = 0
        longest_loss = max(longest_loss, current_loss)

by_coin = {}
for r in rows:
    c = r['coin']
    if c not in by_coin:
        by_coin[c] = {'wins': 0, 'losses': 0, 'pnl': 0}
    by_coin[c]['wins']   += 1 if r['status'] == 'won' else 0
    by_coin[c]['losses'] += 1 if r['status'] == 'lost' else 0
    by_coin[c]['pnl']    += r['pnl'] or 0

up_trades   = [r for r in rows if (r['outcome'] or '').upper() in ('UP','YES','HIGHER')]
down_trades = [r for r in rows if (r['outcome'] or '').upper() not in ('UP','YES','HIGHER')]
up_wins     = sum(1 for r in up_trades   if r['status'] == 'won')
down_wins   = sum(1 for r in down_trades if r['status'] == 'won')

avg_payout   = avg_win / abs(avg_loss) if avg_loss else 0
breakeven_wr = 100 / (1 + avg_payout)  if avg_payout else 0

running = 0
curve   = []
for r in rows:
    running += r['pnl'] or 0
    curve.append(running)
max_drawdown = 0
peak = 0
for val in curve:
    peak = max(peak, val)
    dd   = peak - val
    max_drawdown = max(max_drawdown, dd)

print(f"""╔═══════════════════════════════════════════════════════════╗
║         CRYPTO GHOST — VERIFIED PERFORMANCE              ║
║         Resolver: {"v5 Binance-verified" if has_verified else "Legacy (UNTRUSTED)":<38} ║
╚═══════════════════════════════════════════════════════════╝

  OVERALL STATS
  ─────────────
  Total trades       : {total}
  Wins / Losses      : {wins} / {losses}
  Win rate           : {win_rate:.1f}%
  Total P&L          : ${total_pnl:+.2f}
  Max drawdown       : ${max_drawdown:.2f}
  Avg win            : ${avg_win:+.2f}
  Avg loss           : ${avg_loss:+.2f}
  Payout ratio       : {avg_payout:.1f}:1
  Break-even WR      : {breakeven_wr:.1f}%
  Edge vs breakeven  : {win_rate - breakeven_wr:+.1f}pp

  STREAKS
  ───────
  Longest win streak : {longest_win}
  Longest loss streak: {longest_loss}

  BY COIN""")
for coin, s in sorted(by_coin.items()):
    total_c = s['wins'] + s['losses']
    wr = (s['wins'] / total_c * 100) if total_c else 0
    print(f"  {coin:<4}: {s['wins']:>3}W / {s['losses']:>3}L | WR {wr:5.1f}% | P&L ${s['pnl']:+.2f}")

up_wr   = (up_wins/len(up_trades)*100)     if up_trades   else 0
down_wr = (down_wins/len(down_trades)*100) if down_trades else 0
print(f"""
  BY SIDE
  ───────
  UP   : {len(up_trades)} trades, {up_wins} won ({up_wr:.1f}% WR)
  DOWN : {len(down_trades)} trades, {down_wins} won ({down_wr:.1f}% WR)

  VERDICT
  ───────""")

if total < 30:
    print(f"  ⚠  Only {total} trades — not enough data.")
elif win_rate < breakeven_wr - 1:
    print(f"  ❌ Below breakeven ({win_rate:.1f}% < {breakeven_wr:.1f}%). Losing money.")
elif win_rate > breakeven_wr + 3:
    print(f"  ✅ Above breakeven by {win_rate-breakeven_wr:.1f}pp. Profitable on paper.")
else:
    print(f"  ⚠  Marginal. Within noise of breakeven.")

if has_verified and 'resolution_verified' in cols:
    v = sum(1 for r in rows if r['resolution_verified'])
    print(f"  ✓ {v}/{total} trades verified against Chainlink prices")

# ── WINNING TRADES DETAIL ─────────────────────────────────────────────────────
_secs_col = ", secs_left" if 'secs_left' in cols else ""
win_rows = conn.execute(f"""
    SELECT id, ts, coin, outcome, entry_price, size_usdc, pnl, question{_secs_col}
    FROM trades WHERE status='won' ORDER BY id ASC
""").fetchall()

print(f"\n  WINNING TRADES ({len(win_rows)} total)")
print("  " + "─"*72)
print(f"  {'ID':>4}  {'Time':16}  {'Coin':4}  {'Dir':4}  {'Ask':6}  {'Size':6}  {'Secs':>4}  {'P&L':>10}  Question")
print("  " + "─"*72)
for w in win_rows:
    ts_str  = (w['ts'] or '')[:16]
    q_short = (w['question'] or '')[:30]
    secs    = w['secs_left'] if 'secs_left' in cols and w['secs_left'] is not None else '?'
    print(f"  {w['id']:>4}  {ts_str:16}  {w['coin']:4}  {(w['outcome'] or ''):4}  "
          f"${w['entry_price']:.3f}  ${w['size_usdc']:.2f}  {str(secs):>4}s  ${w['pnl']:>+8.2f}  {q_short}")

# ── LOSING TRADES DETAIL ──────────────────────────────────────────────────────
loss_rows = conn.execute(f"""
    SELECT id, ts, coin, outcome, entry_price, size_usdc, pnl, question{_secs_col}
    FROM trades WHERE status='lost' ORDER BY id ASC
""").fetchall()

print(f"\n  LOSING TRADES ({len(loss_rows)} total)")
print("  " + "─"*72)
print(f"  {'ID':>4}  {'Time':16}  {'Coin':4}  {'Dir':4}  {'Ask':6}  {'Size':6}  {'Secs':>4}  {'P&L':>10}  Question")
print("  " + "─"*72)
for w in loss_rows:
    ts_str  = (w['ts'] or '')[:16]
    q_short = (w['question'] or '')[:30]
    secs    = w['secs_left'] if 'secs_left' in cols and w['secs_left'] is not None else '?'
    print(f"  {w['id']:>4}  {ts_str:16}  {w['coin']:4}  {(w['outcome'] or ''):4}  "
          f"${w['entry_price']:.3f}  ${w['size_usdc']:.2f}  {str(secs):>4}s  ${w['pnl']:>+8.2f}  {q_short}")

# ── BY ENTRY PRICE ────────────────────────────────────────────────────────────
print(f"\n  BY ENTRY PRICE (ask)")
print("  " + "─"*45)
if 'entry_price' in cols:
    buckets = {}
    for r in rows:
        ep = r['entry_price'] or 0
        if ep <= 0.01:   key = "$0.010"
        elif ep <= 0.02: key = "$0.020"
        elif ep <= 0.03: key = "$0.030"
        elif ep <= 0.05: key = "$0.050"
        else:            key = ">$0.05"
        if key not in buckets:
            buckets[key] = {'w': 0, 'l': 0, 'pnl': 0}
        buckets[key]['w']   += 1 if r['status'] == 'won' else 0
        buckets[key]['l']   += 1 if r['status'] == 'lost' else 0
        buckets[key]['pnl'] += r['pnl'] or 0
    for key in sorted(buckets):
        b  = buckets[key]
        t  = b['w'] + b['l']
        wr = (b['w'] / t * 100) if t else 0
        print(f"  {key}: {b['w']:>3}W / {b['l']:>3}L | WR {wr:5.1f}% | P&L ${b['pnl']:+.2f}")

# ── BY HOUR OF DAY ────────────────────────────────────────────────────────────
print(f"\n  BY HOUR (UTC)")
print("  " + "─"*45)
if 'ts' in cols:
    hours = {}
    all_ts_rows = conn.execute(
        "SELECT ts, status, pnl FROM trades WHERE status IN ('won','lost')").fetchall()
    for r in all_ts_rows:
        try:
            hour = int((r['ts'] or '00:00')[11:13])
        except Exception:
            continue
        if hour not in hours:
            hours[hour] = {'w': 0, 'l': 0, 'pnl': 0}
        hours[hour]['w']   += 1 if r['status'] == 'won' else 0
        hours[hour]['l']   += 1 if r['status'] == 'lost' else 0
        hours[hour]['pnl'] += r['pnl'] or 0
    for h in sorted(hours):
        b  = hours[h]
        t  = b['w'] + b['l']
        wr = (b['w'] / t * 100) if t else 0
        bar = "★" * b['w']
        print(f"  {h:02d}:00  {b['w']:>2}W/{b['l']:>3}L  WR {wr:5.1f}%  P&L ${b['pnl']:+7.2f}  {bar}")

# ── BY DAY OF WEEK ────────────────────────────────────────────────────────────
print(f"\n  BY DAY OF WEEK")
print("  " + "─"*45)
if 'ts' in cols:
    days = {}
    day_names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    for r in all_ts_rows:
        try:
            dt  = datetime.fromisoformat((r['ts'] or '')[:19])
            dow = dt.weekday()
        except Exception:
            continue
        if dow not in days:
            days[dow] = {'w': 0, 'l': 0, 'pnl': 0}
        days[dow]['w']   += 1 if r['status'] == 'won' else 0
        days[dow]['l']   += 1 if r['status'] == 'lost' else 0
        days[dow]['pnl'] += r['pnl'] or 0
    for d in sorted(days):
        b  = days[d]
        t  = b['w'] + b['l']
        wr = (b['w'] / t * 100) if t else 0
        print(f"  {day_names[d]}  {b['w']:>3}W/{b['l']:>3}L  WR {wr:5.1f}%  P&L ${b['pnl']:+7.2f}")

# ── RECENT TREND (last 20 vs all) ─────────────────────────────────────────────
print(f"\n  RECENT TREND")
print("  " + "─"*45)
if total >= 10:
    last20 = rows[-20:]
    l20_w  = sum(1 for r in last20 if r['status'] == 'won')
    l20_wr = (l20_w / len(last20) * 100) if last20 else 0
    l20_pnl = sum(r['pnl'] or 0 for r in last20)
    trend = "📈 Improving" if l20_wr > win_rate else ("📉 Declining" if l20_wr < win_rate - 5 else "➡ Stable")
    print(f"  All-time : {win_rate:.1f}% WR | ${total_pnl:+.2f}")
    print(f"  Last 20  : {l20_wr:.1f}% WR | ${l20_pnl:+.2f}  {trend}")
    if total >= 40:
        last40  = rows[-40:]
        l40_w   = sum(1 for r in last40 if r['status'] == 'won')
        l40_wr  = (l40_w / len(last40) * 100) if last40 else 0
        l40_pnl = sum(r['pnl'] or 0 for r in last40)
        print(f"  Last 40  : {l40_wr:.1f}% WR | ${l40_pnl:+.2f}")
else:
    print(f"  Not enough trades for trend ({total}/10 minimum)")

# ── ORACLE CORRECTIONS ────────────────────────────────────────────────────────
if has_pm_result and has_res_code:
    print(f"\n  ORACLE CORRECTIONS (PM vs Chainlink)")
    print("  " + "─"*45)
    overrides = conn.execute("""
        SELECT COUNT(*) FROM trades
        WHERE resolution_verified = 11
    """).fetchone()[0]
    confirmed = conn.execute("""
        SELECT COUNT(*) FROM trades
        WHERE resolution_verified = 12
    """).fetchone()[0]
    pm_pending = conn.execute("""
        SELECT COUNT(*) FROM trades
        WHERE status IN ('won','lost')
          AND COALESCE(pm_result,'pending') = 'pending'
    """).fetchone()[0]
    print(f"  PM confirmed (match)    : {confirmed}")
    print(f"  PM overridden (mismatch): {overrides}")
    print(f"  PM still pending        : {pm_pending}")
    if overrides > 0:
        override_rows = conn.execute("""
            SELECT id, coin, outcome, pnl, question
            FROM trades WHERE resolution_verified = 11
            ORDER BY id ASC
        """).fetchall()
        print(f"\n  Overridden trades:")
        for ov in override_rows:
            q_short = (ov['question'] or '')[:40]
            print(f"    #{ov['id']} {ov['coin']} {ov['outcome']} → "
                  f"${ov['pnl']:+.2f}  {q_short}")

# ── KELLY CRITERION ───────────────────────────────────────────────────────────
print(f"\n  KELLY CRITERION")
print("  " + "─"*45)
if wins > 0 and losses > 0 and avg_payout > 0:
    p  = win_rate / 100
    q  = 1 - p
    b  = avg_payout
    kelly     = (b * p - q) / b
    half_kelly = kelly / 2
    print(f"  Win prob (p)   : {p:.3f}")
    print(f"  Payout ratio (b): {b:.2f}:1")
    print(f"  Full Kelly     : {kelly*100:.1f}% of bankroll per trade")
    print(f"  Half Kelly     : {half_kelly*100:.1f}% of bankroll per trade")
    if kelly <= 0:
        print(f"  ⚠  Negative Kelly — edge doesn't justify trading at these odds")
    else:
        bankroll = float(os.getenv("PORTFOLIO_SIZE", "200"))
        suggested = round(bankroll * half_kelly, 2)
        print(f"  Suggested size (½K on ${bankroll:.0f}): ${suggested:.2f} per trade")
else:
    print(f"  Not enough data for Kelly calculation")

# ── BY ENTRY PRICE × COIN ────────────────────────────────────────────────────
if 'entry_price' in cols:
    print(f"\n  BY ENTRY PRICE × COIN")
    print("  " + "─"*55)
    cross = {}
    for r in rows:
        ep = r['entry_price'] or 0
        if ep <= 0.01:   price_key = "$0.010"
        elif ep <= 0.02: price_key = "$0.020"
        elif ep <= 0.03: price_key = "$0.030"
        else:            price_key = ">$0.03"
        coin = r['coin']
        key  = (coin, price_key)
        if key not in cross:
            cross[key] = {'w': 0, 'l': 0, 'pnl': 0}
        cross[key]['w']   += 1 if r['status'] == 'won' else 0
        cross[key]['l']   += 1 if r['status'] == 'lost' else 0
        cross[key]['pnl'] += r['pnl'] or 0
    for (coin, price_key) in sorted(cross):
        b  = cross[(coin, price_key)]
        t  = b['w'] + b['l']
        wr = (b['w'] / t * 100) if t else 0
        print(f"  {coin:<4} {price_key}: {b['w']:>3}W / {b['l']:>3}L | WR {wr:5.1f}% | P&L ${b['pnl']:+.2f}")

# ── DAILY P&L ─────────────────────────────────────────────────────────────────
if 'ts' in cols:
    print(f"\n  DAILY P&L")
    print("  " + "─"*45)
    daily = {}
    for r in rows:
        try:
            day = (r['ts'] or '')[:10]
            if not day: continue
        except Exception:
            continue
        if day not in daily:
            daily[day] = {'w': 0, 'l': 0, 'pnl': 0}
        daily[day]['w']   += 1 if r['status'] == 'won' else 0
        daily[day]['l']   += 1 if r['status'] == 'lost' else 0
        daily[day]['pnl'] += r['pnl'] or 0
    for day in sorted(daily):
        b  = daily[day]
        t  = b['w'] + b['l']
        wr = (b['w'] / t * 100) if t else 0
        bar = "★" * b['w']
        print(f"  {day}  {b['w']:>2}W/{b['l']:>3}L  WR {wr:5.1f}%  P&L ${b['pnl']:+8.2f}  {bar}")

# ── CURRENT LOSING STREAK ─────────────────────────────────────────────────────
print(f"\n  CURRENT STREAK")
print("  " + "─"*45)
current_streak = 0
streak_type    = None
for r in reversed(rows):
    if streak_type is None:
        streak_type = r['status']
    if r['status'] == streak_type:
        current_streak += 1
    else:
        break
if streak_type == 'lost':
    last_win = next((r for r in reversed(rows) if r['status'] == 'won'), None)
    last_win_ts = (last_win['ts'] or '')[:16] if last_win else 'never'
    print(f"  ❌ Current losing streak : {current_streak} trades")
    print(f"  Last win                : {last_win_ts}")
elif streak_type == 'won':
    print(f"  ✅ Current winning streak: {current_streak} trades")
else:
    print(f"  No streak data.")

# ── BY TREND STRENGTH ────────────────────────────────────────────────────────
if 'trend_dev_1h' in cols:
    trend_rows = conn.execute("""
        SELECT status, pnl, trend_dev_1h FROM trades
        WHERE status IN ('won','lost') AND trend_dev_1h IS NOT NULL
    """).fetchall()
    if trend_rows:
        print(f"\n  BY TREND STRENGTH (1h deviation at entry)")
        print("  " + "─"*55)
        buckets = {
            "weak   0.1-0.3%": (0.001, 0.003),
            "medium 0.3-1.0%": (0.003, 0.010),
            "strong  >1.0%  ": (0.010, 99.0),
        }
        for label, (lo, hi) in buckets.items():
            subset = [r for r in trend_rows if lo <= abs(r[2]) < hi]
            if not subset:
                continue
            w   = sum(1 for r in subset if r[0] == 'won')
            l   = len(subset) - w
            wr  = (w / len(subset) * 100) if subset else 0
            pnl = sum(r[1] or 0 for r in subset)
            print(f"  {label}: {w:>3}W / {l:>3}L | WR {wr:5.1f}% | P&L ${pnl:+.2f}")

# ── BY SECS_LEFT ─────────────────────────────────────────────────────────────
if 'secs_left' in cols:
    secs_rows = conn.execute("""
        SELECT status, pnl, secs_left FROM trades
        WHERE status IN ('won','lost') AND secs_left IS NOT NULL
    """).fetchall()
    if secs_rows:
        print(f"\n  BY SECS LEFT AT ENTRY")
        print("  " + "─"*55)
        sec_buckets = {
            " 0-10s ": (0,   10),
            "10-20s ": (10,  20),
            "20-30s ": (20,  30),
            "30-45s ": (30,  45),
            "45-60s ": (45,  60),
            "60-90s ": (60,  90),
            " >90s  ": (90,  99999),
        }
        for label, (lo, hi) in sec_buckets.items():
            subset = [r for r in secs_rows if lo <= r[2] < hi]
            if not subset:
                continue
            w   = sum(1 for r in subset if r[0] == 'won')
            l   = len(subset) - w
            wr  = (w / len(subset) * 100) if subset else 0
            pnl = sum(r[1] or 0 for r in subset)
            print(f"  {label}: {w:>3}W / {l:>3}L | WR {wr:5.1f}% | P&L ${pnl:+.2f}")

conn.close()
input("\nPress Enter to close...")
