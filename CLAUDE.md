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
| `zugu-scanner` | `zugu_bot/scanner.py` | ZUGU v3.5: FOLLOW LEADER strategy (last 5-15s, dynamic bias, 5 shares, ask 0.30-0.75) |
| `zugu-resolver` | `zugu_bot/crypto_ghost_resolver.py` | ZUGU resolver (Polymarket/Chainlink settlement) |
| `zugu-redeemer` | `zugu_bot/crypto_ghost_redeemer.py` | ZUGU on-chain winnings redeemer |
| `ghost-predator` | `ghost_predator/ghost_predator.py` | GHOST PREDATOR: last-window latency snipe (BTC/ETH 5m+15m, T-12s) |
| `ghost-predator-resolver` | `ghost_predator/resolver.py` | GHOST PREDATOR resolver (on-chain token-level winner, separate subsystem) |
| `ghost-predator-alarm` | `ghost_predator/alarmghost.py` | GHOST PREDATOR Telegram health alarm (loss/cold/daily-loss triggers) |
| `ghost-predator-radar` | `ghost_predator/divergence_scan.py` | GHOST PREDATOR Reversal Radar — read-only T-11s PM-vs-spot divergence collector |
| `ghost-predator-firstmover` | `ghost_predator/firstmover.py` | First-mover edge detector — rolling 20-window WR-vs-PM-ask, signals GO/WAIT/OUT |

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

**WORLD EVENTS — separate system in `world_event_bot/` subfolder:**
- Location: `/root/ghost_v10/world_event_bot/`
- Own `.env`: `/root/ghost_v10/world_event_bot/.env` (gitignored; copy from `.env.example`)
- Own DB: `world_event_bot/polymarket_event_bot.db`
- Dashboard (display-only): `python3 world_event_bot/tui.py`
- Strategy: **NO-side bias on Polymarket world events** — buys NO when YES is overpriced (0.30-0.55 band), detects momentum/extreme crash patterns + crypto binary
- Signal source: `our_engine.py` (rule-based, no API key needed) — EdgeFinder API optional (paid)
- Sizing: fractional Kelly (0.5×), fee-adjusted EV gate, reserve 30%, cluster cap 25%
- Patterns: `extreme_crash` (YES -30% in 7d, 62% conf), `momentum_crash` (YES -15% in 7d, 58%), `structural` (55%), `crypto_binary` (60%)
- Note: crash detection needs 7 days of snapshots to activate — first week mostly structural signals

**GHOST PREDATOR — separate system in `ghost_predator/` subfolder:**
- Location: `/root/ghost_v10/ghost_predator/`
- Own `.env`: `/root/ghost_v10/ghost_predator/.env` (separate from v10 and ghost_lattice — gitignored; copy from `.env.example`)
- Own DB: `ghost_predator/ghost_predator.db` (single DB for paper+live, distinguished by `mode` column)
- Dashboard (display-only, PM2 manages processes): `python3 ghost_predator/dashboard.py`
- Strategy: **Last-window latency snipe** — in the final ~17s (5m) / ~30s (15m) of a BTC/ETH up-down market, compute leader from our own Binance feed and snipe PM's stale ask before it reprices
- Entry band: `MIN_ASK=0.65`, `MAX_ASK=0.85` (favorites with profit room, no coinflips)
- Move gate: `BTC_MIN_MOVE_BPS=1.0`, `ETH_MIN_MOVE_BPS=1.3` (per-coin, data-driven from 57 live trades)
- ETH 15m skipped: `ETH_SKIP_15M=true` (data: 54% WR = coinflip, Chainlink disagree)
- Sizing: `WALK_FILL=true` + per-coin: `BTC_BASE_SIZE=12`, `ETH_BASE_SIZE=7` (data: BTC 82% WR vs ETH 64%)
- Safety: `FILL_FLOOR=0.50` (prevents stale WSS levels dragging blended fill below viable entry)
- Live auth: `signature_type=3` (POLY_1271 deposit wallet flow), `creds=ApiCreds(...)` in constructor
- Realtime: WSS Polymarket book channel (REST fallback), `BOOK_POLL_MS=10` snipe cadence
- Guardrails: `DAILY_LOSS_LIMIT=150`, `MAX_LOSS_STREAK=5` — auto-halts trading
- See `ghost_predator/FILTERS.md` and `ghost_predator/LEARNINGS.md` for full filter chain + tuning history

