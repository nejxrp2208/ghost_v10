# GHOST PREDATOR

Independent **last-window latency snipe** on Polymarket BTC/ETH up-down markets.
Built to run on a low-latency VPS. Fully standalone — nothing shared with GHOST_LATTICE.

## The edge
In the final seconds of a 5m/15m up/down market, the underlying has usually
*already decided* the outcome — but Polymarket's book is often still offering the
winning side below its true probability. We compute the leader from our **own
Binance feed** and snipe PM's stale quote before it reprices.

No copying, no feed-race: we predict the outcome from the underlying (basically
settled) and hit a stale price. Being a few ms late doesn't change the
prediction — speed only governs whether you grab the quote before it's pulled.
That's what the VPS + real-time feed are for.

## How it works
1. **Binance WSS feed** — live BTC/ETH price (`binance_feed`).
2. **Polymarket WSS book feed** — real-time order book via the CLOB market
   channel (`book_feed`). The ask is read from memory at **0ms** (~470 updates/s)
   instead of a ~15ms REST poll, so it never misses depth that flashes between
   polls. Auto-falls back to REST if the feed drops — can only improve fills.
3. **Discovery** — every 20s finds each active BTC/ETH window (5m + 15m) by its
   exact slug (`coin-updown-dur-startTs`) and backfills the true open price from
   Binance klines (`discovery`).
4. **Snipe loop** — every `BOOK_POLL_MS` (10ms): for any in-window market, compute
   the leader (current vs open), and if it's ahead by `≥MIN_MOVE_BPS` with its ask
   in `MIN_ASK..MAX_ASK`, fire (`snipe_loop` → `fire`).
5. **Dynamic sizing** — the order is sized to the depth at the ask
   (`min(BASE_SIZE, depth)`), so the FOK fills in full (~100% fill) and captures
   thin high-win books at small size instead of skipping them.
6. **Resolver** (`resolver.py`) — settles positions via Polymarket's on-chain
   **token-level** winner (never label). Zero false wins/losses.

## Setup
```bash
cd ~/Desktop/ghost_predator
cp .env.example .env          # paper by default, no creds needed
python3 ghost_predator.py     # the snipe engine
python3 resolver.py           # second tab — settles closed positions
python3 alarmghost.py         # optional — Telegram health alarm
```
Needs `aiohttp`. Live mode also needs `py-clob-client-v2` + creds.

## Config (.env) — these are the exact live settings
| Key | Value | Meaning |
|---|---|---|
| `PAPER_TRADE` | false | Live mode. true = paper (no creds needed). |
| `COINS` / `DURATIONS` | BTC,ETH / 5m,15m | What to trade. |
| `SNIPE_WINDOW_SECS` / `_15M` | 17 / 30 | Start sniping at T-17s (5m) / T-30s (15m). |
| `MIN_FILL_SECS` | 2 | Never fire with less than this left. |
| `BTC_MIN_MOVE_BPS` | 1.0 | BTC: leader must be ≥1.0bps ahead (data: 86% WR even at 1.0-1.5bps). |
| `ETH_MIN_MOVE_BPS` | 1.3 | ETH: higher threshold (data: borderline 1.0-1.3bps trades lossy). |
| `ETH_SKIP_15M` | true | Skip ETH 15m entirely (data: 54% WR = coinflip, Chainlink disagree). |
| `MIN_ASK` / `MAX_ASK` | 0.65 / 0.85 | Global ask band — favorites with profit room. |
| `BTC_BASE_SIZE` / `BTC_MAX_PER_MARKET` | 12 / 12 | BTC stake (data: 82% WR → bigger bet). |
| `ETH_BASE_SIZE` / `ETH_MAX_PER_MARKET` | 7 / 7 | ETH stake (data: lower WR → smaller bet). |
| `WALK_FILL` | true | Walk ask ladder to fill full stake; skip if book too thin. |
| `FILL_FLOOR` | 0.50 | Safety floor on blended fill price (prevents stale WSS dragging avg down). |
| `MIN_STAKE` | 1.0 | Skip if depth-matched size below PM's min order. |
| `USE_WSS_BOOK` | true | Real-time book via WSS (REST fallback). |
| `BOOK_POLL_MS` | 10 | Snipe-loop sweep cadence (free reads via WSS). |
| `DAILY_LOSS_LIMIT` / `MAX_LOSS_STREAK` | 150 / 5 | Account-survival halts. |

