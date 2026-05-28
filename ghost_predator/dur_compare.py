#!/usr/bin/env python3
"""
dur_compare.py — backtest 5m vs 15m CORRECTLY (real close = start+duration),
so we can decide on data, not the contaminated 99%. Snipe = buy the leader at
T-7s if its ask is in band; win if it's still the winner at the REAL close.
"""
import sqlite3, bisect
DB="/Volumes/Untitled/speed_edge/lag_data.db"
SYM={"btc":"BTCUSDT","eth":"ETHUSDT"}
DUR={"5m":300,"15m":900}
DECISION_T=7; STAKE=30
SETTINGS=[(0,0.65),(0,0.75),(0,0.85),(2,0.75),(2,0.85),(4,0.85)]

def load_binance(c):
    s={}
    for coin,sym in SYM.items():
        rows=c.execute("SELECT recv_ms,price FROM binance_ticks WHERE symbol=? ORDER BY recv_ms",(sym,)).fetchall()
        s[coin]=([r[0] for r in rows],[r[1] for r in rows])
    return s
def price_at(s,coin,ms):
    ts,px=s[coin]
    if not ts: return None
    i=bisect.bisect_right(ts,ms)-1
    return px[i] if (i>=0 and ms-ts[i]<=15000) else None
def load_markets(c,dlabel,dsecs):
    mk={}; pat=f"-{dlabel}-"
    for recv,slug,token,side in c.execute("SELECT recv_ms,slug,token_id,side FROM active_markets"):
        coin=slug.split("-")[0]
        if coin not in SYM or pat not in slug: continue
        try: start=int(slug.split("-")[-1])
        except: continue
        m=mk.setdefault(slug,{"coin":coin,"start":start,"close":start+dsecs})
        m[side.capitalize()]=token
    return mk
def stale_ask(c,token,dt):
    r=c.execute("SELECT best_ask FROM pm_events WHERE token_id=? AND recv_ms<=? AND recv_ms>=? "
                "AND best_ask IS NOT NULL ORDER BY recv_ms DESC LIMIT 1",(token,dt,dt-30000)).fetchall()
    return r[0][0] if r else None

def run(c,series,dlabel,dsecs,span_h):
    markets=load_markets(c,dlabel,dsecs)
    samples=[]
    for slug,m in markets.items():
        coin,start,close=m["coin"],m["start"],m["close"]
        up,dn=m.get("Up"),m.get("Down")
        if not (up and dn): continue
        po=price_at(series,coin,start*1000); pc=price_at(series,coin,close*1000)
        dt=(close-DECISION_T)*1000; pd=price_at(series,coin,dt)
        if None in (po,pc,pd): continue
        move=abs((pd-po)/po*1e4)
        leading="Up" if pd>=po else "Down"; res="Up" if pc>=po else "Down"
        ltok=up if leading=="Up" else dn
        ask=stale_ask(c,ltok,dt)
        if ask is None: continue
        samples.append((move,ask,leading==res))
    print(f"\n===== {dlabel} markets (real close = start+{dsecs}s) — {len(markets)} markets, {len(samples)} with ask =====")
    print(f"{'min_move':>8} {'max_ask':>8} {'fires':>6} {'/day':>6} {'win%':>6} {'ROI%':>7} {'$/day@30':>9}")
    for mm,ma in SETTINGS:
        fires=wins=0; cost=payout=0.0
        for amove,ask,won in samples:
            if amove<mm or not (0.02<=ask<=ma): continue
            fires+=1; cost+=1
            if won: wins+=1; payout+=1/ask
        if not fires:
            print(f"{mm:>8} {ma:>8} {0:>6} {0:>6.1f} {'—':>6} {'—':>7} {'—':>9}"); continue
        roi=(payout-cost)/cost; perday=fires/(span_h/24)
        print(f"{mm:>8} {ma:>8} {fires:>6} {perday:>6.1f} {100*wins/fires:>5.0f}% "
              f"{roi*100:>+6.0f}% {(payout-cost)/fires*STAKE*perday:>+8.0f}")

def main():
    c=sqlite3.connect(f"file:{DB}?mode=ro&immutable=1",uri=True)
    series=load_binance(c)
    t0=min(series["btc"][0][0],series["eth"][0][0]); t1=max(series["btc"][0][-1],series["eth"][0][-1])
    span_h=(t1-t0)/3.6e6
    for dl,ds in DUR.items():
        run(c,series,dl,ds,span_h)
    c.close()
    print("\nNOTE: resolution here is Binance close vs open (proxy for Chainlink).")
if __name__=="__main__": main()
