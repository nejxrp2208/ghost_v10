# Golden Hour — exact parameters, filters, and why a replay may not reproduce it

**Author:** J · **Date:** 2026-06-18 · **Status:** config disclosure + reproduction-method note
**Scope:** This is *how to reproduce the 08–11Z weekday finding on the book it was measured on (J's)* and the filter traps that suppress it. It is **NOT** a claim the hour transfers — the team has already established (B's 06-16 entries, J's 06-15 self-correction) that hours are **per-book**. Run your own UTC-block × weekday cut before adopting any window.

---

## 1. What "golden hour" actually is

Not a filter setting — a **time-of-day concentration**: **weekday (Mon–Thu) 08:00–11:00 UTC**, run at the normal *loose* live filters. On J's real fills that block is **92.4% WR, +$3.58/fire, +$236 (n=66, 6/6 green days), permutation p=0.0002** vs a ~flat whole-book. The edge is *where* you trade, not a parameter you turn on, and it lives in the **cheap, thin-move** fills — not the expensive confident ones.

## 2. Live-verified config (from the LIVE box `state.json cfg` + `.env`, 2026-06-18 ~11:52Z, mode=LIVE)

| Parameter | Golden hour (08–11Z) | Off-peak | Note |
|---|---|---|---|
| `MORNING_HOURS` | **{8, 9, 10, 11} UTC** | — | hour-of-day in **UTC** |
| `MIN_MOVE_BPS` (`min_move`) | **1.5** | **1.5** (same) | **flat box-wide, NOT hour-varied** |
| `MIN_ASK` | **0.50** | 0.50 | |
| `MAX_ASK` (effective) | **0.90** | 0.90 | `.env` base 0.78; morning & off-peak both lifted to 0.90 |
| stake (`eff_base`) | **$25 flat** | $1 flat | `MORNING_SIZE=25` / `OFFPEAK_SIZE=1`; `ADAPTIVE_STAKE=false` ⇒ no Paroli |
| `MAX_PER_MARKET` | **25** | 25 | stake = `min(eff_base, MAX_PER_MARKET)` |
| snipe window | **T-28s** | T-28s | `SNIPE_WINDOW_SECS=28` (15m also 28) |
| `MIN_FILL_SECS` | **2** | 2 | |
| coins / durations | **BTC, ETH** / **5m, 15m** | same | proven morning edge is 5m |
| `WALK_FILL` | **true** | true | |
| `DAILY_LOSS_LIMIT` | **$75** | $75 | `MAX_LOSS_STREAK=1000` (streak-halt off) |

## 3. Two facts that explain a failed reproduction

1. **`min_move` is `1.5`, FLAT across all hours.** Golden hour does NOT change the bps floor — only stake and max_ask vary by hour (`eff_base()` / `eff_max_ask()`). Any replay using a higher move floor erases the thin-move morning core where the edge sits.

2. **The weekday gate is NOT in the bot.** The live code is literally `_in_morning() = datetime.now(timezone.utc).hour in MORNING_HOURS` — hour-only, no weekday check. The bot applies $25 golden-hour sizing on weekends too (weekend 08–11Z is dead — known pending fix). **Mon–Thu is an analysis filter you must apply yourself.**

## 4. Most likely "filter left on" — ranked

1. **An old replay script with stale hardcoded floors.** J's `freq_check.py` still hardcodes `MIN_MOVE_BPS=9.0`, `MAX_ASK=0.65`, `MIN_ASK=0.02` — the *old pre-package tight values*, NOT the live `1.5 / 0.90 / 0.50`. A `move≥9bps` + `ask≤0.65` gate discards almost the entire morning edge. **First thing to check: what move/ask floors did the replay use?**
2. **Timezone.** Window is **08–11 UTC**. On a UTC+7 box that's 15:00–18:00 local. (A teammate's UTC+7 "Weekly Timetable" already failed to reproduce on 680 fills for exactly this — its line was drawn ~2h early.)
3. **Weekends pooled in.** No Mon–Thu split → dead weekend mornings average out the live ones.
4. **Wrong scope.** Different book/wallet, a taker-only API pull (`?user=`/`?market=` are taker-only by default), or a rigid 11:00Z cutoff that slices off the tail of the staking regime.

## 5. Reproduction checklist

1. Real fills only (`making_amt > 0`), same book the edge is claimed on.
2. Timestamps in **UTC**; filter `hour ∈ {8,9,10,11}`.
3. **Weekday only** (Mon–Thu); report weekend separately.
4. Loose live filters: `move ≥ 1.5bps`, `0.50 ≤ ask ≤ 0.90`. Do NOT raise the move floor or drop the cheap band.
5. Flat notional (not Paroli-stacked) for PnL.
6. Report WR **and** $/fire **and** n.

## 6. Status update (2026-06-18) — reproduced

The 08–11Z weekday block has now been **reproduced over a 9–10 day sample** (up from the original ~6 weekday dates). This confirms the earlier non-reproduction was a **filter / setup issue** (stale move/ask floors or timezone — §4), not an absent edge. **Open item to nail down:** whether this 9–10 day reproduction is J's own book extended or an *independent* book — the latter would be the first cross-book confirmation of the hour (team has so far treated hours as per-book / non-transferable).

## 7. Honest caveats

08–11Z overlaps operator-attended hours (possible density/attendance artifact); high-ask 0.85–0.90 fills will realize **below** a depth-walk backtest (the ~330ms POST-flight in-flight collapse is invisible to it); and the block should still be checked **net of the 0.072·(1−p) taker fee** before being called a durable edge. Hours have so far been **per-book** — this window is J's; B's book is US-midday, not EU-morning, so confirm whose book the 9–10 day reproduction is on before treating the hour as portable.
