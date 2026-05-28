#!/bin/bash
# GHOST PREDATOR watchdog — restarts a process ONLY if it is truly dead.
# IDEMPOTENT BY DESIGN (can never spawn a duplicate):
#   - run under `flock -n` via cron, so only ONE watchdog runs at a time (no race)
#   - counts ONLY real python processes (ps -C python), so a leftover launch shell
#     can neither mask a dead bot nor be mistaken for one
#   - starts a process ONLY when its count == 0
# Restarts are logged + pinged to Telegram so you always know it happened.
cd /root/ghost_predator || exit 1
PY=/root/gp_venv/bin/python

TG=$(grep -E '^TELEGRAM_BOT_TOKEN=' .env | head -1 | cut -d= -f2 | tr -d ' ')
CH=$(grep -E '^TELEGRAM_CHAT_ID=' .env | head -1 | cut -d= -f2 | tr -d ' ')
notify(){ [ -n "$TG" ] && [ -n "$CH" ] && curl -s -m 8 \
  "https://api.telegram.org/bot$TG/sendMessage" \
  --data-urlencode "chat_id=$CH" --data-urlencode "text=$1" >/dev/null 2>&1; }

count(){ ps -C python -o args --no-headers 2>/dev/null | grep -c -- "$1"; }

check(){  # $1=script  $2=logfile
  local n; n=$(count "$1")
  if [ "$n" -eq 0 ]; then
    echo "$(date -u +%FT%TZ) $1 DOWN -> restarting"
    setsid $PY -u "$1" >> "$2" 2>&1 < /dev/null &
    sleep 3
    local m; m=$(count "$1")
    echo "$(date -u +%FT%TZ) $1 restarted -> now $m running"
    notify "Watchdog: $1 was DOWN — restarted $(date -u +%H:%MZ). Now running: $m"
  fi
}

check ghost_predator.py predator.log
check resolver.py       resolver.log
check alarmghost.py     alarmghost.log
