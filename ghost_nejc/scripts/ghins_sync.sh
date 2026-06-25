#!/bin/bash
# ghins_sync.sh — zero-touch team-brain sync.
# Pulls the team repo every hour and auto-pushes YOUR CLAUDE.md updates.
# You do nothing after the one-time install.
#
#   ONE-TIME SETUP (from the repo folder):
#       bash scripts/ghins_sync.sh install
#
#   That's it. An hourly job is created. Log: ~/.ghins-sync.log
#   To remove:  bash scripts/ghins_sync.sh uninstall
#
# What it does every hour:
#   1. Commits any local edits to CLAUDE.md (yours stay yours — dated commit)
#   2. Pulls the team's latest (append-only merges resolve automatically
#      via .gitattributes merge=union — concurrent lessons never conflict)
#   3. Pushes your commits up
#
# SAFETY: only CLAUDE.md is ever auto-committed. Your .env, my-bot.md,
# databases and anything else NEVER get pushed by this script.

set -u
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="ghins-sync-$(basename "$REPO_DIR")"
LOG="$HOME/.ghins-sync.log"
LOCK="/tmp/$TAG.lock"

if [ "${1:-}" = "install" ]; then
    LINE="0 * * * * bash '$REPO_DIR/scripts/ghins_sync.sh' >> '$LOG' 2>&1 # $TAG"
    ( crontab -l 2>/dev/null | grep -v "# $TAG\$"; echo "$LINE" ) | crontab -
    echo "✓ Hourly sync installed for: $REPO_DIR"
    echo "  Log: $LOG"
    echo "  Verifying your GitHub auth with a test pull..."
    cd "$REPO_DIR" && git pull -q && echo "  ✓ Pull works. You're done — touch nothing else." \
        || echo "  ✗ Pull failed — fix your GitHub access first (see README), then rerun install."
    exit 0
fi

if [ "${1:-}" = "uninstall" ]; then
    crontab -l 2>/dev/null | grep -v "# $TAG\$" | crontab -
    echo "✓ Hourly sync removed for: $REPO_DIR"
    exit 0
fi

# ── one sync cycle (run by cron) ─────────────────────────────────────────────
# portable lock (macOS has no flock): mkdir is atomic; stale after 30 min
if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
        rmdir "$LOCK" 2>/dev/null; mkdir "$LOCK" 2>/dev/null || exit 0
    else
        exit 0
    fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

cd "$REPO_DIR" || exit 1
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" || exit 1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] sync: $REPO_DIR ($BRANCH)"

# 1. commit local CLAUDE.md edits (ONLY CLAUDE.md — nothing else, ever)
if ! git diff --quiet -- CLAUDE.md || ! git diff --cached --quiet -- CLAUDE.md; then
    git add CLAUDE.md
    git commit -q -m "auto-sync: CLAUDE.md update [$(git config user.name 2>/dev/null || hostname)]" -- CLAUDE.md \
        && echo "  committed local CLAUDE.md edits"
fi

# 2. pull (rebase keeps your commits on top; union-merge handles append races)
if ! git pull --rebase -q origin "$BRANCH" 2>>"$LOG"; then
    git rebase --abort 2>/dev/null
    git pull --no-rebase -q origin "$BRANCH" 2>>"$LOG" \
        && echo "  pulled (merge fallback)" \
        || { echo "  ✗ PULL FAILED — resolve manually in $REPO_DIR"; exit 1; }
else
    echo "  pulled"
fi

# 3. push anything local
if [ -n "$(git log origin/"$BRANCH"..HEAD --oneline 2>/dev/null)" ]; then
    git push -q origin "$BRANCH" \
        && echo "  ✓ pushed your updates to the team" \
        || echo "  ✗ PUSH FAILED — check your GitHub auth (token expired?)"
else
    echo "  nothing to push"
fi
