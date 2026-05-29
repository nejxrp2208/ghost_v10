# Position Sizing — Fractional Kelly

> Layer 3 reference. Read when touching [sizing.py](../sizing.py) or any code that computes a trade dollar amount.

Authoritative source for the math is [sizing.py](../sizing.py); this file is the *why*.

---

## The formula

For a NO bet against a market with YES price `y`:

```
no_cost  = 1 − y                  contracts cost (1 − y), pay $1 on NO win
b        = y / (1 − y)            net odds on the NO bet
p        = estimated NO win probability
q        = 1 − p

f*       = (b · p − q) / b        full Kelly fraction of bankroll
f_frac   = f* × KELLY_FRACTION    fractional Kelly (half-K default)
f_size   = min(f_frac, CAP_PCT)   capped fraction
$size    = f_size × bankroll
```

## Worked examples (these are sizing.py self-tests)

| Lesson | YES | p | Full Kelly | Half-Kelly | Notes |
|--------|-----|---|-----------|-----------|-------|
| M69 (WTI Crude) | 0.395 | 0.65 | **11.5%** | 5.7% | Lesson cites 0.115 |
| M72 (Bitcoin > $80K) | 0.325 | 0.72 | **13.8%** | 6.9% | Lesson cites 0.138 |

Both reproduced exactly by `python sizing.py`.

---

## Source of `p` (the estimated win probability)

Three layers, in order of preference (see `signal_engine._win_probability`):

### Layer 1 — Trained calibrator (`ml/calibrator.py`)

When `models/calibrator.pkl` exists AND its `promoted` flag is `True`, we use a per-signal probability from the logistic regression trained on EdgeFinder's full resolved history. Features: `yes_price`, `is_ml`, `is_extreme_crash`, `category_politics`, `category_crypto`.

**Promotion criteria** (all three must hold on the time-ordered hold-out):
- Hold-out log-loss < baseline (per-type-average) log-loss
- Hold-out expected calibration error (ECE) ≤ 0.05
- Hold-out AUC ≥ 0.55

If the trained model fails any of these, it's saved but `predict()` returns `None` and we fall through to layer 2.

Train command: `python -m ml.calibrator train`.

**Auto-retrain is built in.** At the end of every cycle (both `scheduler.py` and the Streamlit "Run cycle now" button), `ml.calibrator.maybe_retrain()` runs. It retrains if **both** are true:

1. ≥ `CALIBRATOR_RETRAIN_DAYS` days have elapsed since the last train (default **7**)
2. ≥ `CALIBRATOR_MIN_NEW_RESOLVED` new resolved EdgeFinder signals have accumulated since (default **20**)

Both knobs are overridable via `.env`. The cadence is intentionally weekly: retraining too often on small new samples leaks the validation set and inflates apparent promotion rates. A manual "Retrain now" button in the Our Engine tab bypasses both gates when you want an unconditional refresh.

A new training run **may flip promotion status either way** — that's the point of running it on fresh data. If it un-promotes, the bot reverts to refined static rates automatically.

### Layer 2 — Live published rates

EdgeFinder's `/api/stats` exposes the current win rate per `signal_type`. We read it via `edgefinder_client.stats_win_rate()` on every cycle. This is what `p` falls back to when no calibrator is promoted.

### Layer 3 — Refined static fallback

When both above are unavailable (cold start, API down):

- `p_ml              = 0.707`   (n=205 in 2026-05-27 audit)
- `p_momentum_crash  = 0.588`   (n=68)
- `p_extreme_crash   = 0.640`   (n=25)
- `p_combined        = 0.65`    (n=2, kept conservative)
- `p_unknown         = 0.60`

The split of `crash` into Momentum vs Extreme came from the same audit — averaging them at 0.589 was systematically under-sizing Extreme bets.

---

## Why fractional Kelly (not full)

From lessons M4, M55, M69, M72:

1. **Estimated `p` is itself uncertain.** Full Kelly assumes you know `p` exactly. You don't — `p` is a rolling average of published wins, which is itself an estimator.
2. **Variance is real.** A 67% strategy still produces 5-loss streaks. Half-Kelly survives them; full Kelly halves your bankroll.
3. **Compounding favors resilience.** Peak growth rate is less important than never getting cut in half.

Default `KELLY_FRACTION = 0.5` (half-Kelly). Configurable per session via `.env`.

---

## Fee drag (the silent killer)

Polymarket fees on winning positions have been observed up to **4.8%** (M5).

`sizing.size_no_bet` deducts the fee from the win payout *before* computing EV:

```
ev_per_dollar = p × ((1 − no_cost) − fee_rate) − q × no_cost
```

If `ev_per_dollar ≤ 0` → `KellyOutcome.refuse = True`. The position is skipped with reason `skipped:negative_ev`.

This is a meaningful gate. At YES=0.45 with `p = 0.589` (crash), EV after 4.8% fees is negative — even though the raw structural edge looks fine. Crash signals at high YES prices fail the gate; ML signals at low YES prices generally pass.

---

## Hard caps

| Cap | Value | Where |
|-----|-------|-------|
| `MAX_BANKROLL_PCT_PER_TRADE` | 0.05 (5%) | `config.py`, applied in `sizing.size_no_bet` |
| `RESERVE_PCT` | 0.30 (30% cash always idle) | `config.py`, applied in `paper_trader.execute_signals` (Phase 3) |
| Correlation-cluster cap | 0.25 (25% on same 7-day resolution bucket) | `paper_trader.execute_signals` (Phase 3) |

The 5% cap binds before Kelly does on high-conviction signals. The 30% reserve binds when the open-position count grows. The correlation cap binds when multiple signals resolve in the same week.

---

## When to revise this file

- Changes to the Kelly formula, fraction, or cap
- Changes to fee modeling
- Changes to source of `p` (e.g. switching to per-signal probability if EdgeFinder adds it)
- Changes to reserve or correlation rules
