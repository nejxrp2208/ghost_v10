"""Quick stats — run with:  python stats.py"""
import sqlite3
import os
from datetime import datetime, timezone, timedelta

PAPER = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true","1","yes")
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "crypto_ghost_PAPER.db" if PAPER else "crypto_ghost.db")

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

print(f"DB: {os.path.basename(DB)}\n")

# Status counts
print("Status counts:")
for s, n in c.execute("SELECT status, COUNT(*) FROM trades GROUP BY status"):
    print(f"  {s:8} {n}")

# Overall P&L
n, w, l, pnl, risked = c.execute("""
    SELECT COUNT(*),
           SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
           SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END),
           SUM(pnl),
           SUM(size_usdc)
    FROM trades WHERE closed_at IS NOT NULL
""").fetchone()
wr = (w/n*100) if n else 0
roi = (pnl/risked*100) if risked else 0
print(f"\nClosed: {n}  Wins: {w}  Losses: {l}  WR: {wr:.1f}%")
print(f"Risked: ${risked:,.2f}   PnL: ${pnl:+,.2f}   ROI: {roi:+.2f}%")

# Last 24h
cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
row = c.execute("""
    SELECT COUNT(*),
           SUM(CASE WHEN status='won' THEN 1 ELSE 0 END),
           SUM(pnl)
    FROM trades WHERE closed_at >= ?
""", (cutoff,)).fetchone()
n24, w24, pnl24 = row
wr24 = (w24/n24*100) if n24 else 0
print(f"\nLast 24h:  {n24 or 0} closed  {w24 or 0} wins ({wr24:.1f}%)  ${pnl24 or 0:+,.2f}")

# Last 5 closed
print("\nLast 5 closed:")
for tid, coin, side, status, pnl, closed in c.execute("""
    SELECT id, coin, outcome, status, pnl, closed_at
    FROM trades WHERE closed_at IS NOT NULL
    ORDER BY id DESC LIMIT 5
"""):
    print(f"  #{tid} {coin} {side} {status} ${pnl or 0:+.2f}  {(closed or '')[:19]}")

# Open right now
opens = list(c.execute("""
    SELECT id, tier, coin, outcome, size_usdc, entry_price
    FROM trades WHERE status='open' ORDER BY id DESC
"""))
print(f"\nOpen now: {len(opens)}")
for tid, tier, coin, side, size, entry in opens:
    print(f"  #{tid} T{tier} {coin} {side} ${size:.2f}@{entry:.4f}")

c.close()
