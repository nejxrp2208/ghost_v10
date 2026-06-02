"""
ZUGU v3.5 — Risk-based position sizing.

Single sizing mode: FL_RISK_USDC = exact USDC amount to risk per trade.
Shares scale inversely with ask price so the dollar risk is constant.

  ask=0.30 → shares = $2 / 0.30 = 6.67  (cost $2.00)
  ask=0.50 → shares = $2 / 0.50 = 4.00  (cost $2.00)
  ask=0.70 → shares = $2 / 0.70 = 2.86  (cost $2.00)

Max loss per trade = FL_RISK_USDC (since losing leg goes to $0, full stake gone).

Why this is the only mode
-------------------------
Earlier zugu v3 used FL_FIXED_SHARES which made dollar exposure swing 2.3×
based purely on ask price — a coupled risk that's hard to budget against.
Risk-based sizing decouples position size from market price; you decide
exposure in dollars, not in tokens.
"""
from __future__ import annotations

import os
from typing import Tuple


FL_RISK_USDC = float(os.getenv("FL_RISK_USDC", "2.0"))


def compute_size(entry_ask: float) -> Tuple[float, float]:
    """Return (shares, size_usdc) for the given entry ask.

    size_usdc will always equal FL_RISK_USDC (within float rounding) — that's
    the whole point of risk-based sizing."""
    if entry_ask is None or entry_ask <= 0 or FL_RISK_USDC <= 0:
        # caller must skip — sentinel zero shares
        return 0.0, 0.0
    shares    = round(FL_RISK_USDC / entry_ask, 4)
    size_usdc = round(shares * entry_ask, 4)
    return shares, size_usdc


def describe_mode() -> str:
    """One-line config snapshot for the banner / state.json / dashboard."""
    return f"risk_usdc=${FL_RISK_USDC:.2f}"
