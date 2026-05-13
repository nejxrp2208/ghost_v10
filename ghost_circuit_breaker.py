"""
ghost_circuit_breaker.py — automatic halt watcher for the trade scanner.

Runs as a background agent. Polls the trade DB every 30s. If any of these
fire, it writes `tasks/HALT.flag` (and HALT.reason.txt) — the scanner sees
the flag at the start of each scan cycle and skips all new entries.

Triggers (math-backed thresholds; none fire on normal variance):
    1. Loss streak >= CB_STREAK_LIMIT          (default 100 — 0.003% prob at 10% WR)
    2. Trailing N WR < CB_TRAILING_WR          (default 200 trades, 4% WR — catastrophe)
       Only fires once N trades exist (won't fire on early operation)
    3. Total all-time PnL <= CB_TOTAL_PNL_FLOOR (default $0 — full-cushion loss)
    4. Last-24h PnL <= CB_DAILY_PNL_FLOOR       (default -$500)

The breaker DOES NOT auto-resume. Once HALT.flag is created, you must manually
delete it to resume entries. This is intentional: any condition that triggered
the halt deserves human review before continuing.

  Manual halt:    `New-Item tasks/HALT.flag` (or `touch tasks/HALT.flag`)
  Manual resume:  `Remove-Item tasks/HALT.flag` (or `rm tasks/HALT.flag`)

Telegram alert is sent once on transition into halt state. Reads token/chat-id
from .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).

Run:
    python ghost_circuit_breaker.py
"""
import os, sys, time, sqlite3
import urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Load .env (best-effort)
try:
    from dotenv import load_dotenv
    SD = os.path.dirname(os.path.abspath(__file__))
    ep = os.path.join(SD, ".env")
    if os.path.exists(ep):
        load_dotenv(ep, override=True)
except ImportError:
    SD = os.path.dirname(os.path.abspath(__file__))

# ── Config ────────────────────────────────────────────────────────────────────
PAPER          = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true", "1", "yes")
DB_PATH        = os.path.join(SD, "crypto_ghost_PAPER.db" if PAPER else "crypto_ghost.db")
TASKS_DIR      = os.path.join(SD, "tasks")
HALT_FLAG      = os.path.join(TASKS_DIR, "HALT.flag")
HALT_REASON    = os.path.join(TASKS_DIR, "HALT.reason.txt")

POLL_S         = int(os.getenv("CB_POLL_SEC",          "30"))
THR_STREAK     = int(os.getenv("CB_STREAK_LIMIT",      "100"))
THR_TRAIL_N    = int(os.getenv("CB_TRAILING_N",        "200"))
THR_TRAIL_WR   = float(os.getenv("CB_TRAILING_WR",     "0.04"))   # 4%
THR_TOTAL_PNL  = float(os.getenv("CB_TOTAL_PNL_FLOOR", "0"))      # $
THR_DAILY_PNL  = float(os.getenv("CB_DAILY_PNL_FLOOR", "-500"))   # $

TG_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT        = os.getenv("TELEGRAM_CHAT_ID",   "").strip()


# ── Telegram (one-shot, no deps) ──────────────────────────────────────────────
def alert_telegram(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    TG_CHAT,
            "text":       msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(url, data=data, timeout=10).read()
    except Exception as e:
        print(f"[CB] telegram alert failed: {e}", flush=True)


# ── Trigger checks (returns reason string if any fires) ───────────────────────
def check() -> str | None:
    if not os.path.exists(DB_PATH):
        return None
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except Exception:
        return None
    cur = con.cursor()

    # 1) Loss streak (count consecutive losses from newest backward)
    rows = cur.execute(
        "SELECT status FROM trades WHERE status IN ('won','lost') ORDER BY ts DESC"
    ).fetchall()
    streak = 0
    for (s,) in rows:
        if s == 'lost':
            streak += 1
        else:
            break
    if streak >= THR_STREAK:
        con.close()
        return f"Loss streak {streak} >= {THR_STREAK}"

    # 2) Trailing-N WR
    trail = cur.execute(
        f"SELECT status FROM trades WHERE status IN ('won','lost') "
        f"ORDER BY ts DESC LIMIT {THR_TRAIL_N}"
    ).fetchall()
    if len(trail) >= THR_TRAIL_N:
        wins = sum(1 for (s,) in trail if s == 'won')
        wr = wins / len(trail)
        if wr < THR_TRAIL_WR:
            con.close()
            return (f"Trailing {THR_TRAIL_N} trades WR={wr*100:.1f}% "
                    f"< {THR_TRAIL_WR*100:.0f}%")

    # 3) Total all-time PnL
    tot = cur.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status IN ('won','lost')"
    ).fetchone()[0] or 0.0
    if tot <= THR_TOTAL_PNL:
        con.close()
        return f"Total PnL ${tot:.2f} <= ${THR_TOTAL_PNL:.2f}"

    # 4) Last-24h PnL
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    daily = cur.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM trades "
        "WHERE ts >= ? AND status IN ('won','lost')",
        (cutoff,)
    ).fetchone()[0] or 0.0
    if daily <= THR_DAILY_PNL:
        con.close()
        return f"24h PnL ${daily:.2f} <= ${THR_DAILY_PNL:.2f}"

    con.close()
    return None


# ── Main loop ─────────────────────────────────────────────────────────────────
def main() -> None:
    os.makedirs(TASKS_DIR, exist_ok=True)
    print(f"[CB] starting — poll={POLL_S}s  thresholds:", flush=True)
    print(f"[CB]   streak >= {THR_STREAK}", flush=True)
    print(f"[CB]   trailing {THR_TRAIL_N} WR < {THR_TRAIL_WR*100:.0f}%", flush=True)
    print(f"[CB]   total PnL <= ${THR_TOTAL_PNL:.2f}", flush=True)
    print(f"[CB]   24h PnL <= ${THR_DAILY_PNL:.2f}", flush=True)
    print(f"[CB] DB: {DB_PATH}", flush=True)
    print(f"[CB] flag: {HALT_FLAG}", flush=True)

    while True:
        if os.path.exists(HALT_FLAG):
            # Already halted — passive monitoring only.
            try:
                with open(HALT_FLAG, 'r', encoding='utf-8') as f:
                    reason = f.read().strip() or "(unspecified)"
            except Exception:
                reason = "(unreadable)"
            print(f"[CB] HALTED — reason: {reason}  (delete {HALT_FLAG} to resume)",
                  flush=True)
        else:
            reason = check()
            if reason:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                full = (f"CIRCUIT BREAKER FIRED at {ts}\n"
                        f"Reason: {reason}\n\n"
                        f"To resume: delete tasks/HALT.flag\n")
                try:
                    with open(HALT_FLAG, 'w', encoding='utf-8') as f:
                        f.write(reason)
                    with open(HALT_REASON, 'w', encoding='utf-8') as f:
                        f.write(full)
                except Exception as e:
                    print(f"[CB] FAILED TO WRITE HALT FLAG: {e}", flush=True)
                else:
                    print(f"[CB] *** HALT TRIGGERED *** {reason}", flush=True)
                    alert_telegram(
                        f"🚨 <b>CIRCUIT BREAKER FIRED</b>\n"
                        f"<b>Reason:</b> {reason}\n\n"
                        f"<i>Bot will skip new entries until "
                        f"<code>tasks/HALT.flag</code> is deleted.</i>"
                    )
            else:
                print(f"[CB] ok @ {datetime.now().strftime('%H:%M:%S')}",
                      flush=True)

        time.sleep(POLL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[CB] stopped by user", flush=True)
        sys.exit(0)
