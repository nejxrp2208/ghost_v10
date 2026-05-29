"""
Paper trading engine — v2 (EdgeFinder consumer).

No real trades, no private keys, no wallet.

Sizing       : fractional Kelly via sizing.size_no_bet (see _config/sizing.md)
Entry        : NO contract at the current NO ask (or EdgeFinder's published price
               if live ask unavailable). Cost = entry_price × shares.
Position mgmt:
  - Stop loss: close if live YES climbs back to STOP_LOSS_YES_THRESHOLD (default 0.50)
  - Resolution: close at NO=1.00 when EdgeFinder marks signal `win`,
                close at NO=0.00 when EdgeFinder marks signal `loss`
  - No auto-close at 0.90 — let time decay run (doctrine M22/M35/M50/M64)
P&L          : same formula as v1 — (current - entry) × shares
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
import database as db
import edgefinder_client
import polymarket_lookup
import sizing
from clob_client import fetch_midpoint

logger = logging.getLogger(__name__)


# ── Signal execution ──────────────────────────────────────────────────────────

def execute_signals(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Paper-trade each emitted signal. Sizing comes from signal['_kelly'] (set by
    signal_engine). Respects MAX_OPEN_POSITIONS and live cash.
    """
    executed: List[Dict[str, Any]] = []
    cash       = db.get_cash_balance()
    open_count = db.get_open_positions_count()
    bankroll   = cash + _open_position_cost()

    for sig in signals:
        cap = config.MAX_OPEN_POSITIONS
        if cap > 0 and open_count >= cap:
            db.log_message("INFO", f"Position cap ({cap}) reached — {len(signals)-len(executed)} skipped")
            break

        kelly: Optional[sizing.KellyOutcome] = sig.get("_kelly")
        if kelly is None or kelly.refuse:
            continue

        price = float(sig.get("price") or 0.0)
        if not (0.0 < price < 1.0):
            continue

        # Kelly $size → shares; never exceed available cash
        dollar = sizing.dollar_size(kelly, bankroll)
        if dollar <= 0 or dollar > cash:
            db.log_message(
                "WARNING",
                f"Insufficient sizing (${dollar:.2f}; cash ${cash:.2f}) — "
                f"skip {sig.get('question','')[:55]}"
            )
            continue

        shares = round(dollar / price, 4)
        cost   = round(shares * price, 4)
        if cost > cash or shares <= 0:
            continue

        # Stop-loss price (M17/M22): close NO when YES climbs back to threshold
        stop_loss = config.STOP_LOSS_YES_THRESHOLD

        position = _open_v2_position(
            market_id   = sig["market_id"],
            token_id    = sig["token_id"],
            question    = sig["question"],
            side        = f"NO ({sig['signal']})",
            entry_price = price,
            size        = shares,
            edgefinder_signal_id = sig.get("edgefinder_signal_id", ""),
            stop_loss_price      = stop_loss,
            signal_type          = sig.get("_signal_type", ""),
            pattern              = sig.get("_pattern", ""),
        )

        if position:
            cash       -= cost
            open_count += 1
            executed.append(position)
            if "id" in sig:
                db.mark_signal_acted(sig["id"])
            db.log_message(
                "INFO",
                f"OPENED NO | {shares:.4f} @ ${price:.4f} = ${cost:.2f} | "
                f"Kelly half={kelly.fractional_kelly_pct:.2%} cap={kelly.capped_pct:.2%} | "
                f"{sig.get('question','')[:60]}"
            )

    return executed


# ── Position management ──────────────────────────────────────────────────────

def update_positions(_unused: Any = None) -> None:
    """
    Refresh current_price + unrealized_pnl for every open position.
    Live NO price comes from the CLOB book (via polymarket_lookup).
    """
    updated = 0
    for pos in db.get_open_positions():
        no_now = _current_no_price(pos)
        if no_now is None or not (0.0 < no_now < 1.0):
            continue
        db.update_position_price(pos["id"], no_now)
        updated += 1

    if updated:
        db.log_message("INFO", f"Refreshed prices for {updated} open positions")


