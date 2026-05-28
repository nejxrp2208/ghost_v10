#!/usr/bin/env python3
"""Daily GHOST PREDATOR ledger snapshot -> Telegram. Run by VPS cron.
Sends the fee-adjusted live-fillable summary + per-regime split (concise)."""
import os, subprocess, ssl, urllib.parse, urllib.request
from datetime import datetime, timezone

SD = os.path.dirname(os.path.abspath(__file__))
def load_env():
    p = os.path.join(SD, ".env")
    for line in open(p):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if "#" in v: v = v[:v.index("#")]
            os.environ.setdefault(k.strip(), v.strip())
load_env()
TG = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CH = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# run the ledger, keep only the summary (from the totals line onward — concise for Telegram)
out = subprocess.run(["/root/ghost_v10/venv/bin/python3", os.path.join(SD, "live_ledger.py"),
                      os.path.join(SD, "ghost_predator.db")],
                     capture_output=True, text=True).stdout.splitlines()
keep, grab = [], False
for ln in out:
    if "NET AFTER FEES" in ln: grab = True
    if grab: keep.append(ln)
summary = "\n".join(keep) or "no fillable trades yet"

msg = (f"📒 GHOST PREDATOR — daily ledger\n{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n\n{summary}")
if TG and CH:
    try:
        url = f"https://api.telegram.org/bot{TG}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": CH, "text": msg}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10,
                               context=ssl.create_default_context())
    except Exception as e:
        print("tg failed:", e)
print(msg)
