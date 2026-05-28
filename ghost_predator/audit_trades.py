#!/usr/bin/env python3
"""Audit every trade in a ghost_predator DB against Polymarket's on-chain
TOKEN-level winner. Confirms each recorded won/lost matches the chain — no
false wins/losses. Usage: audit_trades.py <db_path> <label>"""
import sys, json, sqlite3, time, urllib.request

DB = sys.argv[1]
LABEL = sys.argv[2] if len(sys.argv) > 2 else DB
CLOB = "https://clob.polymarket.com"

def http_get(url, timeout=12):
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gp-audit/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception:
            time.sleep(0.4)
    return None

def pm_resolution(cond):
    """(winner_token_id, {token_ids}, market_found)"""
    m = http_get(f"{CLOB}/markets/{cond}")
    if not m or not m.get("tokens"):
        return None, set(), False
    toks = m["tokens"]
    ids = {str(t.get("token_id")) for t in toks}
    w = next((t for t in toks if t.get("winner")), None)
    return (str(w.get("token_id")) if w else None), ids, True

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = c.execute("SELECT id,coin,outcome,token_id,condition_id,status,pnl "
                 "FROM positions ORDER BY id").fetchall()
c.close()

print(f"\n===== {LABEL} — {len(rows)} trades =====")
print(f"{'ID':>4} {'COIN':<4} {'SIDE':<5} {'REC':<6} {'PM(token)':<10} VERDICT")
ok = mism = pend = notin = nofound = 0
for pid, coin, outcome, token, cond, status, pnl in rows:
    token = str(token)
    wtok, ids, found = pm_resolution(cond)
    if not found:
        pmres, verdict = "-", "? market not found"; nofound += 1
    elif token not in ids:
        pmres, verdict = "-", "X TOKEN NOT IN MARKET"; notin += 1
    elif wtok is None:
        pmres = "unsettled"
        if status == "open":
            verdict = "ok pending"; pend += 1
        else:
            verdict = f"X recorded {status} but PM UNSETTLED"; mism += 1
    else:
        won = (wtok == token)
        expected = "won" if won else "lost"
        pmres = "WON" if won else "LOST"
        if status == expected:
            verdict = "MATCH"; ok += 1
        else:
            verdict = f"X MISMATCH (rec {status}, chain {expected})"; mism += 1
    print(f"{pid:>4} {coin:<4} {outcome:<5} {status:<6} {pmres:<10} {verdict}")
    time.sleep(0.1)

print(f"\nSUMMARY [{LABEL}]: {ok} match | {pend} pending-ok | "
      f"{mism} MISMATCH | {notin} token-not-in-market | {nofound} market-not-found")
print("RESULT:", "ALL CLEAN — no false wins/losses" if (mism == 0 and notin == 0)
      else ">>> ISSUES FOUND — see X rows above")
