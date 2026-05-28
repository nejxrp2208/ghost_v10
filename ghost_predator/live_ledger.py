#!/usr/bin/env python3
"""Live-fillable ledger (the real-money number) with Polymarket's modeled taker
fee AND a per-regime split, so you can watch coinflip vs decisive performance as
volatility changes (dead late-night tape vs active peak hours).

Fee model (crypto, from docs): fee = shares * 0.07 * p * (1-p)
  -> for our stake = 0.07 * size_usdc * (1 - entry_price), charged on entry (win OR lose).
"Live-fillable" = stake covered by resting depth at the ask (size_usdc <= ask_usdc) —
robust to the would_fill rounding artifact and to dynamic-sized orders."""
import sqlite3, sys
FEE_RATE = 0.07
DB = sys.argv[1] if len(sys.argv) > 1 else "ghost_predator.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = c.execute("SELECT id,coin,outcome,entry_price,size_usdc,status,COALESCE(pnl,0),ABS(move_bps) "
                 "FROM positions WHERE status IN ('won','lost') "
                 "AND COALESCE(ask_usdc,0) > 0 AND size_usdc <= ask_usdc + 0.02 ORDER BY id").fetchall()
c.close()

def fee_of(size, p): return FEE_RATE * size * (1 - p)

print(f"{'id':>4} {'coin':<4} {'side':<5} {'entry':>5} {'mv':>4} {'res':<4} {'gross':>8} {'fee':>5} {'net':>8}")
print("-"*54)
g=f=n=0.0; w=l=0
for pid,coin,side,p,size,st,pnl,mv in rows:
    fee=fee_of(size,p); net=pnl-fee; g+=pnl; f+=fee; n+=net
    if st=="won": w+=1
    else: l+=1
    print(f"{pid:>4} {coin:<4} {side:<5} {p:>5.2f} {mv:>4.1f} {st:<4} {pnl:>+8.2f} {fee:>5.2f} {net:>+8.2f}")
print("-"*54)
print(f"  {w}W / {l}L   gross ${g:+.2f}   fees -${f:.2f}   NET AFTER FEES ${n:+.2f}"
      + (f"   (${n/len(rows):+.2f}/trade, n={len(rows)})" if rows else "  — no fillable trades yet"))

print("\n=== BY REGIME (|move| at fire — watch the decisive buckets fill in as vol rises) ===")
print(f"  {'regime':<18} {'n':>3} {'win%':>5} {'net':>9} {'$/trade':>8}")
for name,lo,hi in (("coinflip <2bps",0,2),("decisive 2-3bps",2,3),("strong 3+bps",3,1e9)):
    sub=[r for r in rows if lo<=r[7]<hi]
    if not sub:
        print(f"  {name:<18} {0:>3}   (none yet)"); continue
    ww=sum(1 for r in sub if r[5]=="won")
    nn=sum(r[6]-fee_of(r[4],r[3]) for r in sub)
    print(f"  {name:<18} {len(sub):>3} {100*ww/len(sub):>4.0f}% {nn:>+9.2f} {nn/len(sub):>+8.2f}")