**ZUGU v3.5 — separate system in `zugu_bot/` subfolder:**
- Location: `/root/ghost_v10/zugu_bot/`
- Own `.env`: `/root/ghost_v10/zugu_bot/.env` (gitignored; copy from `.env.example`)
- Own DB: `zugu_bot/crypto_ghost_PAPER.db` (paper) / `crypto_ghost.db` (live)
- Dashboard (display-only): `python3 zugu_bot/crypto_ghost_dashboard.py`
- Strategy: **FOLLOW LEADER** (late-candle momentum) — in last 5-15s of 5min candle, if Binance price leads PTB by ≥0.01%, buy that side at ask
- Entry: `FL_MIN_LEAD_PCT=0.0001`, ask band `0.30-0.75`, 5 shares fixed, max 2 open trades
- v3 production data (1113 trades / 21 days, 2026-05-10 → 2026-05-31): **62.1% WR, +$205.55 PnL (+$9.79/day avg)**; BTC 59.1% WR, ETH 65.8% WR; UP/DOWN imbalance 1097/16 (motivated v3.5 dynamic-bias)
- Best hours (UTC): 21:00 (75.7%), 13:00 (73.2%), 12:00 (72.2%), 22:00 (71.1%) — worst: 00:00, 02:00, 17:00
- Sweet entry band: **0.65-0.70 = 75% WR, +$68 PnL**; avoid 0.30-0.40 (31% WR) and 0.70-0.75 (70% WR but -$17)
- **v3 → v3.5 changes (2026-05-31):**
  - WebSocket event-driven entry (~1-2ms vs ~500ms polling) — new `entry_watcher_loop`
  - Dynamic DOWN bias: rolling 60s spread tracker (Binance ~15-19bps over PTB due to Chainlink lag), auto-corrects after 30 samples via `_get_dynamic_bias()`
  - PM oracle check disabled for DOWN signals (oracle has same Chainlink lag)
  - DB now stores `binance_price` + `price_to_beat` for offset calibration analysis
  - `FL_DOWN_BIAS_OFFSET=0` until ~100 v3.5 trades collected to determine real offset
  - `QUIET_MODE` configurable via .env, `FL_RESET_ON_START=false` (preserves DB)
- Assets: BTC + ETH only, `FL_PAPER_ONLY=true` by default
- Tools: `monitor.py` (real-time stats), `full_analysis.py` + `velocity_volume_analysis.py` (post-hoc analysis)

**Tech:** Python async (`asyncio` + `aiohttp`), SQLite, Polygon/Chainlink, Polymarket CLOB + Gamma APIs.

**DBs (v10):** `crypto_ghost_PAPER.db` (trades), `scanner.db` (book/summary), `marketghost.db` (market snapshots).
**DBs (GHOST_LATTICE):** `ghost_lattice/GHOST_LATTICE_PAPER.db` (paper trades), `ghost_lattice/GHOST_LATTICE.db` (live trades).
**DBs (GHOST PREDATOR):** `ghost_predator/ghost_predator.db` (paper + live, distinguished by `mode` column).
**DBs (ZUGU v3.5):** `zugu_bot/crypto_ghost_PAPER.db` (paper) / `zugu_bot/crypto_ghost.db` (live).

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

# ── ZUGU v3.5 ─────────────────────────────────────────────────
pm2 restart zugu-scanner             # FOLLOW LEADER scanner (event-driven entry, dynamic bias)
pm2 restart zugu-resolver            # ZUGU resolver
pm2 restart zugu-redeemer            # ZUGU on-chain winnings redeemer

