#!/usr/bin/env python3
"""
GHOST PREDATOR — minimal web terminal (top stat bar only).
Serves the 6 headline stats at http://127.0.0.1:5001 :
  Win Rate · Total P&L · Open · Portfolio · Streak · Scan Speed
Reads ghost_predator.db (positions) + state.json (live feed/config). Standalone.
"""
import os, json, sqlite3, time
from datetime import datetime, timezone
from aiohttp import web

SD = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(SD, "ghost_predator.db")
STATE = os.path.join(SD, "state.json")
PORT = 5002   # 5001 stays the old CRYPTO GHOST web; Predator gets its own port

def envf(k, d):
    try:
        for line in open(os.path.join(SD, ".env")):
            if line.strip().startswith(k + "="):
                v = line.split("=", 1)[1].split("#")[0].strip()
                return float(v)
    except Exception:
        pass
    return d
START_BAL = envf("STARTING_BALANCE", 1000.0)

def q(sql, args=()):
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
        r = c.execute(sql, args).fetchall(); c.close(); return r
    except Exception:
        return []

def load_state():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception:
        return {}

def stats():
    row = q("SELECT COUNT(*),COALESCE(SUM(status='won'),0),COALESCE(SUM(status='lost'),0),"
            "COALESCE(SUM(status='open'),0),COALESCE(SUM(pnl),0) FROM positions")
    tot, won, lost, opn, pnl = row[0] if row else (0, 0, 0, 0, 0)
    today = (q("SELECT COALESCE(SUM(pnl),0) FROM positions WHERE substr(ts,1,10)=?",
               (datetime.now(timezone.utc).strftime("%Y-%m-%d"),)) or [(0,)])[0][0]
    wr = (won / (won + lost) * 100) if (won + lost) else 0
    # streak: consecutive same result among most-recent resolved
    res = [r[0] for r in q("SELECT status FROM positions WHERE status IN('won','lost') ORDER BY id DESC LIMIT 50")]
    streak = 0
    if res:
        first = res[0]
        for s in res:
            if s == first: streak += 1
            else: break
        streak = streak if first == "won" else -streak
    st = load_state()
    feed_ms = int((time.time() - st.get("ts", 0)) * 1000) if st.get("ts") else None
    cfg = st.get("cfg", {})
    return {"wr": wr, "won": won, "lost": lost, "pnl": pnl, "today": today,
            "open": opn, "portfolio": START_BAL + pnl, "streak": streak,
            "tracking": st.get("tracking", 0), "poll": cfg.get("window") and 300,
            "feed_ms": feed_ms, "mode": st.get("mode", "PAPER"),
            "live": (feed_ms is not None and feed_ms < 5000)}

def card(label, value, sub, vcolor):
    return f"""<div class=card><div class=lbl>{label}</div>
      <div class=val style="color:{vcolor}">{value}</div><div class=sub>{sub}</div></div>"""

async def index(request):
    s = stats()
    pos = "#34d399"; neg = "#fbbf24"; cy = "#22d3ee"; wh = "#e5e7eb"
    pnl_c = pos if s["pnl"] > 0 else (neg if s["pnl"] < 0 else wh)
    today_c = pos if s["today"] > 0 else (neg if s["today"] < 0 else wh)
    wr_c = pos if s["wr"] >= 50 else (cy if s["wr"] >= 30 else neg)
    stk = s["streak"]; stk_c = pos if stk > 0 else (neg if stk < 0 else wh)
    scan = f"{s['feed_ms']}ms" if s["feed_ms"] is not None else "—"
    cards = "".join([
        card("WIN RATE", f"{s['wr']:.1f}%", f"{s['won']} W / {s['lost']} L", wr_c),
        card("TOTAL P&L", f"${s['pnl']:+,.2f}", f"Today: ${s['today']:+,.2f}", pnl_c),
        card("OPEN", f"{s['open']}", "positions", cy if s['open'] else wh),
        card("PORTFOLIO", f"${s['portfolio']:,.2f}", f"starting ${START_BAL:,.0f}", wh),
        card("STREAK", f"{stk:+d}" if stk else "0", "consecutive", stk_c),
        card("SCAN SPEED", scan, f"{s['tracking']} markets", cy),
    ])
    feed = ("● ONLINE" if s["live"] else "● STALE")
    feed_c = pos if s["live"] else neg
    html = f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=3><title>GHOST PREDATOR</title>
<style>
 body{{background:#070b12;color:#e5e7eb;font-family:'SF Mono',Menlo,monospace;margin:0;padding:28px}}
 .top{{display:flex;align-items:center;gap:18px;margin-bottom:22px}}
 .logo{{font-size:30px;font-weight:800;letter-spacing:3px;color:#22d3ee}}
 .tag{{color:#5b6675;font-size:12px;letter-spacing:2px}}
 .badge{{margin-left:auto;border:1px solid #1f6f7a;color:#22d3ee;padding:5px 12px;border-radius:6px;font-size:12px;letter-spacing:1px}}
 .row{{display:grid;grid-template-columns:repeat(6,1fr);gap:14px}}
 .card{{background:#0d1420;border:1px solid #15202e;border-radius:12px;padding:18px 20px}}
 .lbl{{color:#5b6675;font-size:12px;letter-spacing:2px;margin-bottom:10px}}
 .val{{font-size:30px;font-weight:800;letter-spacing:1px}}
 .sub{{color:#7c8794;font-size:12px;margin-top:8px}}
</style></head><body>
 <div class=top>
   <span class=logo>GHOST PREDATOR</span>
   <span class=tag>LATENCY SNIPE // {s['mode']}</span>
   <span style="color:{feed_c};font-size:13px;letter-spacing:1px">{feed}</span>
   <span class=badge>{datetime.now(timezone.utc):%H:%M:%S} UTC</span>
 </div>
 <div class=row>{cards}</div>
</body></html>"""
    return web.Response(text=html, content_type="text/html")

app = web.Application()
app.router.add_get("/", index)
if __name__ == "__main__":
    print(f"GHOST PREDATOR web terminal → http://127.0.0.1:{PORT}")
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)
