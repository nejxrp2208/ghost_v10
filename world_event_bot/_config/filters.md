# Market Filters — v2 (EdgeFinder Consumer)

> Layer 3 reference. **Rewritten 2026-05-27** for the EdgeFinder overhaul.
> Previous strict include/exclude lists are no longer load-bearing.

---

## Why this changed

v1 hard-excluded crypto, weather, and sports. The bot generated its own signals against politics/geopolitics/economy/tech/AI markets only.

v2 consumes EdgeFinder signals directly. EdgeFinder's primary hunting grounds are:

- **Politics / geopolitics** (M20, M31, M50, M52, M60 — softest YES, largest documented edge)
- **Crypto price binaries** (M75 — "cleanest, most repeatable NO trade EdgeFinder runs")
- **Sports favorites** (M74, M83 — weekend slates of NO entries on hyped favorites)
- **Macro / economic prints** (M3, M76 — CPI, jobs, Fed)
- **Entertainment / pop-culture** (M74 — box office, Elon tweet counts)

Filtering EdgeFinder's signals through our old exclusion list would discard the majority of their best trades. We trust EdgeFinder's pre-filter; we don't second-guess it.

---

## What we still filter on (post-EdgeFinder)

Every pending signal must pass these gates *after* EdgeFinder has selected it (see [signal-doctrine.md](signal-doctrine.md) for the full list):

| Gate | Threshold | Source |
|------|-----------|--------|
| Volume on the underlying Polymarket market | ≥ `MIN_VOLUME_24HR` (default $50,000) | M7, M11, M15, M45 |
| Spread on the order book | ≤ 0.04 (default) | Quality / slippage rule (M70) |
| Signal age | ≤ 72h since `detected_at` | M5, M11, M22 |
| Price drift since detection | NO ask within ±2¢ of EdgeFinder's `yes_price` implied NO | M5, M22, M70 |
| Fee-adjusted EV | > 0 (computed by `sizing.size_no_bet`) | M5, M55 |
| Concentration / reserve | Doesn't breach 30% cash reserve | M63 |

These are **execution-quality gates**, not topical filters. They reject signals our trader can't fill cleanly, not signals about topics we don't like.

---

## Topical preferences (advisory only)

We no longer hard-exclude. If you want to track per-category performance:

| Category | Treatment |
|----------|-----------|
| Politics / geopolitics / elections / policy | **Preferred.** Largest documented NO edge per M52. |
| Macro / Fed / CPI / jobs / inflation | **Preferred.** Scheduled resolution events; clean priors. |
| Crypto threshold binaries (BTC/ETH > $X on date Y) | **Preferred.** M75 explicitly cites 5-in-a-row wins. |
| Sports favorites (EPL, LaLiga, MLB, NBA) | **Take if EdgeFinder fires.** M74 weekend slates have been profitable. |
| Weather / natural disasters | **Skipped by EdgeFinder.** No expected signals here. |
| Generic crypto price prediction (non-binary) | **Skipped by EdgeFinder.** No expected signals here. |

These are not rejection rules — they're labels for the UI's category breakdown. EdgeFinder's selection is authoritative.

---

## Implementation notes for v2

- The old `TARGET_KEYWORDS` and `EXCLUDE_KEYWORDS` lists in [config.py](../config.py) are kept for backwards compatibility with the legacy `market_scanner.py` but are **not used** by the EdgeFinder consumer.
- `is_targeted` is no longer load-bearing in `signal_engine.py` — there are no signal tiers that gate on it.
- If we ever want to add a "personal-blacklist" feature (e.g., "I don't want to bet against this specific politician winning"), it goes here as a separate per-user list — not in EdgeFinder's pre-filter.

---

## When to revise this file

- A new EdgeFinder signal category appears in the feed that we want to track / exclude per-user.
- We add an execution gate beyond the six listed above.
- We re-introduce topical hard-filters (would require a discussion — the v2 thesis is that EdgeFinder filters for us).
