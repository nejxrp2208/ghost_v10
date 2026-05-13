"""
paper_fill_sim.py — realistic LIVE-fill simulator for PAPER mode.

PAPER mode currently fakes 100% fills at the exact seen ask. Real LIVE fills
face sniper competition, slippage, partial fills, and ghost fills — typical
real fill rate at peak hours is 40-70%, sometimes lower.

This module models that friction so PAPER PnL projections are honest.

ENABLE: set PAPER_FILL_SIM=true in .env. Default is false (existing behavior).

Outcomes per fire:
  FILLED    — got the seen ask exactly
  SLIPPED   — filled at ask + 1-2 ticks worse (ask price moved during latency)
  REJECTED  — order didn't fill (faster sniper, ask gone, partial cancel)

Fill probability is a function of:
  secs_left    — closer to close = more bots watching = lower fill rate
  ask_price    — cheaper asks = more competitive = lower fill rate
  ask_size_usd — smaller ask = harder to win → lower fill rate
  coin         — BTC > ETH > SOL/XRP/BNB in competition
  hour_et      — peak (8 ET) = more bots vs off-peak

Numbers are educated defaults based on typical CLOB dynamics. Once arb_log
has 1-3 days of orderbook-velocity data, we can replace these with empirically
derived rates. For now they're a reasonable starting model.
"""
import os
import random
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Tuple

REPORTS_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "agent_reports.db"
)


# ── Schema ────────────────────────────────────────────────────────────────────
def init_schema():
    con = sqlite3.connect(REPORTS_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS paper_fill_simulation (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            coin            TEXT,
            tier            INTEGER,
            outcome_bet     TEXT,            -- UP / DOWN
            secs_left       INTEGER,
            hour_et         INTEGER,
            intended_ask    REAL,
            ask_size_usd    REAL,
            order_size_usd  REAL,
            sim_outcome     TEXT,            -- FILLED / SLIPPED / REJECTED
            actual_ask      REAL,            -- price we actually filled at (NULL if rejected)
            slip_ticks      INTEGER,         -- 0 if no slip, 1-2 otherwise
            fill_prob_est   REAL             -- the probability we computed
        )
    """)
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_pfs_ts ON paper_fill_simulation(ts)
    """)
    con.commit()
    con.close()


# ── Probability model ─────────────────────────────────────────────────────────
def _base_fill_prob_by_secs_left(secs: float) -> float:
    """Time-to-close has the biggest effect: more bots near close."""
    if secs > 300:  return 0.92
    if secs > 120:  return 0.85
    if secs > 60:   return 0.72
    if secs > 30:   return 0.55
    if secs > 15:   return 0.40
    if secs >  5:   return 0.25
    return 0.10


def _ask_price_multiplier(ask: float) -> float:
    """Cheaper asks attract more snipers."""
    if ask < 0.01: return 0.45    # deepest stale asks: heavy competition
    if ask < 0.02: return 0.60
    if ask < 0.04: return 0.75
    if ask < 0.10: return 0.90
    return 1.00                    # asks >= $0.10: no competition penalty


def _ask_size_multiplier(ask_size_usd: float, order_size_usd: float) -> float:
    """If their ask size < our order, partial fill probability rises."""
    if ask_size_usd <= 0:    return 0.15
    if ask_size_usd < order_size_usd:
        return 0.6 * (ask_size_usd / order_size_usd)
    if ask_size_usd < order_size_usd * 2: return 0.85
    return 1.00


def _coin_multiplier(coin: str) -> float:
    coin = (coin or "").upper()
    if coin == "BTC": return 0.85
    if coin == "ETH": return 0.92
    return 1.00       # SOL/XRP/BNB: less competition


def _hour_multiplier(hour_et: int) -> float:
    """Peak hours (US open) have more bots watching."""
    if hour_et in (8, 9, 13, 14): return 0.85
    if hour_et in (10, 11, 12, 15, 16): return 0.92
    return 1.00


