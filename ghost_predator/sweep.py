#!/usr/bin/env python3
"""
sweep.py — find the frequency/profit frontier. For each (min_move, max_ask)
combo, replay 3 days of BTC+ETH and report fires/day, win%, ROI, and expected
$/day at a $30 stake. Fetches each market's ask ONCE, then sweeps in memory.
"""
import sqlite3, bisect

DB="/Volumes/Untitled/speed_edge/lag_data.db"
SYM={"btc":"BTCUSDT","eth":"ETHUSDT"}; DUR=300; DECISION_T=7
MOVES=[0,2,4,6,9]; ASKS=[0.55,0.65,0.75,0.85,0.95]; STAKE=30

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
def load_markets(c):
    mk={}
    for recv,slug,token,side in c.execute("SELECT recv_ms,slug,token_id,side FROM active_markets"):
        coin=slug.split("-")[0]
        if coin not in SYM: continue
        if "-5m-" not in slug: continue          # 5m ONLY (close=start+300 is correct only for 5m)
        try: start=int(slug.split("-")[-1])
        except: continue
        m=mk.setdefault(slug,{"coin":coin,"start":start,"close":start+DUR})
        m[side.capitalize()]=token
    return mk
def stale_ask(c,token,dt):
    r=c.execute("SELECT best_ask FROM pm_events WHERE token_id=? AND recv_ms<=? AND recv_ms>=? "
                "AND best_ask IS NOT NULL ORDER BY recv_ms DESC LIMIT 1",(token,dt,dt-30000)).fetchall()
    return r[0][0] if r else None

def main():
    c=sqlite3.connect(f"file:{DB}?mode=ro&immutable=1",uri=True)
    series=load_binance(c); markets=load_markets(c)
    t0=min(series["btc"][0][0],series["eth"][0][0]); t1=max(series["btc"][0][-1],series["eth"][0][-1])
    span_h=(t1-t0)/3.6e6; days=span_h/24
    samples=[]   # (abs_move_bps, ask, won_bool)
    for slug,m in markets.items():
        coin,start,close=m["coin"],m["start"],m["close"]
        up,dn=m.get("Up"),m.get("Down")
        if not (up and dn): continue
        p_open=price_at(series,coin,start*1000); p_close=price_at(series,coin,close*1000)
        dt=(close-DECISION_T)*1000; p_dec=price_at(series,coin,dt)
        if None in (p_open,p_close,p_dec): continue
        move=(p_dec-p_open)/p_open*1e4
        leading="Up" if p_dec>=p_open else "Down"
        resolution="Up" if p_close>=p_open else "Down"
        ltok=up if leading=="Up" else dn
        ask=stale_ask(c,ltok,dt)
        if ask is None: continue
        samples.append((abs(move),ask,leading==resolution))
    c.close()
    print(f"data: {span_h:.1f}h ({days:.1f}d), {len(samples)} markets with a leading-side ask\n")
    print(f"{'min_move':>8} {'max_ask':>8} {'fires':>6} {'/day':>6} {'win%':>6} {'ROI%':>7} {'$/day@30':>9}")
    print("-"*60)
    for mm in MOVES:
        for ma in ASKS:
            fires=wins=0; cost=payout=0.0
            for amove,ask,won in samples:
                if amove<mm: continue
                if not (0.02<=ask<=ma): continue
                fires+=1; cost+=1.0
                if won: wins+=1; payout+=1.0/ask
            if not fires:
                print(f"{mm:>8} {ma:>8} {0:>6} {0:>6.1f} {'—':>6} {'—':>7} {'—':>9}"); continue
            roi=(payout-cost)/cost
            perday=fires/days
            dpd=(payout-cost)/fires*STAKE*perday
            print(f"{mm:>8} {ma:>8} {fires:>6} {perday:>6.1f} {100*wins/fires:>5.0f}% "
                  f"{roi*100:>+6.0f}% {dpd:>+8.0f}")
    print("\n(fires/day = how often it trades; ROI = per-trade edge; $/day = expected at $30 stake)")

if __name__=="__main__": main()
