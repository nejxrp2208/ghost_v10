# Signal Doctrine — EdgeFinder Consumer (v2, 2026-05-27)

> Layer 3 reference. Read on every change to signal/sizing logic.
> **Major rewrite 2026-05-27**: tier-based self-grown engine replaced by EdgeFinder API consumer.
> Previous tiered doctrine (T0-NO at 0.82-0.88, T1-LOCK, etc.) is **retired** — see `_archive/` if added.

---

## Core thesis

We do not generate signals. We consume them from **EdgeFinder** (edgefinderai.org), a public ML-driven signal service whose live track record is **67.3% win rate across 316 resolved signals** (verified 2026-05-27 via `/api/stats`).

Our value-add is in the layers EdgeFinder leaves to the trader:
- **Sizing** — fractional Kelly with fee adjustment (see [sizing.md](sizing.md)).
- **Execution** — paper-fill simulation against the Polymarket CLOB.
- **Risk control** — stop-loss, correlation-cluster cap, bankroll reserve.
- **Audit** — every signal logged, every action recorded, our win rate trended against EdgeFinder's published rate.

Politicas remains **paper-only**. Live trading is out of scope.

---

## What we trade

Every EdgeFinder signal has `direction = NO`. We always buy the NO outcome.

| Signal type | Pattern | Empirical win rate (Day 59) |
|-------------|---------|----------------------------|
| `ml` | (none) | 70.7% (145W / 60L) |
| `crash` | `Momentum Crash` | ~60% (subset of crash 58.9%) |
| `crash` | `Extreme Crash` | ~60% (subset of crash 58.9%) |
| `combined` | both engines | small sample (2W / 0L) — treat as MED conviction until n ≥ 20 |

Probability `p` used in Kelly sizing comes from these published rates **adjusted by signal_type**, not from a per-signal model output (EdgeFinder doesn't expose one).

Default: `p_ml = 0.707`, `p_crash = 0.589`, `p_combined = 0.65` (conservative until sample grows).

---

## Entry gates (in order)

A pending EdgeFinder signal must pass all of these to be paper-traded:

1. **Status check** — `signal.status == "pending"`. Don't re-enter resolved trades.
2. **Already-taken check** — no existing open position with `edgefinder_signal_id == signal.id`. One position per signal id, full stop.
3. **Price drift check** — current Polymarket NO ask (from `clob_client`) must be within ±2¢ of the price implied by `signal.yes_price` (M70 slippage rule). If drift > 2¢, skip with reason `skipped:price_drift_<delta>c`.
4. **Volume check** — current 24h volume on the underlying market must be ≥ `MIN_VOLUME_24HR` ($50,000 per M7/M11/M15/M45). If below, skip with reason `skipped:thin_volume_<vol>`.
5. **Age check** — `now - signal.detected_at` ≤ 72h. If older, skip with reason `skipped:stale_signal`.
6. **Fee-adjusted EV check** — `sizing.size_no_bet(...).refuse` must be False. If True, skip with reason `skipped:negative_ev`.
7. **Concentration check** — open exposure (sum of position cost) + this trade ≤ `(1 - RESERVE_PCT) * starting_bankroll`. If breached, skip with reason `skipped:reserve_breach`.

If all gates pass → take the trade at the current NO ask, record the EdgeFinder signal id on the position row.

---

## Position management

| Rule | Value | Source |
|------|-------|--------|
| Sizing method | Fractional Kelly via [sizing.py](../sizing.py) | M4, M9, M15, M55, M69, M72 |
| Default Kelly fraction | **0.5** (half-Kelly) | M4, M55, M69, M72 |
| Max bankroll per trade | **5%** | M9, M15 |
| Cash reserve | **30%** | M63 |
| Stop-loss trigger | YES climbs back to **0.50** → exit at market NO ask | M17, M22, M28, M30 |
| Hold horizon | Through resolution unless stop-loss fires; **no auto-close at 0.90** | M22, M35, M50, M64 |
| Cooldown after close | 24h same `market_id` (legacy rule, kept) | DB invariant |
| Correlation cluster cap | ≤ **25%** of bankroll on positions resolving in same 7-day bucket | M27, M59, M63 |

---

## What we do NOT do

| Don't | Why |
|-------|-----|
| Generate our own signals | EdgeFinder is the brain. Politicas is the trader. |
| Auto-close at 0.90 | M22/M35/M50/M64 — let time decay run. Stop-loss handles downside. |
| Take signals with `status != pending` | They're already resolved. Re-entry is the bug. |
| Trade more than 5% of bankroll on any signal | Sizing rule. Even a 90% conviction Kelly answer gets capped. |
| Boost confidence on news-correlated markets | M20/M29 — the first 15-30 min after breaking news is *worse*, not better. News-boost is removed entirely until we instrument an actual decay function. |

---

## Filter scope (relaxed in v2)

Politicas previously hard-excluded crypto / weather / sports. EdgeFinder's hunting ground **includes** crypto threshold binaries (M75) and sports favorites (M74) — so we now follow EdgeFinder's market selection rather than filtering it. See [filters.md](filters.md).

---

## When to revise this file

You **must** update this doctrine before merging any change that:
- Changes the entry gates (above).
- Changes the default Kelly fraction, bankroll cap, or reserve.
- Changes the stop-loss trigger.
- Adds/removes a signal_type or alters how `p` is sourced.
- Adds an auto-close threshold.
- Changes the EdgeFinder dependency (e.g. switches to a different feed).

The audit stage (`stages/01-audit`) compares this file against `signal_engine.py` and `sizing.py` — drift is a finding.
