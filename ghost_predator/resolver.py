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
DB = os.getenv("GP_DB_PATH", os.path.join(SD, "ghost_predator.db"))
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
# Shadow mode: if GP_DB_PATH is set, this is a shadow resolver — no Telegram
_SHADOW_MODE = bool(os.getenv("GP_DB_PATH", ""))
TG_TOKEN = "" if _SHADOW_MODE else os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TG_CHAT  = "" if _SHADOW_MODE else os.getenv("TELEGRAM_CHAT_ID","").strip()

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
        for pid,coin,outcome,token,cond,size,shares,close_ts,status,conf,ts_iso,entry,open_px,slug in rows:
            if close_ts and now < close_ts + CLOSE_BUFFER:
                continue
            wtok, ids = pm_resolution(cond)
            # side-safety: our token must belong to this market
            if ids and token not in ids:
                print(f"[resolver] !! #{pid} token NOT in market {cond[:10]} — refusing to settle")
                continue
            if wtok is None:
                # Polymarket has NOT settled on-chain yet. Make sure the record
                # does not show an unconfirmed win/loss — revert to open.
                if status != "open":
                    c=db(); c.execute("UPDATE positions SET status='open',pnl=0,resolved_at=NULL WHERE id=?",(pid,)); c.commit(); c.close()
                    print(f"[resolver] #{pid} {coin} {outcome} reverted to PENDING (PM not settled yet)")
                continue
            won = (wtok == token)
            pnl = round(shares-size,2) if won else round(-size,2)
            new_status = "won" if won else "lost"
            resolved_iso = datetime.now(timezone.utc).isoformat()
            c=db()
            c.execute("UPDATE positions SET status=?,pnl=?,resolved_at=?,pm_confirmed=1 WHERE id=?",
                      (new_status,pnl,resolved_iso,pid))
            c.commit(); c.close()
            print(f"[resolver] {'WIN' if won else 'loss'} #{pid} {coin} {outcome} PM-confirmed | ${pnl:+.2f}")
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
               f"⏱ Held {held_str(ts_iso,resolved_iso)} · resolved via Polymarket token (CLOB authoritative)"
               f"{move}\n\n"
               f"📊 Lifetime: {w}W / {l}L ({wr:.1f}% WR) · ${life:+.2f}")
        time.sleep(20)

if __name__=="__main__":
    try: main()
    except KeyboardInterrupt: print("\n[resolver stopped]")