# ── GHOST PREDATOR ────────────────────────────────────────────
pm2 restart ghost-predator           # main snipe engine (Binance feed + WSS book + snipe loop)
pm2 restart ghost-predator-resolver  # on-chain token-level settlement
pm2 restart ghost-predator-alarm     # Telegram health alarm (loss/cold/daily-loss)
pm2 restart ghost-predator-radar     # Reversal Radar (T-11s PM-vs-spot divergence collector)
pm2 restart ghost-predator-firstmover # First-mover edge detector (gates real fires on GO/OUT)

# ── WORLD EVENTS ──────────────────────────────────────────────
pm2 restart world-events             # background engine (market scan + signal eval + paper trade)
```

### First-time deploy (zugu_bot v3.5)

```bash
cd /root/ghost_v10 && git pull
/root/ghost_v10/venv/bin/pip install -r zugu_bot/requirements.txt
cp zugu_bot/.env.example zugu_bot/.env
# Edit zugu_bot/.env to fill PRIVATE_KEY + CLOB credentials (for live mode)
# Defaults: PAPER_TRADE=true, FL_PAPER_ONLY=true, FL_DOWN_BIAS_OFFSET=0

pm2 start zugu_bot/scanner.py                 --name zugu-scanner   --interpreter /root/ghost_v10/venv/bin/python3
pm2 start zugu_bot/crypto_ghost_resolver.py   --name zugu-resolver  --interpreter /root/ghost_v10/venv/bin/python3
pm2 start zugu_bot/crypto_ghost_redeemer.py   --name zugu-redeemer  --interpreter /root/ghost_v10/venv/bin/python3
pm2 save
```

### ZUGU v3.5 Dashboard & analysis

```bash
# Dashboard (read-only TUI)
cd /root/ghost_v10 && source venv/bin/activate && python3 zugu_bot/crypto_ghost_dashboard.py

# Real-time monitor (lightweight stats)
python3 zugu_bot/monitor.py

# Full historical analysis (after 100+ trades)
python3 zugu_bot/full_analysis.py

# Velocity/volume bucket analysis
python3 zugu_bot/velocity_volume_analysis.py
```

### First-time deploy (world_event_bot)

```bash
cd /root/ghost_v10 && git pull
/root/ghost_v10/venv/bin/pip install -r world_event_bot/requirements.txt
cp world_event_bot/.env.example world_event_bot/.env
# EDGEFINDER_API_KEY pusti prazen — our_engine ne rabi ključa
pm2 start world_event_bot/scheduler.py --name world-events --interpreter /root/ghost_v10/venv/bin/python3
pm2 save
```

### World Events Dashboard

```bash
cd /root/ghost_v10 && source venv/bin/activate && python3 world_event_bot/tui.py
```

Dashboard je display-only — bere iz `polymarket_event_bot.db` v read-only načinu. Ne spawna procesov. Ctrl+C za izhod.

### First-time deploy (ghost_predator)

```bash
cd /root/ghost_v10 && git pull
cp ghost_predator/.env.example ghost_predator/.env   # paper defaults work as-is; fill Telegram + (later) live creds
# venv check: aiohttp + rich must be installed in /root/ghost_v10/venv
/root/ghost_v10/venv/bin/pip install aiohttp rich
pm2 start ghost_predator/ghost_predator.py --name ghost-predator           --interpreter /root/ghost_v10/venv/bin/python3
pm2 start ghost_predator/resolver.py       --name ghost-predator-resolver  --interpreter /root/ghost_v10/venv/bin/python3
pm2 start ghost_predator/alarmghost.py     --name ghost-predator-alarm     --interpreter /root/ghost_v10/venv/bin/python3
pm2 start ghost_predator/divergence_scan.py --name ghost-predator-radar    --interpreter /root/ghost_v10/venv/bin/python3
pm2 save
```

### First-time deploy (Edge-return monitoring — hour_scan + divergence_scan)

These are **read-only collectors** — no money at risk. They sit alongside the bot and watch for the day the PM mispricing returns. Stdlib only (no pip install needed).

**Hour Scanner — backfill + hourly cron** (one-time setup):
```bash
# 1) Backfill 30 days of historical data (~22k windows, takes ~10-15 min HTTP time)
/root/ghost_v10/venv/bin/python3 /root/ghost_v10/ghost_predator/hour_scan.py backfill 30

