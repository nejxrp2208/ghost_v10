#!/usr/bin/env python3
"""
PREDATOR ALARMGHOST — standalone strategy-health alarm for GHOST PREDATOR.

Notification-only: it NEVER halts the bot and NEVER edits config. It just reads
the Predator positions DB and pings Telegram when something looks off, so you
NOTICE problems early.

Fully self-contained — ZERO dependency on GHOST LATTICE. It reads this folder's
ghost_predator.db and uses this folder's own .env Telegram creds. Nothing here
touches, imports, or reads anything from the ghins inc / GHOST LATTICE tree.

Triggers (each rate-limited to one alert per state per ALARM_COOLDOWN_H hours,
and re-armed once the condition clears):
  • Loss streak  >= MAX_LOSS_STREAK   (mirrors the bot's own halt threshold)
  • Same-direction streak >= ALARM_DIR_STREAK
  • Cold run: 0% win over the last ALARM_COLD_MIN settled trades
  • Today's PnL <= -DAILY_LOSS_LIMIT  (mirrors the bot's daily halt)

Run:  python3 alarmghost.py            (alongside ghost_predator.py + resolver.py)
"""
import os, time, json, sqlite3, ssl, urllib.parse, urllib.request
from datetime import datetime, timezone

STATE_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
HEARTBEAT_SECS = 30 * 60  # 30 minutes

SD    = os.path.dirname(os.path.abspath(__file__))
DB    = os.path.join(SD, "ghost_predator.db")          # Predator's own DB — nothing else
STATE = os.path.join(SD, "alarmghost_state.json")

# reuse Predator's .env (same Telegram creds + the same thresholds the bot halts on)
def load_env():
    p = os.path.join(SD, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if "#" in v: v = v[:v.index("#")]
                os.environ.setdefault(k.strip(), v.strip())
load_env()

TG_TOKEN         = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT          = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MAX_LOSS_STREAK  = int(float(os.getenv("MAX_LOSS_STREAK", "5")))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "150"))
DIR_STREAK       = int(os.getenv("ALARM_DIR_STREAK", "6"))   # consecutive same-side bets
COLD_MIN         = int(os.getenv("ALARM_COLD_MIN", "15"))    # window for a 0%-win "cold run"
POLL_SECS        = int(os.getenv("ALARM_POLL_SECS", "30"))
COOLDOWN_H       = float(os.getenv("ALARM_COOLDOWN_H", "6"))

def ts(): return datetime.now().strftime("%H:%M:%S")

def tg(msg):
    if not (TG_TOKEN and TG_CHAT): return
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()
        ctx  = ssl.create_default_context()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=8, context=ctx)
    except Exception as e:
        print(f"[{ts()}] telegram failed: {e}")

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {}
def save_state(s):
    try: json.dump(s, open(STATE, "w"))
    except Exception: pass

def settled_desc():
    """All settled trades, newest first: (id, coin, outcome, status, pnl, ts)."""
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = c.execute("SELECT id,coin,outcome,status,COALESCE(pnl,0),ts FROM positions "
                         "WHERE status IN ('won','lost') ORDER BY id DESC").fetchall()
        c.close()
        return rows
    except Exception as e:
        print(f"[{ts()}] db read err: {e}")
        return []

