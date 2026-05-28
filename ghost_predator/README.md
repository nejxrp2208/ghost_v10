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
| `PAPER_TRADE` | true | Simulated fills. False = live (needs VPS + creds). |
| `COINS` / `DURATIONS` | BTC,ETH / 5m,15m | What to trade. |
| `SNIPE_WINDOW_SECS` / `_15M` | 12 / 30 | Start sniping at T-12s (5m) / T-30s (15m). |
| `MIN_FILL_SECS` | 2 | Never fire with less than this left. |
| `MIN_MOVE_BPS` | 1.0 | Leader must be ≥0.01% ahead. **Don't raise** (see Findings). |
| `MIN_ASK` / `MAX_ASK` | 0.50 / 0.85 | Buy band — a favorite with profit room. |
| `BASE_SIZE` / `MAX_PER_MARKET` | 30 / 30 | Stake **cap** (actual size = depth-matched). |
| `DYNAMIC_SIZE` | true | Size to book depth for ~100% fill. |
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

## Findings (honest, from live paper data)
- Track outcomes with `audit_trades.py` (token-verified) and the real money
  number with `live_ledger.py` (fillable trades, fee-adjusted). The raw paper PnL
  overstates reality because it assumes fills that thin books wouldn't give live.
- **Win rate is ~the same across move sizes**, so raising `MIN_MOVE_BPS` removes
  profitable small-move wins without removing losses — verified, leave it at 1.0.
- **Dynamic sizing flipped the modeled fee-adjusted net positive** by capturing
  thin high-win books at small size — but live capture depends on winning the
  taker race for thin depth (hence the WSS + colocation focus).
- The live-realistic edge is **thin** — paper-test the fee-adjusted ledger across
  a meaningful sample before funding real money.

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