## Settlement & fees — read this before going live
- **Polymarket settles these markets on Chainlink's ETH/USD & BTC/USD data
  streams**, *not* Binance. On decisive moves both agree; on near-flat closes
  (sub ~2bps) they can disagree, and PM resolves ties (`≥ open`) as **Up**. So a
  Binance "Down" lead can lose on Chainlink — this is the dominant near-coinflip
  loss mode.
- **Taker fees** apply (5m/15m/hourly crypto, since Jan 2026):
  `fee = shares × 0.07 × price × (1-price)`. Largest at 0.50, small at the edges
  (favorites). `live_ledger.py` models this — watch the **fee-adjusted net**.

## Findings (from live data, n=57 resolved trades)
- **BTC 82% WR, ETH 64% WR** overall. After filters (ETH_SKIP_15M + ETH_MIN_MOVE_BPS=1.3): ETH ~78%.
- **ETH 15m = coinflip (54% WR)** — Chainlink ETH/USD disagrees with Binance on 15m resolution. Skip entirely.
- **ETH 5m = 78% WR** — solid, keep with slightly higher move threshold (1.3bps).
- **Entry price bands matter:** BTC 0.75–0.85 = 90%+ WR. ETH 0.80–0.85 = 83% WR; ETH 0.75–0.80 = 50% (coinflip).
- **Decisive BTC moves (3+bps) ≠ higher WR** — at 5+bps BTC is 50% (book already repriced, Chainlink agrees). Don't over-filter on move size.
- **WALK_FILL** ($X-or-skip) is the correct execution model for live — thin fills don't survive slippage+fees.
- **FILL_FLOOR=0.50** prevents stale WSS book levels from dragging blended fill below the viable entry zone.
- Track `pre_window_move_bps` and `price_trend` columns in DB — wick pattern analysis after 30-50 trades.

