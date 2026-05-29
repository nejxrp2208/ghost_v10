"""
Signal engine — v2 (EdgeFinder consumer).

Replaces the v1 tier-based engine. We no longer generate signals from
Polymarket scans; we consume the EdgeFinder /api/signals feed and apply
seven entry gates (see _config/signal-doctrine.md) before paper-trading.

For each pending EdgeFinder signal:
  1. status == "pending"
  2. no existing open position with same edgefinder_signal_id
  3. price drift since detection within MAX_PRICE_DRIFT
  4. underlying market volume >= MIN_VOLUME_24HR
  5. signal age <= MAX_SIGNAL_AGE_HOURS
  6. fee-adjusted EV > 0 (via sizing.size_no_bet)
  7. concentration / reserve / cluster caps not breached

Output rows are stored in the `signals` table with EdgeFinder linkage.
Paper execution happens in paper_trader.execute_signals.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
import database as db
import edgefinder_client
import polymarket_lookup
import sizing

logger = logging.getLogger(__name__)


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class GateResult:
    take: bool
    reason: str  # "taken" or "skipped:<reason>"
    yes_price_now: Optional[float] = None
    no_ask_now: Optional[float] = None
    volume_24hr: Optional[float] = None
    kelly_outcome: Optional[sizing.KellyOutcome] = None
    token_id_no: str = ""


# ── Probability source (Kelly's `p`) ─────────────────────────────────────────
#
# Three layers, in order of preference:
#   1. Trained calibrator (ml/calibrator.py) — per-signal P(NO wins | features)
#   2. EdgeFinder /api/stats live rates — per-type averages (refreshed every cycle)
#   3. Static fallback table — refined from the 2026-05-27 audit on 298 resolved
#
# The static table now splits 'crash' into Momentum vs Extreme (audit found
# Extreme Crash wins 64.0%, Momentum 58.8% — averaging at 0.589 under-sizes
# Extreme bets).

_DEFAULT_WIN_RATES = {
    "ml":              0.707,   # n=205 in 2026-05-27 audit
    "momentum_crash":  0.588,   # n=68
    "extreme_crash":   0.640,   # n=25
    "crash":           0.593,   # composite — used only if pattern unknown
    "combined":        0.65,    # n=2, kept conservative
    "unknown":         0.60,
}


def _win_probability(ef_signal: Dict[str, Any]) -> float:
    """
    Per-signal win probability for Kelly sizing.

    Order of preference:
      1. ml.calibrator.predict(ef_signal) — if a promoted model exists
      2. EdgeFinder /api/stats live rate for this signal_type
      3. Refined static rate keyed by (signal_type, pattern)
    """
    # Layer 1 — trained calibrator
    try:
        from ml.calibrator import predict as calibrator_predict
        p = calibrator_predict(ef_signal)
        if p is not None and 0.0 < p < 1.0:
            return p
    except Exception:
        pass  # never let calibrator failure block sizing

    sig_type = ef_signal.get("signal_type", "unknown")
    pattern  = (ef_signal.get("pattern") or "").lower()

    # Layer 3 key: prefer pattern-aware bucket for crash signals
    if sig_type == "crash" and "extreme" in pattern:
        static_key = "extreme_crash"
    elif sig_type == "crash":
        static_key = "momentum_crash"
    else:
        static_key = sig_type

    # Layer 2 — live published rate
    live = edgefinder_client.stats_win_rate(sig_type)
    if live > 0.0:
        return live

    # Layer 3 — static refined rate
    return _DEFAULT_WIN_RATES.get(static_key, _DEFAULT_WIN_RATES["unknown"])


# ── Main entrypoint ──────────────────────────────────────────────────────────

def generate_signals(_unused: Any = None) -> List[Dict[str, Any]]:
    """
    Pull pending EdgeFinder signals and emit a row to the `signals` table for
    each one that passes the seven gates. Returns the list of taken signals,
    each ready for paper_trader.execute_signals.

    The `_unused` parameter is for backwards compatibility with the v1
    `generate_signals(markets)` call site in scheduler.py / app.py. v2 doesn't
    need scanned markets to generate signals.
    """
    # Refresh stats first so Kelly uses the latest published win rate
    edgefinder_client.fetch_stats()

    pending = edgefinder_client.pending_signals()
    if not pending:
        db.log_message("INFO", "EdgeFinder: no pending signals")
        return []

    bankroll        = db.get_cash_balance() + _open_position_cost()
    starting_bank   = config.STARTING_BALANCE
    open_cost       = _open_position_cost()
    reserve_floor   = starting_bank * config.RESERVE_PCT
    available_cash  = db.get_cash_balance() - max(0.0, reserve_floor - (starting_bank - open_cost - db.get_cash_balance()))

    taken: List[Dict[str, Any]] = []
    skipped_counts: Dict[str, int] = {}
    seen_this_cycle: set = set()   # ids we've already evaluated this cycle
    taken_this_cycle: set = set()  # ids we've already decided to take this cycle

    # Defense-in-depth: dedupe pending list by id before iteration
    deduped_pending: List[Dict[str, Any]] = []
    for ef in pending:
        ef_id = ef.get("id", "")
        if ef_id and ef_id not in seen_this_cycle:
            seen_this_cycle.add(ef_id)
            deduped_pending.append(ef)

    for ef in deduped_pending:
        ef_id = ef.get("id", "")
        # Belt-and-braces: even if dedupe missed, refuse to evaluate the same id twice
        if ef_id in taken_this_cycle:
            skipped_counts["skipped:dup_in_cycle"] = skipped_counts.get("skipped:dup_in_cycle", 0) + 1
            continue
        result = _evaluate(ef, bankroll=bankroll, available_cash=available_cash)
        # Record what we did with the EdgeFinder signal regardless of outcome
        db.mark_edgefinder_action(ef["id"], result.reason)

        if not result.take:
            skipped_counts[result.reason] = skipped_counts.get(result.reason, 0) + 1
            continue

        row = _emit_signal_row(ef, result)
        if row is not None:
            taken.append(row)
            taken_this_cycle.add(ef_id)
            # Reduce available cash by what we just committed
            if result.kelly_outcome:
                available_cash -= sizing.dollar_size(result.kelly_outcome, bankroll)

    db.log_message(
        "INFO",
        f"EdgeFinder cycle: {len(pending)} pending / "
        f"{len(taken)} taken / {len(pending) - len(taken)} skipped "
        f"({_format_skipped(skipped_counts)})"
    )
    return taken


# ── Per-signal evaluation (the seven gates) ──────────────────────────────────

def _evaluate(ef: Dict[str, Any], *, bankroll: float, available_cash: float) -> GateResult:
    sig_id   = ef.get("id", "")
    yes_pub  = float(ef.get("yes_price") or 0.0)
    sig_type = ef.get("signal_type", "unknown")
    detected = ef.get("detected_at") or ef.get("date_added", "")

    # Gate 1 — status
    if ef.get("status") != "pending":
        return GateResult(False, f"skipped:not_pending_{ef.get('status')}")

    # Gate 2 — already taken
    if _position_exists_for_signal(sig_id):
        return GateResult(False, "skipped:already_open")

    # Gate 5 — signal age (cheap, do early)
    age_hours = _signal_age_hours(detected)
    if age_hours is not None and age_hours > config.MAX_SIGNAL_AGE_HOURS:
        return GateResult(False, f"skipped:stale_signal_{age_hours:.0f}h")

    # Gates 3 + 4 require Polymarket lookup
    detail = polymarket_lookup.resolve_signal(
        ef.get("market_url") or ef.get("url", ""),
        ef.get("question", ""),
    )
    if not detail:
        return GateResult(False, "skipped:lookup_failed")

    # Gate 4 — volume floor
    if detail.volume_24hr < config.MIN_VOLUME_24HR:
        return GateResult(False, f"skipped:thin_volume_{int(detail.volume_24hr)}")

    # Gate 3 — price drift
    # Compare EdgeFinder's published yes_price to the current YES mid (or implied from NO ask)
    yes_now = detail.current_yes_price
    if yes_now is None and detail.current_no_ask is not None:
        yes_now = round(1.0 - detail.current_no_ask, 4)
    if yes_now is not None:
        drift = abs(yes_now - yes_pub)
        if drift > config.MAX_PRICE_DRIFT:
            return GateResult(False, f"skipped:price_drift_{drift:.3f}")
    # If we couldn't get a live YES price, soft-pass and trust EdgeFinder's published

    # Gate 6 — fee-adjusted Kelly EV
    # Pass the full EdgeFinder signal so the calibrator can extract features.
    p = _win_probability(ef)
    kelly = sizing.size_no_bet(
        yes_price=yes_now if yes_now is not None else yes_pub,
        win_probability=p,
        bankroll=bankroll,
    )
    if kelly.refuse:
        return GateResult(False, f"skipped:{(kelly.refuse_reason or 'kelly_refused')[:50]}")

    # Gate 7 — concentration / reserve / cluster cap
    dollars = sizing.dollar_size(kelly, bankroll)
    if dollars <= 0:
        return GateResult(False, "skipped:zero_size")
    if dollars > config.MAX_TRADE_SIZE:
        # Soft ceiling — clip rather than refuse
        dollars = config.MAX_TRADE_SIZE
    if dollars > available_cash:
        return GateResult(False, "skipped:reserve_breach")

    # Cluster cap: reject if adding this trade would push same-window exposure above
    # CORRELATION_CLUSTER_CAP_PCT × starting_bank. Positions resolving within ±7 days
    # of each other are correlated — if one event surprises the market, related ones move.
    if config.CORRELATION_CLUSTER_CAP_PCT > 0:
        # EF API doesn't expose end_date; pull it from the Gamma raw_market we already fetched.
        end_date = detail.raw_market.get("end_date") or detail.raw_market.get("endDate", "")
        cluster_already = db.get_cluster_exposure(end_date)
        cluster_cap_dollars = config.STARTING_BALANCE * config.CORRELATION_CLUSTER_CAP_PCT
        if cluster_already + dollars > cluster_cap_dollars:
            return GateResult(
                False,
                f"skipped:cluster_cap_{cluster_already + dollars:.0f}>${cluster_cap_dollars:.0f}"
            )

    return GateResult(
        take=True,
        reason="taken",
        yes_price_now=yes_now,
        no_ask_now=detail.current_no_ask,
        volume_24hr=detail.volume_24hr,
        kelly_outcome=kelly,
        token_id_no=detail.token_id_no,
    )


# ── Signal-row emission ──────────────────────────────────────────────────────

def _emit_signal_row(ef: Dict[str, Any], gate: GateResult) -> Optional[Dict[str, Any]]:
    """Persist a row in `signals` and return the dict paper_trader expects."""
    yes_now  = gate.yes_price_now if gate.yes_price_now is not None else float(ef.get("yes_price") or 0.0)
    no_entry = gate.no_ask_now if gate.no_ask_now is not None else round(1.0 - yes_now, 4)
    if not (0.0 < no_entry < 1.0):
        return None

    age_seconds = _signal_age_seconds(ef.get("detected_at") or ef.get("date_added", ""))
    sig_type    = ef.get("signal_type", "unknown")
    pattern     = ef.get("pattern") or ""
    reason_tag  = f"EDGEFINDER {sig_type.upper()}"
    if pattern:
        reason_tag += f" / {pattern}"
    reason_tag += f" | YES {yes_now:.3f} → NO {no_entry:.3f} | p={gate.kelly_outcome.p:.3f}"

    market_id = f"ef:{ef.get('id', '')}"
    token_id  = gate.token_id_no or f"ef-no:{ef.get('id', '')}"

    row = {
        "market_id":  market_id,
        "token_id":   token_id,
        "question":   ef.get("question", ""),
        "outcome":    "No",
        "signal":     f"EF-{sig_type.upper()}",
        "price":      no_entry,
        "confidence": gate.kelly_outcome.p,
        "reason":     reason_tag,
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
        # v2 extra fields
        "yes_price_at_emit":          yes_now,
        "signal_age_at_emit_seconds": age_seconds or 0,
        "fee_adjusted_ev":            gate.kelly_outcome.ev_per_dollar,
        "edgefinder_signal_id":       ef.get("id", ""),
        # for paper_trader sizing
        "_kelly":     gate.kelly_outcome,
        "_pattern":   pattern,
        "_signal_type": sig_type,
        "_market_url": ef.get("market_url") or ef.get("url", ""),
    }

    row_id = db.save_signal(row)
    row["id"] = row_id
    return row


# ── Helpers ──────────────────────────────────────────────────────────────────

def _position_exists_for_signal(edgefinder_signal_id: str) -> bool:
    """
    True if we've EVER opened a position for this EdgeFinder signal id
    (open OR closed). Closed-counts-too prevents the stop-loss → re-entry
    death loop: once a signal stops us out, the same id can't fire again.
    """
    if not edgefinder_signal_id:
        return False
    import sqlite3
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM positions WHERE edgefinder_signal_id = ? LIMIT 1",
            (edgefinder_signal_id,),
        ).fetchone()
    return row is not None


def _open_position_cost() -> float:
    return db.get_open_position_cost()


def _signal_age_hours(detected_at: str) -> Optional[float]:
    secs = _signal_age_seconds(detected_at)
    return (secs / 3600.0) if secs is not None else None


def _signal_age_seconds(detected_at: str) -> Optional[int]:
    if not detected_at:
        return None
    try:
        # EdgeFinder timestamps are ISO-8601 without timezone (UTC naive)
        dt = datetime.fromisoformat(detected_at.replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return int(delta.total_seconds())
    except (ValueError, TypeError):
        return None


def _format_skipped(counts: Dict[str, int]) -> str:
    if not counts:
        return ""
    parts = [f"{r}={n}" for r, n in sorted(counts.items(), key=lambda x: -x[1])]
    return ", ".join(parts[:6]) + (f", +{len(parts)-6} more" if len(parts) > 6 else "")
