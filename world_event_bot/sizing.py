"""
Fractional Kelly position sizing.

Source: lessons M4, M9, M15, M55, M69, M72. See _config/sizing.md for the
"why" and the worked WTI Crude / Bitcoin examples we validate against.

Formula:    f* = (b·p − q) / b

  f*  = fraction of bankroll to bet
  b   = net odds   = (1 / cost) − 1     where `cost` is what we pay per $1 payout
  p   = our estimated win probability   (from EdgeFinder stats by signal_type)
  q   = 1 − p

For NO-side prediction-market trades at YES_price = y:
  cost_per_dollar_NO = (1 − y)           NO contracts cost (1 − y) and pay $1
  b                  = (1 / (1 − y)) − 1 = y / (1 − y)

We then:
  - Multiply by the user's `KELLY_FRACTION` (default 0.5 = half-Kelly).
  - Cap at `MAX_BANKROLL_PCT_PER_TRADE` (default 0.05).
  - Reduce by fee drag: account for `POLYMARKET_FEE_RATE` before computing EV.
  - Refuse to size if fee-adjusted EV ≤ 0.
"""

from dataclasses import dataclass
from typing import Optional

import config


@dataclass
class KellyOutcome:
    """Result of a sizing decision — all the numbers needed for audit + UI."""
    yes_price: float           # market YES price at sizing
    no_cost: float             # 1 - yes_price
    b: float                   # net odds on NO bet
    p: float                   # estimated NO win probability
    q: float                   # 1 - p
    full_kelly_pct: float      # raw f* (before fraction + cap)
    fractional_kelly_pct: float  # after KELLY_FRACTION applied
    capped_pct: float          # after MAX_BANKROLL_PCT_PER_TRADE cap
    fee_rate: float
    ev_per_dollar: float       # expected value of $1 risk after fees
    refuse: bool               # True if we should NOT take the trade
    refuse_reason: Optional[str]


def size_no_bet(
    yes_price: float,
    win_probability: float,
    bankroll: float,
    fee_rate: Optional[float] = None,
    kelly_fraction: Optional[float] = None,
    cap_pct: Optional[float] = None,
) -> KellyOutcome:
    """
    Compute the dollar size to risk on a NO bet against the given YES price.

    Returns a KellyOutcome with `dollar_size = capped_pct * bankroll` callable as
    `outcome.dollar_size(bankroll)`. Use `outcome.refuse` to gate the trade.
    """
    # Defaults pull from config so callers can override per-signal
    fee_rate       = fee_rate       if fee_rate       is not None else config.POLYMARKET_FEE_RATE
    kelly_fraction = kelly_fraction if kelly_fraction is not None else config.KELLY_FRACTION
    cap_pct        = cap_pct        if cap_pct        is not None else config.MAX_BANKROLL_PCT_PER_TRADE

    # Sanity gates
    if not 0.0 < yes_price < 1.0:
        return _refuse("Invalid yes_price (must be 0 < y < 1)", yes_price, win_probability, fee_rate)
    if not 0.0 < win_probability < 1.0:
        return _refuse("Invalid win_probability (must be 0 < p < 1)", yes_price, win_probability, fee_rate)
    if bankroll <= 0:
        return _refuse("Bankroll must be positive", yes_price, win_probability, fee_rate)

    no_cost = 1.0 - yes_price
    b = yes_price / no_cost   # equivalent to (1/no_cost) - 1
    p = win_probability
    q = 1.0 - p

    # Fee-adjusted EV per dollar risked.
    # Win: collect (1 - no_cost) - fee_rate * 1   (fee on the $1 payout)
    # Lose: lose no_cost
    gross_win_per_dollar  = (1.0 - no_cost) - fee_rate
    gross_loss_per_dollar = no_cost
    ev_per_dollar = p * gross_win_per_dollar - q * gross_loss_per_dollar

    # Refuse if fee-adjusted EV ≤ 0
    if ev_per_dollar <= 0:
        return _refuse(
            f"Fee-adjusted EV ≤ 0 (ev={ev_per_dollar:+.4f}/$). Skip per M5/M55.",
            yes_price, p, fee_rate
        )

    # Raw Kelly
    full_kelly_pct = (b * p - q) / b
    if full_kelly_pct <= 0:
        return _refuse(
            f"Full Kelly is non-positive (f*={full_kelly_pct:+.4f}). Edge too small.",
            yes_price, p, fee_rate
        )

    fractional_kelly_pct = full_kelly_pct * kelly_fraction
    capped_pct = min(fractional_kelly_pct, cap_pct)

    return KellyOutcome(
        yes_price=yes_price,
        no_cost=no_cost,
        b=b,
        p=p,
        q=q,
        full_kelly_pct=full_kelly_pct,
        fractional_kelly_pct=fractional_kelly_pct,
        capped_pct=capped_pct,
        fee_rate=fee_rate,
        ev_per_dollar=ev_per_dollar,
        refuse=False,
        refuse_reason=None,
    )


