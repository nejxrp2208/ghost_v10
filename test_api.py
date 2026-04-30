"""Use wallet activity to discover active crypto markets — python test_api.py"""
import asyncio, aiohttp, sys, json, urllib.request
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DATA_API = "https://data-api.polymarket.com"
CLOB     = "https://clob.polymarket.com"
HEADERS  = {"User-Agent": "Mozilla/5.0"}

# Wallets to scan for active market discovery (read-only public activity).
# Sanity-test only — the live bot no longer uses third-party wallet
# discovery. Edit this list freely; nothing depends on it at runtime.
WALLETS = [
    ("Your wallet",      "0x8e578e64359ee17925e20a67e4ad2705c6ce2ba9"),
]

async def test():
    async with aiohttp.ClientSession(headers=HEADERS) as session:

        discovered_tokens = {}  # token_id -> market info

        print("Step 1: Fetch recent trades from known wallets to discover active markets...")
        for name, wallet in WALLETS:
            try:
                url = (f"{DATA_API}/activity?user={wallet}"
                       f"&type=TRADE&limit=50&sortBy=TIMESTAMP&sortDirection=DESC")
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    trades = await r.json()
                    trades = trades if isinstance(trades, list) else []
                    print(f"\n  {name}: {len(trades)} recent trades")
                    for t in trades:
                        title  = t.get("title","")
                        asset  = str(t.get("asset",""))
                        cid    = t.get("conditionId","")
                        price  = float(t.get("price",0))
                        outcome= t.get("outcome","")
                        # Only care about crypto
                        tlow = title.lower()
                        if any(x in tlow for x in ["btc","eth","bitcoin","ethereum",
                                                    "sol","xrp","up or down","crypto"]):
                            discovered_tokens[asset] = {
                                "title": title, "condition_id": cid,
                                "price": price, "outcome": outcome
                            }
                            print(f"    CRYPTO: {title[:55]} | {outcome} @ {price:.3f}")
            except Exception as e:
                print(f"  {name} ERROR: {e}")

        print(f"\n\nStep 2: Discovered {len(discovered_tokens)} unique crypto token IDs")
        print("Fetching orderbooks to find signals...\n")

        signals = []
        for token_id, info in list(discovered_tokens.items())[:20]:
            try:
                async with session.get(f"{CLOB}/book",
                    params={"token_id": token_id},
                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status != 200:
                        continue
                    book = await r.json()
                    asks = book.get("asks",[])
                    if not asks:
                        continue
                    best_ask = min(float(a["price"]) for a in asks)
                    liq = sum(float(a["price"])*float(a["size"]) for a in asks[:3])
                    if best_ask <= 0.20 and liq >= 1.0:
                        signals.append((best_ask, liq, info["title"], info["outcome"]))
            except Exception:
                continue

        signals.sort()
        print(f"Signals found (YES ask <= 0.20): {len(signals)}")
        for ask, liq, title, outcome in signals[:10]:
            print(f"  ask={ask:.3f} liq=${liq:.1f} | {outcome} | {title[:55]}")

asyncio.run(test())
input("\nPress Enter to close...")
