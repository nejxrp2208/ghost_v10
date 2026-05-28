#!/usr/bin/env python3
"""
snipe_backtest.py — does the independent last-window latency snipe actually pay?

Tests the thesis on 3 days of real captured data (BTC+ETH):
  In the final SNIPE_SECS of a 5-min up/down market, the underlying has usually
  already decided the outcome. If PM's ask for the LEADING side is still < 1,
  a sub-ms player can snipe it before the book reprices.

For each market we reconstruct from the data (no API needed):
  - open price   = Binance price at window start   (from binance_ticks)
  - close price  = Binance price at window close   -> ground-truth resolution
  - decision     = Binance price at (close - SNIPE_SECS) -> leading side then
  - PM ask       = the stale quote on the leading token at decision time
We then simulate buying the leading side at that ask and check if it won.

Run against the STATIC lag_data.db (recorder retired).
"""
import sqlite3, bisect, statistics

DB = "/Volumes/Untitled/speed_edge/lag_data.db"
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT"}
SNIPE_SECS_LIST = [2, 5, 10, 20, 30]   # try several decision points
MIN_ASK = 0.02
MAX_ASK = 0.97          # only snipe if there's profit room (ask below 1)
DUR = 300               # 5-min markets

def load_binance(c):
    series = {}
    for coin, sym in SYM.items():
        rows = c.execute("SELECT recv_ms, price FROM binance_ticks "
                         "WHERE symbol=? ORDER BY recv_ms", (sym,)).fetchall()
        ts = [r[0] for r in rows]; px = [r[1] for r in rows]
        series[coin] = (ts, px)
    return series

def price_at(series, coin, ms):
    ts, px = series[coin]
    if not ts: return None
    i = bisect.bisect_right(ts, ms) - 1
    if i < 0: return None
    if ms - ts[i] > 15000:    # no tick within 15s -> unreliable
        return None
    return px[i]

def load_markets(c):
    """slug -> {coin,start,close, 'Up':token, 'Down':token}"""
    mk = {}
    for recv, slug, token, side in c.execute(
            "SELECT recv_ms, slug, token_id, side FROM active_markets"):
        coin = slug.split("-")[0]
        if coin not in SYM:  # only btc/eth (have binance)
            continue
        try: start = int(slug.split("-")[-1])
        except: continue
        m = mk.setdefault(slug, {"coin": coin, "start": start, "close": start+DUR})
        m[side.capitalize()] = token        # "UP"->"Up", "DOWN"->"Down"
    return mk

def stale_ask(c, token, dt):
    """Most recent best_ask on `token` at-or-before decision time dt."""
    rows = c.execute(
        "SELECT best_ask FROM pm_events WHERE token_id=? AND recv_ms<=? "
        "AND recv_ms>=? AND best_ask IS NOT NULL ORDER BY recv_ms DESC LIMIT 1",
        (token, dt, dt-30000)).fetchall()
    return rows[0][0] if rows else None

def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
    print("loading binance series..."); series = load_binance(c)
    print("loading markets...");        markets = load_markets(c)
    print(f"markets with btc/eth: {len(markets)}\n")

    for SNIPE_SECS in SNIPE_SECS_LIST:
        wins = losses = 0
        cost = payout = 0.0
        lead_correct = 0; evaluated = 0
        for slug, m in markets.items():
            coin, start, close = m["coin"], m["start"], m["close"]
            up_tok, dn_tok = m.get("Up"), m.get("Down")
            if not (up_tok and dn_tok): continue
            p_open  = price_at(series, coin, start*1000)
            p_close = price_at(series, coin, close*1000)
            dt = (close - SNIPE_SECS) * 1000
            p_dec   = price_at(series, coin, dt)
            if None in (p_open, p_close, p_dec): continue
            evaluated += 1
            resolution = "Up" if p_close >= p_open else "Down"
            leading    = "Up" if p_dec  >= p_open else "Down"
            if leading == resolution: lead_correct += 1
            lead_tok = up_tok if leading == "Up" else dn_tok
            ask = stale_ask(c, lead_tok, dt)
            if ask is None or not (MIN_ASK <= ask <= MAX_ASK):
                continue
            shares = 1.0 / ask
            cost += 1.0
            if leading == resolution:
                wins += 1; payout += shares
            else:
                losses += 1
        c2 = wins + losses
        print(f"── SNIPE @ T-{SNIPE_SECS}s "+"─"*40)
        if evaluated:
            print(f"   leading-side persistence: {100*lead_correct/evaluated:.1f}% "
                  f"({lead_correct}/{evaluated} markets — does the T-{SNIPE_SECS}s leader win?)")
        if c2:
            roi = 100*(payout-cost)/cost
            print(f"   snipes taken: {c2}   WINS {wins}  LOSSES {losses}  "
                  f"hit-rate {100*wins/c2:.1f}%")
            print(f"   ROI {roi:+.1f}%   (cost ${cost:.0f} -> payout ${payout:.0f})")
        else:
            print("   no snipes qualified")
        print()
    c.close()

if __name__ == "__main__":
    main()
