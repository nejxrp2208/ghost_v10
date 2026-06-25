#!/usr/bin/env python3
"""
resolver.py — settle GHOST PREDATOR positions 100% from Polymarket's on-chain
TOKEN WINNER. No price approximation. No false wins/losses.

For each position not yet PM-confirmed:
  - fetch clob.polymarket.com/markets/{condition_id}
  - if a token has winner=True  -> the market is settled on-chain:
        won = (our token_id == winner token_id)   [token-level, never label]
        pnl = shares - stake (win) | -stake (loss)   ; mark pm_confirmed=1
  - if no winner yet -> Polymarket hasn't settled; leave the position OPEN
    (and REVERT any earlier non-confirmed status so the record never shows a
    win/loss the chain hasn't confirmed).

Side-safety: refuses to settle if our token_id isn't even in the market.

Run alongside the bot:  python3 resolver.py
"""
import os, time, json, sqlite3, urllib.request
from datetime import datetime, timezone, timedelta

_DURS = {"5m": 300, "15m": 900, "1h": 3600}
def et_window(close_ts, dur_secs):
    try:
        s = datetime.fromtimestamp(close_ts - dur_secs, timezone.utc) - timedelta(hours=4)
        e = datetime.fromtimestamp(close_ts, timezone.utc) - timedelta(hours=4)
        return f"{s.strftime('%-I:%M')}-{e.strftime('%-I:%M')} ET"
    except Exception:
        return ""

SD = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(SD, "ghost_predator.db")
CLOB = "https://clob.polymarket.com"
CLOSE_BUFFER = 20      # don't bother polling until this many secs after close

def load_env():
    p=os.path.join(SD,".env")
    if os.path.exists(p):
        for line in open(p):
            line=line.strip()
            if line and not line.startswith("#") and "=" in line:
                k,v=line.split("=",1)
                if "#" in v: v=v[:v.index("#")]
                os.environ.setdefault(k.strip(),v.strip())
load_env()
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip(); TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","").strip()

def http_get(url,timeout=12):
    for _ in range(3):
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"ghost-predator-resolver/1.0"})
            with urllib.request.urlopen(req,timeout=timeout) as r: return json.load(r)
        except Exception: time.sleep(0.4)
    return None

def tg(msg):
    if not (TG_TOKEN and TG_CHAT): return
    try:
        import urllib.parse
        u=f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        d=urllib.parse.urlencode({"chat_id":TG_CHAT,"text":msg}).encode()
        urllib.request.urlopen(urllib.request.Request(u,data=d),timeout=8)
    except Exception: pass

def db():
    c=sqlite3.connect(DB,timeout=10); c.execute("PRAGMA journal_mode=WAL"); return c

BINANCE_SYM={"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","XRP":"XRPUSDT","BNB":"BNBUSDT"}
def binance_close(coin, close_ts):
    """Binance close price of the candle ending at close_ts — for the alert's price move."""
    sym=BINANCE_SYM.get(coin)
    if not sym: return None
    d=http_get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1m"
               f"&startTime={(close_ts-60)*1000}&limit=1")
    try: return float(d[0][4]) if d else None
    except Exception: return None

def lifetime():
    c=db()
    w,l,p=c.execute("SELECT COALESCE(SUM(status='won'),0),COALESCE(SUM(status='lost'),0),"
                    "COALESCE(SUM(pnl),0) FROM positions WHERE pm_confirmed=1").fetchone()
    c.close()
    wr=(w/(w+l)*100) if (w+l) else 0
    return w,l,wr,p

def held_str(ts_iso, resolved_iso):
    try:
        a=datetime.fromisoformat(ts_iso); b=datetime.fromisoformat(resolved_iso)
        s=int((b-a).total_seconds()); return f"{s//60}m{s%60:02d}s"
    except Exception: return "?"

def ensure_schema():
    c=db()
    cols=[r[1] for r in c.execute("PRAGMA table_info(positions)").fetchall()]
    if "pm_confirmed" not in cols:
        c.execute("ALTER TABLE positions ADD COLUMN pm_confirmed INTEGER DEFAULT 0")
    c.commit(); c.close()

