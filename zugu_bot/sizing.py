"""
ZUGU v3.5 — Risk-based position sizing.

Replaces the legacy FL_FIXED_SHARES=5 model with a fixed-risk-in-USDC model so
the operator can directly cap their per-trade dollar exposure.

Why this matters
----------------
With FL_FIXED_SHARES=5 the actual capital deployed varies wildly with ask price:
  ask=0.30 → 5 shares × $0.30 = $1.50 risk
  ask=0.70 → 5 shares × $0.70 = $3.50 risk
That's a 2.3× difference for "the same" position. With FL_RISK_USDC=2 you ALWAYS
risk exactly $2, and the shares scale inversely with ask:
  ask=0.30 → 6.67 shares (cost $2.00)
  ask=0.70 → 2.86 shares (cost $2.00)

Max loss per trade = size_usdc (since if you lose, NO tokens → $0 = full stake).

Backwards compatibility
-----------------------
If FL_RISK_USDC is unset OR <= 0, falls back to FL_FIXED_SHARES so the v3.5
release default still works. Existing operators see no change unless they
explicitly opt in to risk sizing.
"""
from __future__ import annotations

import os
from typing import Tuple


FL_RISK_USDC    = float(os.getenv("FL_RISK_USDC",    "0"))   # 0 = disabled, use fixed shares
FL_FIXED_SHARES = float(os.getenv("FL_FIXED_SHARES", "5"))   # legacy fallback


def compute_size(entry_ask: float) -> Tuple[float, float]:
    """Returns (shares, size_usdc) for the given entry ask price.

    - If FL_RISK_USDC > 0: shares scaled to hit that exact risk cap.
    - Else: legacy FL_FIXED_SHARES * entry_ask (no behavior change).

    Both values are rounded to 4 decimals (matches scanner.py persist style)."""
    if entry_ask is None or entry_ask <= 0:
        # nonsense ask — return a degenerate size; caller is expected to skip
        return 0.0, 0.0

    if FL_RISK_USDC > 0:
        shares    = round(FL_RISK_USDC / entry_ask, 4)
        size_usdc = round(shares * entry_ask, 4)
    else:
        shares    = float(FL_FIXED_SHARES)
        size_usdc = round(FL_FIXED_SHARES * entry_ask, 4)

    return shares, size_usdc


def describe_mode() -> str:
    """Used by banner / state.json to surface current sizing config to operator."""
    if FL_RISK_USDC > 0:
        return f"risk_usdc=${FL_RISK_USDC:.2f}"
    return f"fixed_shares={FL_FIXED_SHARES:g}"
