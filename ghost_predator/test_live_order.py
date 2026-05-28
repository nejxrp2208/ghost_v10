#!/usr/bin/env python3
"""
En live trejd na BTC 5m market. Brez filtrov — gre noter takoj.
Izmeri čas od klica do fill potrditve.
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

if not PRIVATE_KEY or not PROXY_ADDR:
    sys.exit("PRIVATE_KEY ali POLYMARKET_PROXY_ADDRESS manjkata v .env")

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "gp-test/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

# Poišči BTC 5m market
t = int(time.time())
dur = 300
market = None
for offset in range(3):
    start = (t // dur) * dur + offset * dur
    slug = f"btc-updown-5m-{start}"
    mk = get(f"https://gamma-api.polymarket.com/markets?slug={slug}")
    if mk:
        market = mk[0]
        break

if not market:
    sys.exit("Ni aktivnega BTC 5m marketa")

toks = market.get("clobTokenIds", [])
outs = market.get("outcomes", [])
if isinstance(toks, str): toks = json.loads(toks)
if isinstance(outs, str): outs = json.loads(outs)

# Vzemi Up token
tok = toks[outs.index("Up")] if "Up" in outs else toks[0]
out = "Up" if "Up" in outs else outs[0]

# Dobim ask iz knjige
book = get(f"https://clob.polymarket.com/book?token_id={tok}")
asks = sorted([(float(a["price"]), float(a.get("size",0))) for a in (book.get("asks") or []) if float(a.get("size",0)) > 0])
ask  = asks[0][0] if asks else 0.5
print(f"Market: {slug} | stran: BTC {out} | ask: {ask:.4f} | stake: ${SIZE:.2f}")
print(f"Pošiljam order...")

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.constants import POLYGON
from py_clob_client_v2.clob_types import MarketOrderArgs, OrderType
from py_clob_client_v2.order_builder.constants import BUY

cl = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY, chain_id=POLYGON,
                signature_type=1, funder=PROXY_ADDR)
cl.set_api_creds(cl.create_or_derive_api_key())

args  = MarketOrderArgs(token_id=tok, amount=SIZE, side=BUY,
                        price=1.0, order_type=OrderType.FOK)
order = cl.create_market_order(args)

t0   = time.perf_counter()
resp = cl.post_order(order, OrderType.FOK)
ms   = round((time.perf_counter() - t0) * 1000)

print(f"Response: {resp}")
taking = float(resp.get("takingAmount") or 0)
making = float(resp.get("makingAmount") or 0)

if making < 0.01:
    print(f"\n>>> NI FILLA ({ms}ms) — FOK zavrnjen ali knjiga prazna")
else:
    shares     = round(taking, 4)
    fill_price = round(making / taking, 4) if taking else 0
    print(f"\n>>> FILL ✓  {shares:.4f} delnic @ {fill_price:.4f} | plačano ${making:.2f} | čas: {ms}ms")