def dollar_size(outcome: KellyOutcome, bankroll: float) -> float:
    """Round to 4 decimals for paper trading; live trading would use platform tick size."""
    if outcome.refuse:
        return 0.0
    return round(outcome.capped_pct * bankroll, 4)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _refuse(reason: str, yes_price: float, p: float, fee_rate: float) -> KellyOutcome:
    return KellyOutcome(
        yes_price=yes_price,
        no_cost=max(0.0, 1.0 - yes_price),
        b=0.0,
        p=p,
        q=1.0 - p,
        full_kelly_pct=0.0,
        fractional_kelly_pct=0.0,
        capped_pct=0.0,
        fee_rate=fee_rate,
        ev_per_dollar=0.0,
        refuse=True,
        refuse_reason=reason,
    )


# ── Module-level self-test (run as `python -m sizing` or `python sizing.py`) ─

if __name__ == "__main__":
    # M69 WTI Crude example:
    # YES @ 39.5¢, NO @ 60.5¢, p=0.65 → f* = 0.115 (full Kelly = 11.5%)
    # Half Kelly should be 5.7%
    o = size_no_bet(
        yes_price=0.395,
        win_probability=0.65,
        bankroll=1000.0,
        fee_rate=0.0,        # M69 example doesn't deduct fees
        kelly_fraction=1.0,  # show full Kelly first
        cap_pct=1.0,         # no cap so we see raw
    )
    print(f"WTI full-Kelly check: f*={o.full_kelly_pct:.4f}  (expected ~0.1150)")

    o_half = size_no_bet(
        yes_price=0.395,
        win_probability=0.65,
        bankroll=1000.0,
        fee_rate=0.0,
        kelly_fraction=0.5,
        cap_pct=1.0,
    )
    print(f"WTI half-Kelly check: fractional={o_half.fractional_kelly_pct:.4f}  (expected ~0.0575)")

    # M72 Bitcoin example:
    # YES @ 32.5¢, p=0.72 → f* = 0.138 full Kelly, 6.9% half
    o_btc = size_no_bet(
        yes_price=0.325,
        win_probability=0.72,
        bankroll=1000.0,
        fee_rate=0.0,
        kelly_fraction=1.0,
        cap_pct=1.0,
    )
    print(f"BTC full-Kelly check: f*={o_btc.full_kelly_pct:.4f}  (expected ~0.1380)")

    # With realistic fee drag (4.8%) on the WTI example:
    o_fees = size_no_bet(
        yes_price=0.395,
        win_probability=0.65,
        bankroll=1000.0,
    )
    print(f"WTI half-Kelly + 4.8% fees + 5% cap: ${dollar_size(o_fees, 1000.0):.2f}")
    print(f"  ev_per_dollar={o_fees.ev_per_dollar:+.4f}  refuse={o_fees.refuse}")