## Going live (only after the fee-adjusted ledger is convincingly positive)
1. Run on a VPS in **AWS eu-west-2 (London)** — Polymarket's CLOB is hosted there;
   colocation minimizes order-placement latency (the half WSS doesn't fix).
2. Fill `PRIVATE_KEY`, `POLYMARKET_PROXY_ADDRESS`, `CLOB_API_KEY/SECRET/PASSPHRASE`.
3. `pip install py-clob-client-v2`, set `PAPER_TRADE=false`, start tiny.

Live orders are FOK at `price=ask` (capped — no slippage walk-up); unfilled orders are skipped.

## Files
- `ghost_predator.py` — snipe engine (Binance feed + WSS book + discovery + dynamic-sized snipe loop)
- `resolver.py` — settles positions via on-chain token winner
- `alarmghost.py` — standalone Telegram health alarm (loss streak / cold run / daily loss)
- `live_ledger.py` — fee-adjusted live-fillable ledger (your real-money number)
- `audit_trades.py` — re-checks every trade against PM's token winner (no false W/L)
- `dashboard.py` — rich terminal dashboard
- `snipe_backtest.py` — the backtest that proved the edge
- `ghost_predator.db` — SQLite: `positions` (every snipe + depth/fill/move/secs columns)

---

## Changelog (v10 deploy)

### Live mode fixes (2026-05-28)
- `live_client()` fixed: `signature_type=1` → `signature_type=3` (POLY_1271 — deposit wallet flow required by CLOB v2)
- `creds=ApiCreds(...)` now passed directly to `ClobClient` constructor instead of `set_api_creds()` — required for correct L1/L2 auth pairing
- `POLYMARKET_PROXY_ADDRESS` in `.env` must be the proxy wallet shown in Polymarket profile (not the EOA)
- `ledger_report.py` venv path fixed: `/root/gp_venv/bin/python` → `/root/ghost_v10/venv/bin/python3`

### Wick analysis logging (2026-05-29)
Two new columns added to `positions` table to detect the **wick loss pattern**:

| Column | Type | Meaning |
|---|---|---|
| `pre_window_move_bps` | REAL | Move from open at ~T-25s (5m) or ~T-40s (15m) — price state just before the snipe window opened. Captures the wick height. |
| `price_trend` | REAL | bps change over last ~5s at fire time, relative to leader direction. Positive = moving away from open (good momentum). Negative = reverting toward open (wick warning). |

**What to look for after 30-50 trades:**
```sql
SELECT id, coin, outcome,
       pre_window_move_bps,
       move_bps,
       round(cast(move_bps as float)/pre_window_move_bps, 2) as reversal_ratio,
       price_trend,
       status, pnl
FROM positions WHERE status IN ('won','lost') ORDER BY id;
```

**Loss patterns:**
- `reversal_ratio` low (< 0.3) + `price_trend` negative → wick/late reversal (price was far, came back)
- `move_bps` < 2 → near-flat close, Chainlink vs Binance disagreement risk
- `price_trend` negative + `move_bps` still large → active reversal in progress

**Next step:** once enough data is collected, add `REVERSAL_FACTOR` config to skip trades where `move_bps < pre_window_move_bps × factor`.

### Edge-return monitoring (2026-05-30)
Added two **standalone read-only collectors** to detect when PM mispricing returns. Neither risks money; both sit alongside the bot ("ambush with the safety on").

| Tool | File | DB | Run mode |
|---|---|---|---|
| **Reversal Radar** | `divergence_scan.py` | `divergence.db` | PM2 process `ghost-predator-radar` (forever loop, ~2s tick) |
| **Hour Scanner** | `hour_scan.py` | `hour_scan.db` | CLI (`backfill 30` once, then hourly cron `append`) |

**Reversal Radar:** snapshots Polymarket ask vs Binance spot at ~T-11s for every BTC/ETH 5m + 15m window. Flags `diverged=1` when PM already prices the OTHER side as favorite (whale footprint). After close, flags `reversed=1` if T-11 spot leader lost. Key test in `report()`: does reversal rate among DIVERGED windows exceed reversal rate in AGREED windows? If yes, divergence is a defensive predictive signal.

**Hour Scanner:** backfills 30 days of Binance 1m klines (~22k windows), measures whether the late-window leader (price ~1 min before close) predicted the actual close, broken down by UTC hour. Initial finding: 86.8% overall accuracy, 92.6% on ≥2bps moves, **flat across all 24 hours (no time-of-day edge)**. Standing job: watch `edge` column (acc≥2 minus 75%) — when it sustains positive across multiple hours with n≥40, PM mispricing has returned. Watch this alongside paper-watch shadow picks in the trading bot.

**Team framing:** *"bps has not gone above +2, that's where our edge lives and our 92% win ratio... don't lose money — we have the edge, now we find the time to use it when actual mispricing happens."* The bot is in edge gate OFF; these radars tell us when to flip it ON.

### Data-driven filters (2026-05-29, n=57 live trades)
Based on analysis of both DB files:
- `ETH_SKIP_15M=true` — ETH 15m WR=54% (coinflip), Chainlink/Binance disagree on 15m ETH resolution
- `ETH_MIN_MOVE_BPS=1.3` — borderline ETH moves (1.0–1.3bps) are lossy; BTC stays at 1.0 (86% WR even at 1.0–1.5bps)
- `BTC_BASE_SIZE=12.0` / `ETH_BASE_SIZE=7.0` — per-coin Kelly-proportional sizing (BTC 82% WR vs ETH 64% WR)
- `BTC_MAX_PER_MARKET=12.0` / `ETH_MAX_PER_MARKET=7.0` — match per-coin base size
- `FILL_FLOOR=0.50` — safety floor on blended walk-fill entry (prevents stale WSS dragging avg to 0.46)
- All three paths updated for per-coin sizing: `snipe_loop` (target), `presign_loop` (pre-signed amount), `fire()` inline fallback
