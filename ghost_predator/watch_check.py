#!/usr/bin/env python3
"""Read-only watch check for Ghost Predator. Prints hard-safety + walk-the-book
health for one instance. Usage: watch_check.py [dir]  (dir defaults to cwd).
Exits 0 always; surfaces TRIGGER line if a safety condition is met."""
import sqlite3, json, time, datetime, sys, os
d = sys.argv[1] if len(sys.argv) > 1 else "."
def jload(f):
    try: return json.load(open(os.path.join(d, f)))
    except Exception: return {}
st = jload("state.json")
halt = st.get("halt")
age = round(time.time() - st.get("ts", 0)) if st.get("ts") else None
c = sqlite3.connect(f"file:{os.path.join(d, 'ghost_predator.db')}?mode=ro", uri=True)
q = lambda sql, a=(): c.execute(sql, a).fetchall()
last = [r[0] for r in q("SELECT status FROM positions WHERE status IN ('won','lost') ORDER BY id DESC LIMIT 25")]
streak = 0
for s in last:
    if s == 'lost': streak += 1
    else: break
life = q("SELECT ROUND(COALESCE(SUM(pnl),0),2),COUNT(*) FROM positions WHERE status IN ('won','lost')")[0]
today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
tp = q("SELECT ROUND(COALESCE(SUM(pnl),0),2),COUNT(*) FROM positions WHERE status IN ('won','lost') AND substr(ts,1,10)=?", (today,))[0]
we = q("SELECT COUNT(DISTINCT token_id),COALESCE(SUM(status='won'),0),COUNT(*) FROM positions WHERE status IN ('won','lost') AND ROUND(size_usdc)>=29")[0]
trig = []
if halt: trig.append(f"HALT={halt}")
if streak >= 5: trig.append(f"LOSS_STREAK={streak}")
if tp[0] is not None and tp[0] <= -150: trig.append(f"DAILY_LOSS={tp[0]}")
print(f"halt={halt} state_age={age}s | lifetime ${life[0]} n={life[1]} | today ${tp[0]} n={tp[1]} | loss_streak={streak}")
print("recent(newest->old): " + "".join('W' if s == 'won' else 'L' for s in last))
wr = round(100 * we[1] / we[2]) if we[2] else 0
print(f"$30-fills: distinct_tokens={we[0]} W={we[1]} total_rows={we[2]} WR={wr}% (rows>distinct => restart re-fires)")
print("TRIGGER: " + ("; ".join(trig) if trig else "none"))