# 2) Install hourly cron (append last 2 days, idempotent — keeps dataset growing)
( crontab -l 2>/dev/null; echo "5 * * * * /root/ghost_v10/venv/bin/python3 /root/ghost_v10/ghost_predator/hour_scan.py append >> /tmp/hour_scan.log 2>&1" ) | crontab -

# 3) Verify cron installed
crontab -l | grep hour_scan
```

**Reversal Radar — PM2** (included in first-time deploy above as `ghost-predator-radar`).
Snapshots PM-vs-spot at ~T-11s for every BTC/ETH 5m + 15m window, resolves after close. Own DB `ghost_predator/divergence.db`.

**First-Mover Detector — PM2 + .env switch:**
```bash
# 1) Add to .env (gates real fires when ENABLED=true)
echo "FIRSTMOVER_ENABLED=true" >> /root/ghost_v10/ghost_predator/.env

# 2) Start PM2 process (reads divergence.db, writes firstmover_state.json every 60s)
pm2 start /root/ghost_v10/ghost_predator/firstmover.py --name ghost-predator-firstmover --interpreter /root/ghost_v10/venv/bin/python3 && pm2 save

# 3) Restart predator to pick up new env
pm2 restart ghost-predator

# 4) Verify
pm2 logs ghost-predator-firstmover --lines 10 --nostream
/root/ghost_v10/venv/bin/python3 /root/ghost_v10/ghost_predator/firstmover.py report
```

When ENABLED=true, predator only fires real money when signal is GO or WARMUP (default-safe). WAIT/OUT routes to shadow paper.

### GHOST_LATTICE Dashboard

```bash
cd /root/ghost_v10 && source venv/bin/activate && python3 ghost_lattice/GHOST_LATTICE_dashboard.py
```

Dashboard je display-only — PM2 upravlja procese. Ne zažene dvojnikov.

### GHOST PREDATOR Dashboard

```bash
cd /root/ghost_v10 && source venv/bin/activate && python3 ghost_predator/dashboard.py
```

Ali od koderkoli z eno vrstico:
```bash
ssh root@70.34.204.152 'cd /root/ghost_v10 && source venv/bin/activate && python3 ghost_predator/dashboard.py'
```

Dashboard je display-only — bere `ghost_predator/state.json` (zapisuje ga `ghost-predator` PM2 proces vsako sekundo) in DB v read-only načinu. Ne spawna procesov. Ctrl+C ga zapre, PM2 procesi tečejo dalje.

### GHOST PREDATOR — Operativni ukazi (vse, kar rabiš)

#### Pogled na status / loge

```bash
pm2 list | grep ghost-predator                          # 3 vrstice (scanner, resolver, alarm)
pm2 logs ghost-predator --lines 50 --nostream           # zadnjih 50 vrstic (snapshot)
pm2 logs ghost-predator                                 # živo sledenje (Ctrl+C izstop)
pm2 logs ghost-predator --err --lines 100 --nostream    # samo napake
pm2 logs ghost-predator-resolver --lines 30 --nostream  # resolver log
pm2 logs ghost-predator-alarm --lines 30 --nostream     # alarm log
pm2 describe ghost-predator                             # podroben info (pot, PID, uptime, restarts, memory)
pm2 monit                                               # interaktivni live monitor vseh procesov
```

#### Restart / stop / start

```bash
# Restart enega procesa
pm2 restart ghost-predator

# Restart vseh 3 naenkrat (rabiš to po vsaki spremembi .env ali kode)
pm2 restart ghost-predator ghost-predator-resolver ghost-predator-alarm

