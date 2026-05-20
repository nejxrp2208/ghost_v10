# GhostScanner v10 — Claude Code Guide

## Project Overview

4-tier Polymarket crypto sniper bot (paper mode first, live when validated):

| Process | File | Role |
|---------|------|------|
| `scanner` | `crypto_ghost_scanner.py` | Scanner 1: cheap longshot (entry ≤$0.03, last 5-60s) — `strategy='lottery'` |
| `scanner3` | `crypto_ghost_scanner3.py` | Scanner 3: precision sniper (hardcoded filters, parallel S1 test) — `strategy='s3'` |
| `scanner2` | `crypto_ghost_scanner2.py` | Scanner 2: raw data collector (tier=6) — `strategy='raw'` |
| `scanner4` | `crypto_ghost_scanner4.py` | Scanner 4: early 50/50 zone predator (CVD+HMA+OBI, $0.38-$0.45) — `strategy='s4_reversal'` |
| `scanner5` | `crypto_ghost_scanner5.py` | Scanner 5: precision timer (BTC+ETH only, skip dead zone 10-45s) — `strategy='s5_precision'` |
| `resolver` | `crypto_ghost_resolver.py` | Resolves trades via Chainlink → Polymarket (shared, handles all scanners) |
| `redeemer` | `crypto_ghost_redeemer.py` | Claims winnings on-chain (shared) |
| `marketghost` | `marketghost.py` | Silent data collector (no trades) |
| `ghostscanner-rt` | — | Real-time scanner variant |
| `ghost-lattice` | `ghost_lattice/GHOST_LATTICE.py` | GHOST_LATTICE: unified ML scanner (T1=WRAITH, T2=SPECTER, ghost_brain) |
| `ghost-lattice-resolver` | `ghost_lattice/GHOST_LATTICE_resolver.py` | GHOST_LATTICE resolver (separate from v10 resolver) |
| `ghost-lattice-redeemer` | `ghost_lattice/GHOST_LATTICE_redeemer.py` | GHOST_LATTICE redeemer (separate from v10 redeemer) |

**Scanner architecture:** Each scanner = one strategy = one PM2 process. All write to the same `crypto_ghost_PAPER.db`, distinguished by `strategy` column. Resolver handles all.

**Scanner 3 hardcoded overrides (does NOT read these from .env):**
- `T2_MAX_ENTRY = 0.010` (S1 reads from .env, default 0.030)
- `MIN_NO_PRICE = 0.98` (S1 reads from .env, default 0.97)
- `MIN_TREND_STRENGTH = 0.003` (S1 hardcoded at 0.001)
- Exhaustion gate: skips if 1h trend direction contradicts last 3 Binance ticks

**Scanner 4 hardcoded config (completely different entry logic, nothing from .env):**
- `S4_ENTRY_MIN = 0.38`, `S4_ENTRY_MAX = 0.45` — 50/50 zone only
- `S4_MIN_MINS = 1.5`, `S4_MAX_MINS = 5.5` — enter early, not last 60s
- `S4_SIZE_USDC = 3.0`, `S4_TIER = 8`, `strategy = 's4_reversal'`
- Pillar 1: Rolling CVD (5×1min Binance klines) — net pressure positive AND improving
- Pillar 2: ATR-relative squeeze (`range < 0.6×ATR10`) + OBI > 0.12 on entry token
- Pillar 3: HMA direction (period 9) matches entry side
- Anomaly switch: OBI vs CVD strongly conflicting → skip (trap)
- Spread filter: ask−bid ≤ $0.04
- Telegram alert on every fire

**Scanner 5 hardcoded config (fork of Scanner 3):**
- BTC + ETH only (no BNB/SOL/XRP)
- Dead zone filter: skip tier=2 trades with `10 < secs_left < 45` (0/44 wins historically)
- Both UP and DOWN active
- `S5_TIER = 9`, `strategy = 's5_precision'`

**GHOST_LATTICE v10 — separate system in `ghost_lattice/` subfolder:**
- Location: `/root/ghost_v10/ghost_lattice/`
- Own `.env`: `/root/ghost_v10/ghost_lattice/.env` (separate from v10 .env)
- Own DB: `ghost_lattice/GHOST_LATTICE_PAPER.db` (paper) / `GHOST_LATTICE.db` (live)
- Dashboard (display-only, PM2 manages processes): `python3 ghost_lattice/GHOST_LATTICE_dashboard.py`
- T1=WRAITH (contrarian), T2=SPECTER (oracle-lag), `ghost_brain.py` ML scoring (0-100)
- Flat $6.66 sizing, CoinDirEngine (auto-blocks negative-EV pairs), HourBiasEngine, AdaptiveRisk

**Tech:** Python async (`asyncio` + `aiohttp`), SQLite, Polygon/Chainlink, Polymarket CLOB + Gamma APIs.

**DBs (v10):** `crypto_ghost_PAPER.db` (trades), `scanner.db` (book/summary), `marketghost.db` (market snapshots).
**DBs (GHOST_LATTICE):** `ghost_lattice/GHOST_LATTICE_PAPER.db` (paper trades), `ghost_lattice/GHOST_LATTICE.db` (live trades).

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
# ── GhostScanner v10 ──────────────────────────────────────────
pm2 restart marketghost      # data collector
pm2 restart scanner          # Scanner 1: cheap longshot (strategy='lottery')
pm2 restart scanner3         # Scanner 3: precision sniper (strategy='s3', parallel test)
pm2 restart scanner4         # Scanner 4: early 50/50 zone predator (strategy='s4_reversal')
pm2 restart scanner5         # Scanner 5: BTC+ETH, dead zone filter (strategy='s5_precision')
pm2 restart scanner2         # Scanner 2: raw data collector (strategy='raw')
pm2 restart resolver         # trade resolver (shared — handles all v10 scanners)
pm2 restart redeemer         # on-chain redeemer (shared)
pm2 restart ghostscanner-rt  # real-time scanner
pm2 restart all              # everything at once

# ── GHOST_LATTICE v10 ─────────────────────────────────────────
pm2 restart ghost-lattice            # main scanner (T1+T2, ghost_brain ML)
pm2 restart ghost-lattice-resolver   # GHOST_LATTICE resolver
pm2 restart ghost-lattice-redeemer   # GHOST_LATTICE redeemer
```

### GHOST_LATTICE Dashboard

```bash
cd /root/ghost_v10 && source venv/bin/activate && python3 ghost_lattice/GHOST_LATTICE_dashboard.py
```

Dashboard je display-only — PM2 upravlja procese. Ne zažene dvojnikov.

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
