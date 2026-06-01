#!/usr/bin/env python3
"""
monitor.py – Live price monitor for zugubotv2
Shows: PM current price, Binance price, PTB, and direction per market
"""
import asyncio, json, time, urllib.parse, os, sys
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple

import aiohttp
import websockets

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
except Exception:
    pass

RTDS_URL       = "wss://ws-live-data.polymarket.com"
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade"
GAMMA_URL      = "https://gamma-api.polymarket.com"
CRYPTO_PRICE   = "https://polymarket.com/api/crypto/crypto-price"

ASSETS = ["BTC", "ETH"]
TF_SECS = 300  # 5min

pm_prices:  Dict[str, float] = {}
bin_prices: Dict[str, float] = {}
ptb_data:   Dict[str, Dict]  = {}   # asset -> {ptb, candle_end}
pm_status  = "connecting"
bin_status = "connecting"


# ── helpers ──────────────────────────────────────────────────────────────────

def _iso_z(ts: int) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")

def _slug(asset: str, ts: int) -> str:
    return f"{asset.lower()}-updown-5m-{ts}"

def _current_candle_end() -> int:
    now = int(time.time())
    return ((now // TF_SECS) + 1) * TF_SECS

def _secs_left() -> int:
    return _current_candle_end() - int(time.time())


# ── Polymarket RTDS price feed ────────────────────────────────────────────────

CHAINLINK_MAP = {"btc/usd": "BTC", "eth/usd": "ETH"}

async def pm_price_feed():
    global pm_status
    while True:
        try:
            pm_status = "connecting"
            async with websockets.connect(RTDS_URL, ping_interval=None, open_timeout=15) as ws:
                sub = {"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "update"}]}
                await ws.send(json.dumps(sub))
                pm_status = "waiting"
                async def ping_loop():
                    while True:
                        await asyncio.sleep(5)
                        try:
                            await ws.send(json.dumps({"type": "PING"}))
                        except Exception:
                            break
                ping_task = asyncio.create_task(ping_loop())
                try:
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        if not raw:
                            continue
                        data = json.loads(raw)
                        if data.get("topic") == "crypto_prices_chainlink":
                            payload = data.get("payload", {})
                            symbol = str(payload.get("symbol", "")).lower()
                            asset = CHAINLINK_MAP.get(symbol)
                            if asset:
                                try:
                                    pm_prices[asset] = float(payload["value"])
                                    pm_status = "ok"
                                except Exception:
                                    pass
                finally:
                    ping_task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            pm_status = f"reconnecting"
            await asyncio.sleep(3)


# ── Binance price feed ────────────────────────────────────────────────────────

BIN_MAP = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}

async def binance_price_feed():
    global bin_status
    while True:
        try:
            bin_status = "connecting"
            async with websockets.connect(BINANCE_WS_URL, ping_interval=None, open_timeout=15) as ws:
                bin_status = "waiting"
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(raw)
                    stream = data.get("stream", "")
                    trade  = data.get("data", {})
                    if "@trade" in stream and "p" in trade:
                        sym = stream.split("@")[0].upper()
                        asset = BIN_MAP.get(sym)
                        if asset:
                            bin_prices[asset] = float(trade["p"])
                            bin_status = "ok"
        except asyncio.CancelledError:
            raise
        except Exception:
            bin_status = "reconnecting"
            await asyncio.sleep(3)


# ── PTB fetcher ───────────────────────────────────────────────────────────────

async def fetch_ptb_for_asset(session: aiohttp.ClientSession, asset: str, candle_end: int) -> Optional[float]:
    start_ts = candle_end - TF_SECS
    params = {
        "symbol": asset,
        "eventStartTime": _iso_z(start_ts),
        "variant": "fiveminute",
        "endDate": _iso_z(candle_end),
    }
    url = f"{CRYPTO_PRICE}?{urllib.parse.urlencode(params)}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            val = data.get("openPrice")
            return float(val) if val is not None else None
    except Exception:
        return None

async def ptb_loop():
    last_candle_end = 0
    while True:
        candle_end = _current_candle_end()
        new_candle = candle_end != last_candle_end
        async with aiohttp.ClientSession() as session:
            for asset in ASSETS:
                if new_candle or ptb_data.get(asset, {}).get("ptb") is None:
                    ptb = await fetch_ptb_for_asset(session, asset, candle_end)
                    ptb_data[asset] = {"ptb": ptb, "candle_end": candle_end}
        if new_candle:
            last_candle_end = candle_end
        await asyncio.sleep(5)


# ── Display ───────────────────────────────────────────────────────────────────

G  = "\033[92m"   # green
R  = "\033[91m"   # red
DIM= "\033[2m"
RST= "\033[0m"
BLD= "\033[1m"

def _side_str(price: Optional[float], ptb: Optional[float]) -> str:
    if price is None or ptb is None:
        return f"{DIM}  ---{RST}"
    if price > ptb:
        return f"{G}  UP {RST}"
    if price < ptb:
        return f"{R}DOWN {RST}"
    return f"{DIM}  == {RST}"

def _p(val: Optional[float]) -> str:
    return f"{val:>12,.2f}" if val is not None else f"{'---':>12}"

SEP  = "  " + "─" * 58
SEP2 = "  " + "═" * 58

async def display_loop():
    while True:
        now   = datetime.now(timezone.utc).strftime("%H:%M:%S")
        secs  = _secs_left()
        mins  = secs // 60
        secs_r = secs % 60
        candle_str = f"{mins}m {secs_r:02d}s left"

        out = ["\033[2J\033[H"]  # clear screen
        out.append(SEP2)
        out.append(f"  {BLD}PRICE MONITOR{RST}   {now}   candle: {BLD}{candle_str}{RST}")
        out.append(SEP2)
        out.append(f"  {'Asset':<6}  {'Source':<4}  {'Side':<6}  {'Price':>12}  {'PTB':>12}")
        out.append(SEP)

        for asset in ASSETS:
            pm  = pm_prices.get(asset)
            bn  = bin_prices.get(asset)
            ptb = ptb_data.get(asset, {}).get("ptb")

            out.append(f"  {BLD}{asset:<6}{RST}  {'PM':<4}  {_side_str(pm, ptb)}  {_p(pm)}  {_p(ptb)}")
            out.append(f"  {'':6}  {'BIN':<4}  {_side_str(bn, ptb)}  {_p(bn)}")
            out.append(SEP)

        out.append(f"  PM feed: {G if pm_status=='ok' else R}{pm_status}{RST}    BIN feed: {G if bin_status=='ok' else R}{bin_status}{RST}")
        out.append(SEP2)

        print("\n".join(out), flush=True)
        await asyncio.sleep(1)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    await asyncio.gather(
        pm_price_feed(),
        binance_price_feed(),
        ptb_loop(),
        display_loop(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
