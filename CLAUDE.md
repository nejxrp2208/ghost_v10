# GhostScanner v10 — Claude Code Guide

## Project Overview

4-tier Polymarket crypto sniper bot (paper mode first, live when validated):

| Process | File | Role |
|---------|------|------|
| `scanner` | `crypto_ghost_scanner.py` | Scanner 1: cheap longshot (entry ≤$0.03, last 5-60s) — `strategy='lottery'` |
| `scanner3` | `crypto_ghost_scanner3.py` | Scanner 3: precision sniper (hardcoded filters, parallel S1 test) — `strategy='s3'` |
| `scanner2` | `crypto_ghost_scanner2.py` | Scanner 2: raw data collector (tier=6) — `strategy='raw'` |
| `resolver` | `crypto_ghost_resolver.py` | Resolves trades via Chainlink → Polymarket (shared, handles all scanners) |
| `redeemer` | `crypto_ghost_redeemer.py` | Claims winnings on-chain (shared) |
| `marketghost` | `marketghost.py` | Silent data collector (no trades) |
| `ghostscanner-rt` | — | Real-time scanner variant |

**Scanner architecture:** Each scanner = one strategy = one PM2 process. All write to the same `crypto_ghost_PAPER.db`, distinguished by `strategy` column. Resolver handles all.

**Scanner 3 hardcoded overrides (does NOT read these from .env):**
- `T2_MAX_ENTRY = 0.010` (S1 reads from .env, default 0.030)
- `MIN_NO_PRICE = 0.98` (S1 reads from .env, default 0.97)
- `MIN_TREND_STRENGTH = 0.003` (S1 hardcoded at 0.001)
- Exhaustion gate: skips if 1h trend direction contradicts last 3 Binance ticks

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
pm2 restart scanner          # Scanner 1: cheap longshot (strategy='lottery')
pm2 restart scanner3         # Scanner 3: precision sniper (strategy='s3', parallel test)
pm2 restart scanner2         # Scanner 2: raw data collector (strategy='raw')
pm2 restart resolver         # trade resolver (shared — handles all scanners)
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
