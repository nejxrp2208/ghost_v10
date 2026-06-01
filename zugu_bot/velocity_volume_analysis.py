#!/usr/bin/env python3
"""
velocity_volume_analysis.py
Retroactive analysis: fetches Binance historical price velocity and volume
for each trade entry, then correlates with outcome/PnL.
"""

import sqlite3
import asyncio
import aiohttp
import csv
import sys
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH     = "/root/zugubotv3/crypto_ghost_PAPER.db"
OUTPUT_CSV  = "/root/zugubotv3/velocity_volume_analysis.csv"
BINANCE_API = "https://api.binance.com/api/v3"
COIN_MAP    = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


# ── Binance helpers ───────────────────────────────────────────────────────────

async def fetch_agg_trades(session, symbol, start_ms, end_ms):
    url = f"{BINANCE_API}/aggTrades"
    params = {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": 1000}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 429:
                await asyncio.sleep(5)
                return []
            if r.status != 200:
                return []
            return await r.json()
    except Exception:
        return []

async def fetch_kline_volume(session, symbol, interval, end_ms):
    url = f"{BINANCE_API}/klines"
    # last completed candle before entry
    params = {"symbol": symbol, "interval": interval, "endTime": end_ms - 1, "limit": 1}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data:
                return None
            return float(data[0][5])  # volume field
    except Exception:
        return None


# ── Per-trade analysis ────────────────────────────────────────────────────────

async def analyze_trade(session, trade, sem):
    async with sem:
        symbol = COIN_MAP.get(trade["coin"])
        if not symbol:
            return None

        ts_str = trade["ts"]
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None
        entry_ms = int(dt.timestamp() * 1000)

        # fetch 35s of agg trades before entry (buffer for sparse markets)
        raw_trades = await fetch_agg_trades(session, symbol, entry_ms - 35000, entry_ms)
        if not raw_trades:
            return None

        entry_price = trade["entry_price"]  # PM odds (kept for CSV reference)

        def price_at(offset_ms):
            target = entry_ms - offset_ms
            closest = min(raw_trades, key=lambda t: abs(t["T"] - target))
            if abs(closest["T"] - target) > 5000:  # >5s away = no data
                return None
            return float(closest["p"])

        # Binance prices (USD) at each point
        p_now = price_at(0)       # Binance price at entry moment
        p10   = price_at(10_000)
        p20   = price_at(20_000)
        p30   = price_at(30_000)

        if p_now is None or p10 is None or p30 is None:
            return None

        # velocity = % move per 10s window (all Binance USD prices)
        vel_10s   = (p_now - p10) / p10 * 100
        vel_30s   = (p_now - p30) / p30 * 100
        vel_20_10 = (p10  - p20)  / p20 * 100 if p20 else None
        abs_vel_10s = abs(vel_10s)

        # fetch volumes
        vol_5m  = await fetch_kline_volume(session, symbol, "5m",  entry_ms)
        vol_15m = await fetch_kline_volume(session, symbol, "15m", entry_ms)
        vol_1h  = await fetch_kline_volume(session, symbol, "1h",  entry_ms)

        await asyncio.sleep(0.05)  # gentle rate limit

        return {
            "id":           trade["id"],
            "ts":           trade["ts"],
            "coin":         trade["coin"],
            "outcome":      trade["outcome"],
            "result":       trade["pm_result"],
            "pnl":          trade["pnl"],
            "entry_price":  entry_price,
            "vel_10s_pct":  round(vel_10s,   6) if vel_10s  is not None else "",
            "vel_30s_pct":  round(vel_30s,   6) if vel_30s  is not None else "",
            "vel_20_10_pct":round(vel_20_10, 6) if vel_20_10 is not None else "",
            "abs_vel_10s":  round(abs_vel_10s, 6) if abs_vel_10s is not None else "",
            "vol_5m":       round(vol_5m,  4) if vol_5m  is not None else "",
            "vol_15m":      round(vol_15m, 4) if vol_15m is not None else "",
            "vol_1h":       round(vol_1h,  4) if vol_1h  is not None else "",
        }


# ── Analysis output ───────────────────────────────────────────────────────────

def print_bucket(label, rows, key, buckets_def):
    print(f"\n[{label}]")
    print(f"  {'Bucket':<16} {'N':>5} {'WinRate':>8} {'AvgPnL':>9}")
    print(f"  {'─'*16} {'─'*5} {'─'*8} {'─'*9}")
    for bname, lo, hi in buckets_def:
        grp = [r for r in rows if r.get(key) != "" and lo <= abs(r[key]) < hi]
        if not grp:
            continue
        wins = sum(1 for r in grp if r["result"] == "won")
        wr   = wins / len(grp) * 100
        avg  = sum(r["pnl"] for r in grp) / len(grp)
        print(f"  {bname:<16} {len(grp):>5} {wr:>7.1f}% {avg:>+9.3f}")

def percentile_buckets(values, n=3):
    sv = sorted(values)
    cuts = [sv[int(len(sv)*i/n)] for i in range(1, n)]
    return cuts

def print_vol_bucket(label, rows, key):
    vals = [r[key] for r in rows if r.get(key) != ""]
    if not vals:
        return
    p33, p66 = percentile_buckets(vals)
    print(f"\n[{label}]  (low <{p33:,.0f}, mid <{p66:,.0f})")
    print(f"  {'Bucket':<8} {'N':>5} {'WinRate':>8} {'AvgPnL':>9}")
    print(f"  {'─'*8} {'─'*5} {'─'*8} {'─'*9}")
    for bname, cond in [("low", lambda v: v < p33), ("medium", lambda v: p33 <= v < p66), ("high", lambda v: v >= p66)]:
        grp = [r for r in rows if r.get(key) != "" and cond(r[key])]
        if not grp:
            continue
        wins = sum(1 for r in grp if r["result"] == "won")
        wr   = wins / len(grp) * 100
        avg  = sum(r["pnl"] for r in grp) / len(grp)
        print(f"  {bname:<8} {len(grp):>5} {wr:>7.1f}% {avg:>+9.3f}")

def print_analysis(results):
    rows = [r for r in results if r is not None]
    total = len(rows)
    wins  = sum(1 for r in rows if r["result"] == "won")
    print(f"\n{'='*58}")
    print(f"  VELOCITY & VOLUME ANALYSIS  —  {total} trades")
    print(f"{'='*58}")
    print(f"  Overall win rate: {wins/total*100:.1f}%   AvgPnL: {sum(r['pnl'] for r in rows)/total:+.3f}")

    # velocity buckets (absolute value, % per 10s)
    vel_buckets = [
        ("0–0.005%",   0,      0.005),
        ("0.005–0.01%",0.005,  0.010),
        ("0.01–0.02%", 0.010,  0.020),
        ("0.02–0.05%", 0.020,  0.050),
        ("0.05–0.10%", 0.050,  0.100),
        (">0.10%",     0.100,  9999),
    ]
    print_bucket("Velocity |10s| buckets — Win Rate", rows, "vel_10s_pct", vel_buckets)
    print_bucket("Velocity |30s| buckets — Win Rate", rows, "vel_30s_pct", vel_buckets)

    # direction split: moving toward PTB side vs against
    # (outcome=UP means bet UP → winning velocity = positive vel = price rising)
    rows_dir = [r for r in rows if r.get("vel_10s_pct") != ""]
    aligned  = [r for r in rows_dir if (r["outcome"] == "UP"   and r["vel_10s_pct"] > 0)
                                     or (r["outcome"] == "DOWN" and r["vel_10s_pct"] < 0)]
    against  = [r for r in rows_dir if r not in aligned]

    print(f"\n[Velocity direction vs bet direction]")
    print(f"  {'Direction':<12} {'N':>5} {'WinRate':>8} {'AvgPnL':>9}")
    print(f"  {'─'*12} {'─'*5} {'─'*8} {'─'*9}")
    for label, grp in [("aligned", aligned), ("against", against)]:
        if not grp: continue
        w = sum(1 for r in grp if r["result"] == "won")
        print(f"  {label:<12} {len(grp):>5} {w/len(grp)*100:>7.1f}% {sum(r['pnl'] for r in grp)/len(grp):>+9.3f}")

    # volume buckets
    print_vol_bucket("Volume 5m  — Win Rate", rows, "vol_5m")
    print_vol_bucket("Volume 15m — Win Rate", rows, "vol_15m")
    print_vol_bucket("Volume 1h  — Win Rate", rows, "vol_1h")

    # BTC vs ETH split
    print(f"\n[Coin split]")
    print(f"  {'Coin':<6} {'N':>5} {'WinRate':>8} {'AvgPnL':>9}")
    for coin in ["BTC", "ETH"]:
        grp = [r for r in rows if r["coin"] == coin]
        if not grp: continue
        w = sum(1 for r in grp if r["result"] == "won")
        print(f"  {coin:<6} {len(grp):>5} {w/len(grp)*100:>7.1f}% {sum(r['pnl'] for r in grp)/len(grp):>+9.3f}")

    print(f"\n  CSV: {OUTPUT_CSV}")
    print(f"{'='*58}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT id, ts, coin, entry_price, pnl, pm_result, outcome
        FROM trades
        WHERE pm_result IN ('won', 'lost')
        ORDER BY ts
    """)
    trades = [dict(r) for r in cur.fetchall()]
    con.close()

    print(f"Loaded {len(trades)} resolved trades from DB")
    print("Fetching Binance historical data... (may take 3–5 min)\n")

    sem = asyncio.Semaphore(8)  # max 8 concurrent requests
    async with aiohttp.ClientSession() as session:
        tasks   = [analyze_trade(session, t, sem) for t in trades]
        results = []
        batch   = 60
        for i in range(0, len(tasks), batch):
            chunk = await asyncio.gather(*tasks[i:i+batch])
            results.extend(chunk)
            ok = sum(1 for r in chunk if r is not None)
            print(f"  {min(i+batch, len(tasks)):>4}/{len(tasks)}  (ok={ok})", flush=True)
            if i + batch < len(tasks):
                await asyncio.sleep(1.5)

    results = [r for r in results if r is not None]

    # save CSV
    if results:
        with open(OUTPUT_CSV, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=results[0].keys())
            w.writeheader()
            w.writerows(results)

    print_analysis(results)

if __name__ == "__main__":
    asyncio.run(main())
