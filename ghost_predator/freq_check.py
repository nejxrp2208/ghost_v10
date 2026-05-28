#!/usr/bin/env python3
"""
freq_check.py — how OFTEN does the current config actually fire, and does the
edge hold at that strictness? Replays 3 days of BTC+ETH with the live filters.
"""
import sqlite3, bisect

DB="/Volumes/Untitled/speed_edge/lag_data.db"
SYM={"btc":"BTCUSDT","eth":"ETHUSDT"}
DUR=300
# live config:
MIN_MOVE_BPS=9.0
MAX_ASK=0.65
MIN_ASK=0.02
DECISION_T=7        # representative point inside the 12s window (T-7s)

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
    if i<0 or ms-ts[i]>15000: return None
    return px[i]
def load_markets(c):
    mk={}
    for recv,slug,token,side in c.execute("SELECT recv_ms,slug,token_id,side FROM active_markets"):
        coin=slug.split("-")[0]
        if coin not in SYM: continue
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
    span_h=(t1-t0)/3.6e6
    evaluated=fires=wins=losses=0; cost=payout=0.0
    for slug,m in markets.items():
        coin,start,close=m["coin"],m["start"],m["close"]
        up,dn=m.get("Up"),m.get("Down")
        if not (up and dn): continue
        p_open=price_at(series,coin,start*1000); p_close=price_at(series,coin,close*1000)
        dt=(close-DECISION_T)*1000; p_dec=price_at(series,coin,dt)
        if None in (p_open,p_close,p_dec): continue
        evaluated+=1
        move_bps=(p_dec-p_open)/p_open*1e4
        if abs(move_bps)<MIN_MOVE_BPS: continue            # not decisive
        leading="Up" if p_dec>=p_open else "Down"
        resolution="Up" if p_close>=p_open else "Down"
        ltok=up if leading=="Up" else dn
        ask=stale_ask(c,ltok,dt)
        if ask is None or not (MIN_ASK<=ask<=MAX_ASK): continue   # not underpriced enough
        fires+=1; cost+=1.0
        if leading==resolution: wins+=1; payout+=1.0/ask
        else: losses+=1
    c.close()
    print("="*60)
    print(f"WINDOW: {span_h:.1f}h ({span_h/24:.1f} days) | BTC+ETH | markets eval'd: {evaluated}")
    print(f"FILTERS: move>={MIN_MOVE_BPS}bps  ask<={MAX_ASK}  (decision @ T-{DECISION_T}s)")
    print("="*60)
    print(f"  qualifying FIRES: {fires}")
    print(f"  => {fires/span_h:.2f} per hour   |   {fires/span_h*24:.1f} per day")
    print(f"  => 1 trade every {span_h/fires*60:.0f} min" if fires else "  (no fires)")
    pct_mkts=100*fires/evaluated if evaluated else 0
    print(f"  => fires on {pct_mkts:.1f}% of markets")
    if fires:
        roi=100*(payout-cost)/cost
        print(f"\n  win-rate at this strictness: {100*wins/fires:.1f}%  ({wins}W/{losses}L)")
        print(f"  ROI {roi:+.1f}%   ($ {cost:.0f} -> $ {payout:.0f})")
        # expected $ at $30 stake
        exp_per_trade=(payout-cost)/fires*30
        print(f"  at $30/snipe: ~${exp_per_trade:+.2f} expected per trade, "
              f"~${exp_per_trade*fires/span_h*24:+.0f}/day")

if __name__=="__main__": main()