# Ustavi proces (ostane v PM2 listi, status=stopped)
pm2 stop ghost-predator

# Zaženi ustavljen proces nazaj
pm2 start ghost-predator

# Popolnoma odstrani iz PM2 (rabiš First-time deploy za nazaj)
pm2 delete ghost-predator
```

#### Urejanje .env (in uveljavitev sprememb)

`.env` je v `.gitignore` — obstaja SAMO na VPS-u, ne v gitu. Po vsaki spremembi je treba restartirati procese, ker `.env` berejo samo ob zagonu.

```bash
nano /root/ghost_v10/ghost_predator/.env
# uredi karkoli, shrani (Ctrl+O, Enter, Ctrl+X)
pm2 restart ghost-predator ghost-predator-resolver ghost-predator-alarm
# preveri da ni napake po restartu
pm2 logs ghost-predator --lines 20 --nostream
```

#### Telegram alerti — setup

Telegram je trenutno disabled (`no Telegram creds in .env`). Da vključiš:

```bash
nano /root/ghost_v10/ghost_predator/.env
# najdi vrstici na dnu:
#   TELEGRAM_BOT_TOKEN=
#   TELEGRAM_CHAT_ID=
# vnesi:
#   TELEGRAM_BOT_TOKEN=<token od BotFather>
#   TELEGRAM_CHAT_ID=<tvoj chat id>
pm2 restart ghost-predator ghost-predator-resolver ghost-predator-alarm
# v alarm logu ne bi smel več videti "alerts disabled"
pm2 logs ghost-predator-alarm --lines 5 --nostream
```

Po tem boš dobil Telegram alerte: 🎯 ob vsakem fire, ✅/❌ ob resolve, in 🧊/🥶/🛑 od alarmghosta.

#### Deploy spremembe kode (lokalno → GitHub → VPS)

```bash
# 1) LOKALNO (Windows): uredi datoteko v c:/Users/nejcj/Downloads/GhostScanner_v10_full/ghost_predator/
cd c:/Users/nejcj/Downloads/GhostScanner_v10_full
git add ghost_predator/<datoteka>
git commit -m "ghost-predator: <kaj se je spremenilo>"
git push origin master

# 2) VPS: povleci in restartiraj prizadeti proces
cd /root/ghost_v10 && git pull && pm2 restart ghost-predator
# (če si spremenil resolver.py: pm2 restart ghost-predator-resolver)
# (če si spremenil alarmghost.py: pm2 restart ghost-predator-alarm)
# (če nisi siguren: pm2 restart ghost-predator ghost-predator-resolver ghost-predator-alarm)
```

#### Analiza / poročila (CLI orodja, enkratni zagon na VPS)

```bash
cd /root/ghost_v10 && source venv/bin/activate

# Fee-adjusted live-fillable ledger — "pravi" denarni rezultat z modelirano taker fee
python3 ghost_predator/live_ledger.py ghost_predator/ghost_predator.db

# Re-audit vseh trade-ov proti Polymarket on-chain winnerju (preveri da ni lažnih W/L)
python3 ghost_predator/audit_trades.py ghost_predator/ghost_predator.db predator

# Dnevno ledger poročilo → Telegram (lahko poženeš ročno; cron tudi to lahko kliče)
python3 ghost_predator/ledger_report.py

# Backtest (rabi lag_data.db)
python3 ghost_predator/snipe_backtest.py

# Analiza po (coin, duration)
python3 ghost_predator/dur_compare.py

# Frekvenca fire-ov + win rate sweep
python3 ghost_predator/freq_check.py
python3 ghost_predator/sweep.py
python3 ghost_predator/watch_check.py
```

#### Edge-return radarji (READ-ONLY — preverka kdaj se PM mispricing vrne)

```bash
# Reversal Radar — ali PM-vs-spot divergence pri T-11s napove reversal?
python3 ghost_predator/divergence_scan.py report

# Hour Scanner — per-hour late-leader accuracy + edge vs PM cena 0.75
python3 ghost_predator/hour_scan.py report

