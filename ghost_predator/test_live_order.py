#!/usr/bin/env python3
"""
One-shot live test — place ONE $2 FOK on current BTC 5m market.
Reads credentials from .env. Does NOT touch the bot or its DB.
Run: python3 ghost_predator/test_live_order.py
"""
import os, json, time, sys
import urllib.request

SD = os.path.dirname(os.path.abspath(__file__))

def load_env():
    for line in open(os.path.join(SD, ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if "#" in v: v = v[:v.index("#")]
            os.environ.setdefault(k.strip(), v.strip())
load_env()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()
PROXY_ADDR  = os.getenv("POLYMARKET_PROXY_ADDRESS", "").strip()
SIZE        = 2.0
MAX_ASK     = 0.85
MIN_ASK     = 0.65
MIN_BPS     = 1.0
GAMMA       = "https://gamma-api.polymarket.com"
CLOB        = "https://clob.polymarket.com"
BINANCE     = "https://api.binance.com"

if not PRIVATE_KEY or not PROXY_ADDR:
    sys.exit("ERROR: PRIVATE_KEY ali POLYMARKET_PROXY_ADDRESS manjkata v .env")

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "gp-test/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

# Poišči BTC 5m market z dovolj časa
t = int(time.time())
dur = 300
market = None
for offset in (0, 1):
    start = (t // dur) * dur + offset * dur
    close_ts = start + dur
    secs_left = close_ts - t
    if secs_left < 30:
        continue
    slug = f"btc-updown-5m-{start}"
    print(f"Iščem market: {slug} ({secs_left}s do zapora)")
    mk = get(f"{GAMMA}/markets?slug={slug}")
    if mk:
        market = mk[0]
        market["_close_ts"] = close_ts
        market["_secs_left"] = secs_left
        break

if not market:
    sys.exit("Ni aktivnega BTC 5m marketa z dovolj časa")

toks = market.get("clobTokenIds", [])
outs = market.get("outcomes", [])
if isinstance(toks, str): toks = json.loads(toks)
if isinstance(outs, str): outs = json.loads(outs)

# Binance: open + current price
kline = get(f"{BINANCE}/api/v3/klines?symbol=BTCUSDT&interval=5m&startTime={start*1000}&limit=1")
open_px = float(kline[0][1]) if kline else None
cur_px  = float(get(f"{BINANCE}/api/v3/ticker/price?symbol=BTCUSDT")["price"])

if not open_px:
    sys.exit("Ne morem dobiti open cene iz Binance")

move_bps = (cur_px - open_px) / open_px * 1e4
leader   = "Up" if cur_px >= open_px else "Down"
print(f"BTC open={open_px:.2f} zdaj={cur_px:.2f} premik={move_bps:+.1f}bps → vodi={leader}")

if abs(move_bps) < MIN_BPS:
    sys.exit(f"Premik {move_bps:.1f}bps < {MIN_BPS}bps — prekratok (prevec enakovredna)")

# Najdi leader token
leader_tok = next((tok for tok, out in zip(toks, outs) if out.lower() == leader.lower()), None)
if not leader_tok:
    sys.exit(f"Ne najdem {leader} tokena")

# Preveri book
book     = get(f"{CLOB}/book?token_id={leader_tok}")
asks     = [(float(a["price"]), float(a.get("size", 0))) for a in (book.get("asks") or []) if float(a.get("size", 0)) > 0]
if not asks:
    sys.exit("Knjiga je prazna — ni asks")
best_ask = min(p for p, _ in asks)
print(f"Najboljši ask: {best_ask:.3f} (band {MIN_ASK}-{MAX_ASK})")

if not (MIN_ASK <= best_ask <= MAX_ASK):
    sys.exit(f"Ask {best_ask:.3f} je izven banda [{MIN_ASK}-{MAX_ASK}] — ne firamo")

profit_if_win = round(SIZE * (1/best_ask - 1), 2)
print(f"\n>>> LIVE ORDER: BTC {leader} @ ask≤{MAX_ASK} | ${SIZE:.2f} stake")
print(f"    Zmaga:  +${profit_if_win:.2f}")
print(f"    Izguba: -${SIZE:.2f}")
print(f"    Zapre se čez: {market['_secs_left']}s")
print()

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.constants import POLYGON
from py_clob_client_v2.clob_types import MarketOrderArgs, OrderType
from py_clob_client_v2.order_builder.constants import BUY

cl = ClobClient(CLOB, key=PRIVATE_KEY, chain_id=POLYGON,
                signature_type=1, funder=PROXY_ADDR)
cl.set_api_creds(cl.create_or_derive_api_key())

args  = MarketOrderArgs(token_id=leader_tok, amount=SIZE, side=BUY,
                        price=MAX_ASK, order_type=OrderType.FOK)
order = cl.create_market_order(args)
print("Pošiljam order...")
resp  = cl.post_order(order, OrderType.FOK)
print(f"Raw response: {resp}")

taking = float(resp.get("takingAmount") or 0)
making = float(resp.get("makingAmount") or 0)
if making < 0.01:
    print("\n>>> ORDER NI ŠEL SKOZI (FOK promašil ali zavrnjen)")
else:
    shares     = round(taking, 4)
    fill_price = round(making / taking, 4) if taking > 0 else 0
    print(f"\n>>> FILLED ✓  {shares:.4f} delnic @ {fill_price:.4f} | plačano ${making:.2f}")
    print(f"    Zmaga če Up: +${round(shares - making, 2):.2f} | izguba: -${making:.2f}")