def fill_probability(
    secs_left: float,
    ask: float,
    ask_size_usd: float,
    order_size_usd: float,
    coin: str,
    hour_et: int,
) -> float:
    """Combined fill probability, clamped to [0.05, 0.98]."""
    p = _base_fill_prob_by_secs_left(secs_left)
    p *= _ask_price_multiplier(ask)
    p *= _ask_size_multiplier(ask_size_usd, order_size_usd)
    p *= _coin_multiplier(coin)
    p *= _hour_multiplier(hour_et)
    return max(0.05, min(0.98, p))


# ── Slippage model (when filled, sometimes worse than seen ask) ───────────────
def _sample_slippage_ticks() -> int:
    """0 ticks (clean fill) most of the time, 1 tick sometimes, 2 ticks rarely."""
    r = random.random()
    if r < 0.70: return 0   # 70% clean fills
    if r < 0.95: return 1   # 25% one tick worse
    return 2                # 5% two ticks worse


def _apply_slippage(ask: float, ticks: int) -> float:
    """Polymarket tick = $0.001."""
    return round(ask + ticks * 0.001, 4)


# ── Main entry point ──────────────────────────────────────────────────────────
def simulate_fill(
    *,
    coin: str,
    tier: int,
    outcome_bet: str,
    secs_left: float,
    intended_ask: float,
    ask_size_usd: float,
    order_size_usd: float,
    hour_et: int,
) -> Tuple[bool, Optional[float], dict]:
    """
    Simulate a LIVE fill in PAPER mode.

    Returns:
      (filled, actual_ask, sim_meta)
        filled:     True if order would have filled, False if rejected
        actual_ask: filled price if filled (with slippage), None if rejected
        sim_meta:   dict with all decision metadata (for logging)
    """
    p = fill_probability(secs_left, intended_ask, ask_size_usd,
                          order_size_usd, coin, hour_et)
    roll = random.random()
    if roll > p:
        meta = dict(sim_outcome="REJECTED", actual_ask=None,
                    slip_ticks=0, fill_prob_est=p)
        return False, None, meta

    ticks = _sample_slippage_ticks()
    if ticks == 0:
        meta = dict(sim_outcome="FILLED", actual_ask=intended_ask,
                    slip_ticks=0, fill_prob_est=p)
        return True, intended_ask, meta

    actual = _apply_slippage(intended_ask, ticks)
    # Cap slippage so absurd asks aren't generated
    if actual >= 1.00:
        actual = round(intended_ask, 4)
        ticks = 0
    meta = dict(sim_outcome="SLIPPED", actual_ask=actual,
                slip_ticks=ticks, fill_prob_est=p)
    return True, actual, meta


def log_simulation(
    *,
    coin: str,
    tier: int,
    outcome_bet: str,
    secs_left: float,
    hour_et: int,
    intended_ask: float,
    ask_size_usd: float,
    order_size_usd: float,
    sim_meta: dict,
):
    """Write one row to paper_fill_simulation. Best-effort; never raises."""
    try:
        con = sqlite3.connect(REPORTS_DB, timeout=2)
        con.execute("""
            INSERT INTO paper_fill_simulation (
                ts, coin, tier, outcome_bet,
                secs_left, hour_et,
                intended_ask, ask_size_usd, order_size_usd,
                sim_outcome, actual_ask, slip_ticks, fill_prob_est
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            coin, tier, outcome_bet,
            int(secs_left) if secs_left is not None else None,
            hour_et,
            intended_ask, ask_size_usd, order_size_usd,
            sim_meta["sim_outcome"], sim_meta["actual_ask"],
            sim_meta["slip_ticks"], sim_meta["fill_prob_est"],
        ))
        con.commit()
        con.close()
    except Exception:
        pass


# ── Init schema on module import (idempotent) ────────────────────────────────
init_schema()