def check():
    rows = settled_desc()
    if not rows:
        return
    # loss streak (newest-first)
    ls = 0
    for r in rows:
        if r[3] == "lost": ls += 1
        else: break
    # same-direction streak
    ds = 0; dside = None
    for r in rows:
        if dside is None: dside = r[2]; ds = 1; continue
        if r[2] == dside: ds += 1
        else: break
    # cold run: 0% wins over the most recent COLD_MIN settled
    recent = rows[:COLD_MIN]
    wins_recent = sum(1 for r in recent if r[3] == "won")
    # today's pnl + W/L
    today      = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_rows = [r for r in rows if (r[5] or "")[:10] == today]
    tp         = sum((r[4] or 0) for r in today_rows)
    today_won  = sum(1 for r in today_rows if r[3] == "won")
    today_lost = sum(1 for r in today_rows if r[3] == "lost")
    base  = f"\n📊 Predator today: {today_won}W/{today_lost}L  ${tp:+.2f}"

    st = load_state(); now_h = time.time() / 3600.0
    heartbeat(st, now_h)
    def fire(key, msg):
        if now_h - st.get(key, 0) >= COOLDOWN_H:
            tg(msg); st[key] = now_h
            print(f"[{ts()}] ALERT {key} sent")

    # loss streak
    if ls >= MAX_LOSS_STREAK:
        fire("loss_streak", f"🥶 PREDATOR AlarmGhost — loss streak\n"
                            f"{ls} consecutive losses (halt threshold {MAX_LOSS_STREAK}).{base}")
    else:
        st.pop("loss_streak", None)
    # direction streak
    if ds >= DIR_STREAK:
        fire("dir_streak", f"↪️ PREDATOR AlarmGhost — one-sided run\n"
                           f"{ds} {dside} bets in a row.{base}")
    else:
        st.pop("dir_streak", None)
    # cold run
    if len(recent) >= COLD_MIN and wins_recent == 0:
        fire("cold", f"🧊 PREDATOR AlarmGhost — cold run\n"
                     f"0 wins in the last {len(recent)} settled trades.{base}")
    else:
        st.pop("cold", None)
    # daily loss
    if tp <= -DAILY_LOSS_LIMIT:
        fire("daily", f"🛑 PREDATOR AlarmGhost — daily loss limit\n"
                      f"Today ${tp:+.2f} (limit -${DAILY_LOSS_LIMIT:.0f}).{base}")
    else:
        st.pop("daily", None)

    save_state(st)

def heartbeat(st, now_h):
    """Send 30-minute balance + daily summary to Telegram."""
    if now_h - st.get("heartbeat", 0) < HEARTBEAT_SECS / 3600.0:
        return
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_rows = c.execute(
            "SELECT status, COALESCE(pnl,0) FROM positions "
            "WHERE status IN ('won','lost') AND substr(ts,1,10)=?", (today,)
        ).fetchall()
        c.close()
        tp        = sum(r[1] for r in today_rows)
        today_won  = sum(1 for r in today_rows if r[0] == "won")
        today_lost = sum(1 for r in today_rows if r[0] == "lost")
    except Exception:
        tp = today_won = today_lost = 0

    pm_bal = None
    try:
        s = json.load(open(STATE_JSON))
        pm_bal = s.get("pm_balance")
    except Exception:
        pass

    total_today = today_won + today_lost
    wr = f"{today_won/total_today*100:.0f}%" if total_today else "—"
    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
    bal_line = f"Balance:  ${pm_bal:,.2f}" if pm_bal is not None else ""
    msg = (f"📊 PREDATOR — 30m update\n{now_utc}\n\n"
           + (f"{bal_line}\n" if bal_line else "")
           + f"Today:    ${tp:+.2f}  ({today_won}W / {today_lost}L)\n"
           + f"Win rate: {wr}")
    tg(msg)
    st["heartbeat"] = now_h
    print(f"[{ts()}] heartbeat sent")


def main():
    print(f"[{ts()}] PREDATOR AlarmGhost — watching {os.path.basename(DB)} "
          f"(loss>={MAX_LOSS_STREAK} dir>={DIR_STREAK} cold>={COLD_MIN} daily<=-{DAILY_LOSS_LIMIT:.0f}) "
          f"poll {POLL_SECS}s | standalone, no GHOST LATTICE")
    if not (TG_TOKEN and TG_CHAT):
        print(f"[{ts()}] no Telegram creds in .env — alerts disabled (still logs)")
    while True:
        try: check()
        except Exception as e: print(f"[{ts()}] err: {e}")
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    main()
