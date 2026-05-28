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
| `zugu-scanner` | `zugu_bot/zugu_scanner.py` | ZUGU: FOLLOW LEADER strategy (50/50 zone, last 5-15s, BTC+ETH) |
| `zugu-resolver` | `zugu_bot/zugu_resolver.py` | ZUGU resolver (separate subsystem) |
| `zugu-redeemer` | `zugu_bot/zugu_redeemer.py` | ZUGU redeemer (separate subsystem) |
| `ghost-predator` | `ghost_predator/ghost_predator.py` | GHOST PREDATOR: last-window latency snipe (BTC/ETH 5m+15m, T-12s) |
| `ghost-predator-resolver` | `ghost_predator/resolver.py` | GHOST PREDATOR resolver (on-chain token-level winner, separate subsystem) |
| `ghost-predator-alarm` | `ghost_predator/alarmghost.py` | GHOST PREDATOR Telegram health alarm (loss/cold/daily-loss triggers) |

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

**GHOST PREDATOR — separate system in `ghost_predator/` subfolder:**
- Location: `/root/ghost_v10/ghost_predator/`
- Own `.env`: `/root/ghost_v10/ghost_predator/.env` (separate from v10, ghost_lattice, zugu — gitignored; copy from `.env.example`)
- Own DB: `ghost_predator/ghost_predator.db` (single DB for paper+live, distinguished by `mode` column)
- Dashboard (display-only, PM2 manages processes): `python3 ghost_predator/dashboard.py`
- Strategy: **Last-window latency snipe** — in the final ~12s of a 5m/15m up/down market, compute leader from our own Binance feed and snipe PM's stale ask before it reprices
- Entry band: `MIN_ASK=0.65`, `MAX_ASK=0.85` (favorites with profit room, no coinflips)
- Move gate: `MIN_MOVE_BPS=1.0` (≥0.01% Binance move from open required)
- Window: 5m=T-12s, 15m=T-30s (`SNIPE_WINDOW_SECS=12`, `SNIPE_WINDOW_15M=30`)
- Sizing: `WALK_FILL=true` — walks the ask ladder, fires only if full $30 fillable in-band ("$30 or skip")
- Realtime: WSS Polymarket book channel (REST fallback), `BOOK_POLL_MS=10` snipe cadence
- Guardrails: `DAILY_LOSS_LIMIT=150`, `MAX_LOSS_STREAK=5` — auto-halts trading
- See `ghost_predator/FILTERS.md` and `ghost_predator/LEARNINGS.md` for full filter chain + tuning history

**ZUGU v4 — separate system in `zugu_bot/` subfolder:**
- Location: `/root/ghost_v10/zugu_bot/`
- Own `.env`: `/root/ghost_v10/zugu_bot/.env` (separate from v10 and ghost_lattice)
- Own DB: `zugu_bot/zugu_PAPER.db` (paper) / `zugu.db` (live)
- Strategy: **FOLLOW LEADER** — last 5-15s of 5min candle, if Binance leads PTB by >=0.01%, buy that side
- Entry zone: $0.30 - $0.75 (50/50 zone, NOT cheap longshot)
- Assets: BTC + ETH only, 5 shares fixed, max 2 open trades
- Backtest: 86.9% WR, +30.7c PnL/trade (small sample, validate with paper first)
- `FL_PAPER_ONLY=true` — strategy only fires in paper mode by default

**Tech:** Python async (`asyncio` + `aiohttp`), SQLite, Polygon/Chainlink, Polymarket CLOB + Gamma APIs.

**DBs (v10):** `crypto_ghost_PAPER.db` (trades), `scanner.db` (book/summary), `marketghost.db` (market snapshots).
**DBs (GHOST_LATTICE):** `ghost_lattice/GHOST_LATTICE_PAPER.db` (paper trades), `ghost_lattice/GHOST_LATTICE.db` (live trades).
**DBs (GHOST PREDATOR):** `ghost_predator/ghost_predator.db` (paper + live, distinguished by `mode` column).

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

# ── ZUGU v4 ───────────────────────────────────────────────────
pm2 restart zugu-scanner             # FOLLOW LEADER (50/50 zone, 5-15s window)
pm2 restart zugu-resolver            # ZUGU resolver
pm2 restart zugu-redeemer            # ZUGU redeemer

# ── GHOST PREDATOR ────────────────────────────────────────────
pm2 restart ghost-predator           # main snipe engine (Binance feed + WSS book + snipe loop)
pm2 restart ghost-predator-resolver  # on-chain token-level settlement
pm2 restart ghost-predator-alarm     # Telegram health alarm (loss/cold/daily-loss)
```

### First-time deploy (zugu_bot)

```bash
cd /root/ghost_v10 && git pull
pip install -r zugu_bot/requirements.txt
# Edit zugu_bot/.env to fill PRIVATE_KEY + CLOB credentials (for live mode)
pm2 start zugu_bot/zugu_scanner.py  --name zugu-scanner  --interpreter python3
pm2 start zugu_bot/zugu_resolver.py --name zugu-resolver --interpreter python3
pm2 start zugu_bot/zugu_redeemer.py --name zugu-redeemer --interpreter python3
```

### First-time deploy (ghost_predator)

```bash
cd /root/ghost_v10 && git pull
cp ghost_predator/.env.example ghost_predator/.env   # paper defaults work as-is; fill Telegram + (later) live creds
# venv check: aiohttp + rich must be installed in /root/ghost_v10/venv
/root/ghost_v10/venv/bin/pip install aiohttp rich
pm2 start ghost_predator/ghost_predator.py --name ghost-predator           --interpreter /root/ghost_v10/venv/bin/python3
pm2 start ghost_predator/resolver.py       --name ghost-predator-resolver  --interpreter /root/ghost_v10/venv/bin/python3
pm2 start ghost_predator/alarmghost.py     --name ghost-predator-alarm     --interpreter /root/ghost_v10/venv/bin/python3
pm2 save
```

### GHOST_LATTICE Dashboard

```bash
cd /root/ghost_v10 && source venv/bin/activate && python3 ghost_lattice/GHOST_LATTICE_dashboard.py
```

Dashboard je display-only — PM2 upravlja procese. Ne zažene dvojnikov.

### GHOST PREDATOR Dashboard

```bash
cd /root/ghost_v10 && source venv/bin/activate && python3 ghost_predator/dashboard.py
```

Dashboard je display-only — bere `ghost_predator/state.json` (zapisuje ga PM2 proces vsako sekundo) in DB v read-only načinu. Ne spawna procesov.

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