def pm_resolution(cond):
    """Return (winner_token_id, token_id_set) or (None, set) if unsettled / error."""
    m=http_get(f"{CLOB}/markets/{cond}")
    if not m or not m.get("tokens"):
        return None, set()
    toks=m["tokens"]
    ids={t.get("token_id") for t in toks}
    w=next((t for t in toks if t.get("winner")),None)
    return (w.get("token_id") if w else None), ids

# ── FAST PATH: on-chain CTF payout (settles ~21s after close vs ~379s for the
#    CLOB winner flag). Reads the SAME source of truth — Polymarket's CTF payout
#    vector on Polygon — straight from the chain. No wallet, no gas, no API key. ──
CTF       = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"   # Polymarket CTF on Polygon
SEL_DEN   = "0xdd34de67"   # payoutDenominator(bytes32)
SEL_NUM   = "0x0504c814"   # payoutNumerators(bytes32,uint256)
# Public Polygon RPCs — TEST THIS LIST FROM THE VPS; some reject certain hosts.
POLY_RPCS = ["https://polygon-bor-rpc.publicnode.com",
             "https://polygon-pokt.nodies.app",
             "https://gateway.tenderly.co/public/polygon",
             "https://polygon.api.onfinality.io/public",
             "https://1rpc.io/matic",
             "https://polygon.drpc.org"]
_rpc_dead = {}            # rpc -> unix ts to skip until (benches flaky nodes ~120s)

def eth_call(data_hex, rpc, timeout=5):
    """One read-only eth_call to the CTF contract; int result or None."""
    try:
        body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call",
                           "params":[{"to":CTF,"data":data_hex},"latest"]}).encode()
        req = urllib.request.Request(rpc, data=body, headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r).get("result")
        return int(res,16) if res and res != "0x" else None
    except Exception:
        return None

def chain_winner_index(cond):
    """Winning outcome index (0/1) from the on-chain CTF payout, or None if
    unsettled / unclear. Requires TWO RPCs that RESPONDED to AGREE on the same
    settled verdict — a single bad node can never flip or fake an outcome."""
    if not cond:
        return None
    word = cond[2:].lower().zfill(64) if cond.startswith("0x") else cond.lower().zfill(64)
    now = time.time()
    verdicts = []
    for rpc in POLY_RPCS:
        if _rpc_dead.get(rpc, 0) > now:
            continue
        den = eth_call(SEL_DEN + word, rpc)
        if den is None:
            _rpc_dead[rpc] = now + 120; continue
        if den == 0:
            v = "unsettled"
        else:
            n0 = eth_call(SEL_NUM + word + "%064x" % 0, rpc)
            n1 = eth_call(SEL_NUM + word + "%064x" % 1, rpc)
            if n0 is None or n1 is None:
                _rpc_dead[rpc] = now + 120; continue
            if (n0 > 0) == (n1 > 0):          # split / tie -> nonsense for binary, never guess
                return None
            v = 0 if n0 > 0 else 1
        verdicts.append(v)
        if len(verdicts) == 2:
            if verdicts[0] == verdicts[1] and verdicts[0] != "unsettled":
                return verdicts[0]            # two responders agree on a settled winner
            return None                       # disagree / both unsettled -> retry next poll
    return None

GAMMA = "https://gamma-api.polymarket.com"
_tok_order = {}           # cond -> [token_id, ...] in OUTCOME-INDEX order (static per market)
def token_order(cond, slug):
    """Ordered clobTokenIds for a market (index 0 = outcomes[0], 1 = outcomes[1]) —
    the SAME ordering the bot uses at fire-time. Lets us map an on-chain winning
    INDEX back to a winning TOKEN_ID, so settlement stays token-level (no labels)."""
    if cond in _tok_order:
        return _tok_order[cond]
    mk = http_get(f"{GAMMA}/markets?slug={slug}") if slug else None
    toks = None
    try:
        toks = mk[0].get("clobTokenIds")
        if isinstance(toks, str): toks = json.loads(toks)
    except Exception:
        toks = None
    if toks:
        _tok_order[cond] = toks
    return toks

