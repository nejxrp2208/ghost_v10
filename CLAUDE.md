# GhostScanner v10 — Claude Code Guide

## Project Overview

4-tier Polymarket crypto sniper bot (paper mode first, live when validated):

| Process | File | Role |
|---------|------|------|
| `scanner` | `crypto_ghost_scanner.py` | Scanner 1: cheap longshot (entry ≤$0.03, last 5-60s) |
| `scanner2` | `crypto_ghost_scanner2.py` | Scanner 2: mean-reversion mid-prob (entry $0.35-$0.45) |
| `resolver` | `crypto_ghost_resolver.py` | Resolves trades via Chainlink → Polymarket (shared) |
| `redeemer` | `crypto_ghost_redeemer.py` | Claims winnings on-chain (shared) |
| `marketghost` | `marketghost.py` | Silent data collector (no trades) |
| `ghostscanner-rt` | — | Real-time scanner variant |

**Scanner architecture:** Each scanner = one strategy = one PM2 process. Both write to the same `crypto_ghost_PAPER.db` (tier=2 for Scanner 1, tier=5 for Scanner 2). Resolver handles both.

**Tech:** Python async (`asyncio` + `aiohttp`), SQLite, Polygon/Chainlink, Polymarket CLOB + Gamma APIs.

**DBs:** `crypto_ghost_PAPER.db` (trades), `scanner.db` (book/summary), `marketghost.db` (market snapshots).

---

## VPS Deployment

**Always deploy via GitHub — never scp.**

```bash
# Local: commit + push
git add <files> && git commit -m "..." && git push origin master

# VPS: pull + restart
cd /root/ghost_v10 && git pull && pm2 restart <name>
```

VPS: `root@70.34.204.152` · `/root/ghost_v10` · Ubuntu 24.04 · Stockholm

### PM2 Process Names

```bash
pm2 restart marketghost      # data collector
pm2 restart scanner          # Scanner 1: cheap longshot
pm2 restart scanner2         # Scanner 2: mean-reversion
pm2 restart resolver         # trade resolver (shared)
pm2 restart redeemer         # on-chain redeemer (shared)
pm2 restart ghostscanner-rt  # real-time scanner
pm2 restart all              # everything at once
```

---

## Workflow

### Plan Before Acting
- For any task with 3+ steps or architectural impact: describe the plan and wait for confirmation before touching code.
- If something goes sideways mid-task: stop, re-plan, confirm before continuing.

### Subagents
- Use subagents for research, exploration, and parallel lookups to keep main context clean.
- One focused task per subagent — don't overload a single subagent.

### Verification
- Never call a task done without proving it works (logs, output, or test).
- For VPS changes: check `pm2 logs <name>` after restart to confirm no errors.

### Bug Fixing
- When given a bug: diagnose from logs/errors first, then fix. No guessing.
- Point at the root cause explicitly before changing code.

---

## Core Principles

- **Simplicity first** — minimal code change for maximum effect. Don't add abstractions for hypothetical future needs.
- **No laziness** — find root causes, not workarounds. Senior developer standard.
- **Minimal impact** — only touch what's necessary. Avoid unintended regressions.
- **Elegance when it matters** — for non-trivial changes, ask "is there a cleaner way?" Skip this for simple obvious fixes.
- **Paper mode is sacred** — never change `PAPER_TRADE=true` to false without explicit user instruction.

---

## Key APIs

| API | Base URL | Used for |
|-----|----------|----------|
| Gamma | `https://gamma-api.polymarket.com` | Market discovery, resolution |
| CLOB | `https://clob.polymarket.com` | Order books, trading |
| Binance | `https://api.binance.com` | Price data (global endpoint) |
| Chainlink | Polygon RPC | Settlement source (same as PM's UMA) |