# First-Mover Detector — current GO/WAIT/OUT signal + rolling edge stats
python3 ghost_predator/firstmover.py report
```

**Kdaj se edge vrne (kar opazuješ v hour_scan report):**
- `acc≥2` (accuracy na ≥2bps moveih) sustained nad 90% → signal je močan
- `edge` (acc≥2 − 75%) sustained pozitivna → PM ne ceni efficiently
- **OPOMBA:** zahteva n≥40 per hour preden zaupaš; ne reagiraj na enkratno uro

**Kdaj reversal opozori (kar opazuješ v divergence_scan report):**
- `reversed` rate v DIVERGED oknih bistveno višji kot v AGREED oknih → divergence je defensive signal (NE stavi na spot leaderja takrat)

#### Reset DB (počisti vse trade-e in začni znova)

⚠️ **DESTRUKTIVNO** — trajno briše vse zgodovinske trade-e. Naredi samo če zares hočeš čist start.

```bash
pm2 stop ghost-predator ghost-predator-resolver ghost-predator-alarm
rm /root/ghost_v10/ghost_predator/ghost_predator.db
rm /root/ghost_v10/ghost_predator/ghost_predator.db-shm 2>/dev/null
rm /root/ghost_v10/ghost_predator/ghost_predator.db-wal 2>/dev/null
rm /root/ghost_v10/ghost_predator/state.json 2>/dev/null
rm /root/ghost_v10/ghost_predator/alarmghost_state.json 2>/dev/null
pm2 start ghost-predator ghost-predator-resolver ghost-predator-alarm
# DB se avtomatsko ustvari ob prvem fire-u
```

#### PAPER → LIVE (NIKOLI brez explicit ukaza lastnika)

⚠️ Pred LIVE: fee-adjusted ledger (`live_ledger.py`) MORA biti prepričljivo pozitiven na smiselnem vzorcu. **Paper mode je sacred** — ne preklapljaj sam.

```bash
# 1) inštaliraj live SDK (samo prvič)
/root/ghost_v10/venv/bin/pip install py-clob-client-v2

# 2) uredi .env
nano /root/ghost_v10/ghost_predator/.env
#   PAPER_TRADE=false
#   PRIVATE_KEY=<tvoj key>
#   POLYMARKET_PROXY_ADDRESS=<tvoj proxy addr>
#   CLOB_API_KEY=<...>
#   CLOB_SECRET=<...>
#   CLOB_PASSPHRASE=<...>

# 3) restart in nadziraj prve order-je
pm2 restart ghost-predator ghost-predator-resolver ghost-predator-alarm
pm2 logs ghost-predator       # živo sledi prvim fire-om za [live-err] ali [miss]
```

Banner mora pokazati `*** LIVE ***` namesto `PAPER`. Začni z majhnim BASE_SIZE.

#### Pomembne poti na VPS

| Kaj | Pot |
|---|---|
| Koda | `/root/ghost_v10/ghost_predator/` |
| `.env` (gitignored) | `/root/ghost_v10/ghost_predator/.env` |
| DB (trading) | `/root/ghost_v10/ghost_predator/ghost_predator.db` |
| DB (Reversal Radar) | `/root/ghost_v10/ghost_predator/divergence.db` |
| DB (Hour Scanner) | `/root/ghost_v10/ghost_predator/hour_scan.db` |
| First-mover state | `/root/ghost_v10/ghost_predator/firstmover_state.json` |
| Dashboard state | `/root/ghost_v10/ghost_predator/state.json` |
| Alarm state | `/root/ghost_v10/ghost_predator/alarmghost_state.json` |
| PM2 stdout log | `/root/.pm2/logs/ghost-predator-out.log` |
| PM2 stderr log | `/root/.pm2/logs/ghost-predator-error.log` |
| Python venv | `/root/ghost_v10/venv/bin/python3` |
| SSH | `ssh root@70.34.204.152` |

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