def auto_resolve_positions(_unused: Any = None) -> List[Dict]:
    """
    Close positions whose:
      • EdgeFinder signal flipped to `win` or `loss` (use $1 / $0 settlement)
      • live YES price has climbed back to STOP_LOSS_YES_THRESHOLD (stop-loss)

    No 0.90 auto-close. No 24h forced close. Time decay is the strategy.
    """
    closed: List[Dict] = []

    # Build a lookup of EdgeFinder signal status by id
    ef_signals = {s["id"]: s for s in db.get_edgefinder_signals(limit=500)}

    for pos in db.get_open_positions():
        ef_id = pos.get("edgefinder_signal_id") or ""
        ef    = ef_signals.get(ef_id)

        # 1. EdgeFinder resolved this signal?
        if ef:
            status = ef.get("status")
            if status == "win":
                result = _close(pos["id"], 1.0, f"EDGEFINDER WIN — settles at $1 NO")
                if result:
                    closed.append(result)
                continue
            if status == "loss":
                result = _close(pos["id"], 0.0, f"EDGEFINDER LOSS — settles at $0 NO")
                if result:
                    closed.append(result)
                continue

        # 2. Stop-loss: YES climbed back to threshold
        yes_now = _current_yes_price(pos)
        if yes_now is not None and yes_now >= config.STOP_LOSS_YES_THRESHOLD:
            no_now = round(1.0 - yes_now, 4)
            result = _close(
                pos["id"], no_now,
                f"STOP-LOSS yes={yes_now:.3f} ≥ {config.STOP_LOSS_YES_THRESHOLD:.2f}"
            )
            if result:
                closed.append(result)
            continue

    if closed:
        db.log_message("INFO", f"Auto-resolve closed {len(closed)} positions")
    return closed


def close_position_by_id(position_id: int, exit_price: float) -> Optional[Dict]:
    """Manual close — exposed for the UI."""
    return _close(position_id, exit_price, f"MANUAL @ ${exit_price:.4f}")


def close_all_positions_at_mid(_unused: Any = None) -> List[Dict]:
    """Emergency: close everything at current NO mid (or entry if unknown)."""
    closed = []
    for pos in db.get_open_positions():
        no_now = _current_no_price(pos) or pos["entry_price"]
        if 0.0 < no_now < 1.0:
            result = _close(pos["id"], no_now, "EMERGENCY CLOSE ALL")
            if result:
                closed.append(result)
    return closed


# ── DB layer (v2 columns) ────────────────────────────────────────────────────

def _open_v2_position(
    *, market_id: str, token_id: str, question: str, side: str,
    entry_price: float, size: float,
    edgefinder_signal_id: str, stop_loss_price: float,
    signal_type: str, pattern: str,
) -> Optional[Dict]:
    """
    Wrap db.open_position so we can also write the v2-only columns
    (edgefinder_signal_id, stop_loss_price, signal_type, pattern).
    """
    pos = db.open_position(
        market_id=market_id, token_id=token_id, question=question,
        side=side, entry_price=entry_price, size=size,
    )
    if not pos:
        return None

    import sqlite3
    try:
        with sqlite3.connect(config.DB_PATH) as conn:
            conn.execute(
                """
                UPDATE positions SET
                  edgefinder_signal_id = ?,
                  stop_loss_price      = ?,
                  signal_type          = ?,
                  pattern              = ?
                WHERE id = ?
                """,
                (edgefinder_signal_id, stop_loss_price, signal_type, pattern, pos["id"]),
            )
            conn.commit()
    except sqlite3.OperationalError as e:
        logger.warning("v2 position columns not present (run migration 001): %s", e)

    pos["edgefinder_signal_id"] = edgefinder_signal_id
    pos["stop_loss_price"]      = stop_loss_price
    pos["signal_type"]          = signal_type
    pos["pattern"]              = pattern
    return pos


# ── Live price helpers ───────────────────────────────────────────────────────

def _current_no_price(pos: Dict[str, Any]) -> Optional[float]:
    """
    Try, in order:
      1. CLOB midpoint on the position's NO token
      2. Polymarket lookup via the EdgeFinder signal's market_url
    """
    token_id = pos.get("token_id", "")
    if token_id and not token_id.startswith("ef-no:"):
        mid = fetch_midpoint(token_id)
        if mid is not None and 0.0 < mid < 1.0:
            return mid

    # Fallback: look up via the signal
    ef_id = pos.get("edgefinder_signal_id") or ""
    if ef_id:
        ef = next((s for s in db.get_edgefinder_signals(limit=500) if s["id"] == ef_id), None)
        if ef:
            yes_now = polymarket_lookup.current_yes_price(
                ef.get("market_url", ""), ef.get("question", "")
            )
            if yes_now is not None:
                return round(1.0 - yes_now, 4)
    return None


def _current_yes_price(pos: Dict[str, Any]) -> Optional[float]:
    no_now = _current_no_price(pos)
    return round(1.0 - no_now, 4) if no_now is not None else None


def _open_position_cost() -> float:
    return db.get_open_position_cost()


# ── Close helper ─────────────────────────────────────────────────────────────

def _close(position_id: int, exit_price: float, log_reason: str) -> Optional[Dict]:
    result = db.close_position(position_id, exit_price)
    if result:
        pnl = result.get("realized_pnl", 0)
        db.log_message(
            "INFO",
            f"CLOSED #{position_id} | {log_reason} | P&L ${pnl:+.4f}"
        )
    return result
