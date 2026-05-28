# Ghost Predator — Complete Filter / Gate Reference

Every condition that decides whether a trade fires or is skipped, in the order
it is applied. **A trade must clear ALL gates to fire.** Threshold values are
read from `.env` at startup (the code holds different fallback defaults — `.env`
overrides them). Values below are the active config as of 2026-05-27.

---

## Stage 1 — Market discovery (what gets tracked)
A market is only watched if it passes all of these:

| Filter | Active value | Effect |
|---|---|---|
| Coin | `COINS=BTC,ETH` | Only BTC & ETH (code supports SOL+ but they're off) |
| Duration | `DURATIONS=5m,15m` | Only 5-minute and 15-minute up/down markets |
| Market exists | gamma API returns slug | Skips windows not yet created |
| Has tokens/outcomes | `clobTokenIds` + `outcomes` present | Skips malformed markets |
| Not closed | `close_ts > now + 1s` | Skips already-expired windows |
| Lookahead | current + next 2 windows | Tracks each window early, full lead time |
| Prune | drop markets closed > 5s ago | Keeps tracking state clean |
| Refresh cadence | `DISCOVERY_SECS=20` | Re-scans the market list every 20s |

## Stage 2 — Account-survival guardrails (these HALT *all* trading)
Checked every loop tick. If any trips, the bot stops firing entirely until cleared.

| Guardrail | Active value | Trips when |
|---|---|---|
| Daily loss limit | `DAILY_LOSS_LIMIT=150` | today's PnL ≤ −$150 → halt for the day |
| Loss streak | `MAX_LOSS_STREAK=5` | 5 losses in a row → halt (edge may be broken) |
| Balance floor | `MIN_BALANCE=0` (floors at 1 stake) | balance < one stake ($30) → halt (can't fund a bet) |

## Stage 3 — Per-trade entry gates (the snipe decision, in exact order)
Evaluated for each tracked market, every tick. First failure → skip.

1. **In-window timing** — `MIN_FILL_SECS ≤ secs_left ≤ snipe_window`.
   `MIN_FILL_SECS=2`. Window = `SNIPE_WINDOW_SECS=30` for 5m, `SNIPE_WINDOW_15M=30`
   for 15m. Outside the window → skip.
2. **One fire per token** — if already sniped this window → skip (no double-firing).
3. **Prices present** — open price & live Binance price both exist → else skip.
4. **Stale-feed guard** — `PRICE_MAX_AGE=3.0s`; if the Binance quote is older than
   this → skip (never trade a frozen feed).
5. **Leader check** — this token must be the side currently winning per Binance
   (`cur ≥ open` → Up leads). Only the leading side is bought; lagging side → skip.
6. **Move gate** — `MIN_MOVE_BPS=1.0`; |move| must be ≥ 1.0 bps (0.01%).
   Smaller = "too close to call" → skip.
7. **Ask band** — ask must exist and be `MIN_ASK ≤ ask ≤ MAX_ASK` = **0.65–0.85**.
   Below 0.65 (coinflip, no edge) or above 0.85 (no profit room) → skip.
8. **Exposure cap** — `MAX_PER_MARKET=30`; if existing exposure in this market
   ≥ $30 → skip.
9. **Fill gate (`WALK_FILL=true` → "$30-or-skip")** — walk the ask ladder within
   `MAX_ASK`; fire ONLY if the in-band book supplies the full stake
   (`target = min(BASE_SIZE, MAX_PER_MARKET − exposure)` = $30) and ≥ `MIN_STAKE=1.0`.
   If it can only partially fill → **skip** (`[skip-thin]`), never a partial.

## Stage 4 — Feed / data quality (infrastructure gates)

| Gate | Active value | Effect |
|---|---|---|
| WSS book freshness | `WSS_FRESH_SECS=3.0` | No WSS message in 3s → fall back to REST (never blocks a fire) |
| WSS authority | `USE_WSS_BOOK=true` | A swept book seen via WSS is authoritative (prevents false fills) |
| Sweep cadence | `BOOK_POLL_MS=10` | Re-checks the in-window ask every 10ms |

## Stage 5 — Order execution gate (LIVE only)
- **FOK (fill-or-kill)** — the live order either fills the full $30 at ≤ `MAX_ASK`
  instantly, or cancels entirely. No partial fills, ever. In paper this is
  simulated by the Stage-3 fill gate.

---

## Dormant (overridden, not active)
`REQUIRE_FILL` and `DYNAMIC_SIZE` are both **superseded while `WALK_FILL=true`** —
the legacy depth-matched sizing path never runs. They only matter if you set
`WALK_FILL=false`.

## Summary chain
right coin/duration → not halted → in the final-seconds window → leading side →
real move (≥1 bp) → ask in 0.65–0.85 → under exposure cap → full $30 fillable.
Miss any one and it skips.
