"""
Fresh Analysis — v5 Resolver Verified Trades (auto-detects schema)
"""
import sqlite3, os, sys

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

has_verified = 'price_start' in cols and 'price_end' in cols

base_cols = ['id', 'coin', 'outcome', 'status', 'pnl']
if 'entry_price' in cols: base_cols.append('entry_price')
if 'size_usdc' in cols:   base_cols.append('size_usdc')
if 'question' in cols:    base_cols.append('question')
if has_verified:
    base_cols.extend(['price_start', 'price_end'])
    if 'resolution_verified' in cols:
        base_cols.append('resolution_verified')

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

total = len(rows)
wins = sum(1 for r in rows if r['status'] == 'won')
losses = sum(1 for r in rows if r['status'] == 'lost')
win_rate = (wins / total * 100) if total else 0
total_pnl = sum(r['pnl'] or 0 for r in rows)
avg_win = sum(r['pnl'] or 0 for r in rows if r['status'] == 'won') / wins if wins else 0
avg_loss = sum(r['pnl'] or 0 for r in rows if r['status'] == 'lost') / losses if losses else 0

longest_win = longest_loss = current_win = current_loss = 0
for r in rows:
    if r['status'] == 'won':
        current_win += 1
        current_loss = 0
        longest_win = max(longest_win, current_win)
    else:
        current_loss += 1
        current_win = 0
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
up_wins   = sum(1 for r in up_trades   if r['status'] == 'won')
down_wins = sum(1 for r in down_trades if r['status'] == 'won')

avg_payout = avg_win / abs(avg_loss) if avg_loss else 0
breakeven_wr = 100 / (1 + avg_payout) if avg_payout else 0

running = 0
curve = []
for r in rows:
    running += r['pnl'] or 0
    curve.append(running)
max_drawdown = 0
peak = 0
for val in curve:
    peak = max(peak, val)
    dd = peak - val
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

up_wr   = (up_wins/len(up_trades)*100)   if up_trades   else 0
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
    print(f"  ✓ {v}/{total} trades verified against Binance prices")

conn.close()
input("\nPress Enter to close...")