def main():
    ensure_schema()
    print(f"[resolver] watching {DB} — Polymarket TOKEN-level settlement only")
    while True:
        now=int(time.time())
        try:
            c=db()
            rows=c.execute("SELECT id,coin,outcome,token_id,condition_id,size_usdc,shares,"
                           "close_ts,status,COALESCE(pm_confirmed,0),ts,entry_price,open_price,slug "
                           "FROM positions WHERE COALESCE(pm_confirmed,0)=0").fetchall()
            c.close()
        except Exception as e:
            print(f"[resolver] db err: {e}"); time.sleep(20); continue
        hot = False
        for pid,coin,outcome,token,cond,size,shares,close_ts,status,conf,ts_iso,entry,open_px,slug in rows:
            if close_ts and now < close_ts + CLOSE_BUFFER:
                continue
            # ── determine the winning TOKEN: on-chain CTF first (fast), CLOB winner flag as fallback ──
            order = token_order(cond, slug)
            # side-safety: our token must belong to this market
            if order and token not in order:
                print(f"[resolver] !! #{pid} token NOT in market {cond[:10]} — refusing to settle")
                continue
            winner_tok, src = None, None
            if order:                                   # fast path needs the index→token map
                wi = chain_winner_index(cond)
                if wi is not None and wi < len(order):
                    winner_tok, src = order[wi], "chain"
            if winner_tok is None:                       # fallback: CLOB winner flag (slow, lags ~6min)
                wtok, ids = pm_resolution(cond)
                if ids and token not in ids:
                    print(f"[resolver] !! #{pid} token NOT in market {cond[:10]} — refusing to settle")
                    continue
                if wtok is not None:
                    winner_tok, src = wtok, "clob"
            if winner_tok is None:
                # Not settled on-chain OR via CLOB yet — keep polling fast and make sure
                # the record shows no unconfirmed win/loss (revert to open).
                hot = True
                if status != "open":
                    c=db(); c.execute("UPDATE positions SET status='open',pnl=0,resolved_at=NULL WHERE id=?",(pid,)); c.commit(); c.close()
                    print(f"[resolver] #{pid} {coin} {outcome} reverted to PENDING (not settled yet)")
                continue
            won = (winner_tok == token)
            pnl = round(shares-size,2) if won else round(-size,2)
            new_status = "won" if won else "lost"
            resolved_iso = datetime.now(timezone.utc).isoformat()
            c=db()
            c.execute("UPDATE positions SET status=?,pnl=?,resolved_at=?,pm_confirmed=1 WHERE id=?",
                      (new_status,pnl,resolved_iso,pid))
            c.commit(); c.close()
            print(f"[resolver] {'WIN' if won else 'loss'} #{pid} {coin} {outcome} settled via {src} | ${pnl:+.2f}")
            # ── rich Telegram WON/LOST alert ──
            dur=(slug or "").split("-")[-2] if slug else "?"
            up=outcome.upper() in ("UP","YES")
            close_px=binance_close(coin, close_ts)
            move=""
            if open_px and close_px:
                pct=(close_px-open_px)/open_px*100
                move=f"\n📈 {coin} moved ${open_px:,.2f} → ${close_px:,.2f} ({pct:+.3f}%)"
            w,l,wr,life=lifetime()
            win = et_window(close_ts, _DURS.get(dur, 300))
            tg(f"{'✅ WON' if won else '❌ LOST'} #{pid} — ${pnl:+.2f}\n"
               f"{'🟢' if up else '🔴'} {coin} {outcome.upper()} @ ${entry:.4f} (size ${size:.2f})\n"
               f"📋 {dur} · {win}\n\n"
               f"⏱ Held {held_str(ts_iso,resolved_iso)} · settled via {'on-chain CTF (~21s)' if src=='chain' else 'CLOB winner'} — token-level"
               f"{move}\n\n"
               f"📊 Lifetime: {w}W / {l}L ({wr:.1f}% WR) · ${life:+.2f}")
        time.sleep(4 if hot else 20)   # hot-poll while a closed market is still pending (~21s settle); idle otherwise

if __name__=="__main__":
    try: main()
    except KeyboardInterrupt: print("\n[resolver stopped]")
