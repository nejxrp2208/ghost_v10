"""
Our own signal engine — rule-based EdgeFinder replica (v1).

This is the deterministic baseline before we have data for an ML model.
It implements the rules EdgeFinder published in M11 + M41 + M51 + M67:

  - YES price in 0.30-0.55 range (the structurally overpriced band)
  - Momentum crash precondition: YES down ≥ 15% in last 7 days (CRASH)
                                 or YES down ≥ 30% (EXTREME_CRASH)
  - Volume ≥ MIN_VOLUME_24HR ($50K default)
  - Spread reasonable (≤ 0.04)
  - Time to resolution between 1 hour and 30 days

When all conditions fire, we emit a row in `our_signals` with `direction = NO`.
The bot can then either:
  - Compare our pick to EdgeFinder's pending list (agreement scoring), or
  - Eventually paper-trade our signals once we trust them.

This is intentionally simple. No ML, no probabilistic outputs. Predicates only.
The model layer comes later — see ml/ directory.
"""

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import config
import database as db
from clob_client import fetch_order_book
from crypto_spot import get_spot_price

logger = logging.getLogger(__name__)


# ── Rule thresholds (single source of truth — also in _config/our-engine-rules.md) ──

YES_ENTRY_MIN     = 0.30
YES_ENTRY_MAX     = 0.55
CRASH_PCT_7D      = 0.15   # ≥15% drop in 7 days = "Momentum Crash"
EXTREME_CRASH_PCT = 0.30   # ≥30% drop in 7 days = "Extreme Crash"
MIN_TIME_TO_RESOLVE_H = 1
MAX_TIME_TO_RESOLVE_H = 24 * 30   # 30 days
MAX_SPREAD            = 0.04

# Confidence (probability) we attach to each pattern. Calibrate later via Phase C.
_BASE_CONFIDENCE = {
    "extreme_crash":   0.62,   # EdgeFinder's Day-59 crash WR is 58.9%; we'll start conservative
    "momentum_crash":  0.58,
    "structural":      0.55,   # YES in the band but no observed crash — weakest signal
    "crypto_binary":   0.60,   # M75: 5-in-a-row published wins on threshold binaries
}


# Crypto-binary detection (M75) — shadow-only pattern, never fed to paper_trader.
CRYPTO_BINARY_YES_MIN     = 0.30
CRYPTO_BINARY_YES_MAX     = 0.40
CRYPTO_BINARY_MIN_TTR_H   = 1
CRYPTO_BINARY_MAX_TTR_H   = 48
CRYPTO_BINARY_MIN_GAP_PCT = 0.05   # threshold must sit ≥5% above current spot
CRYPTO_BINARY_MIN_VOL_USD = 100_000

# Matches: "Will Bitcoin be above $76,000 on May 27?"
#          "Will the price of Bitcoin be above $76,000 on May 27?"
#          "Will BTC be at or above $100k on Dec 1?"
_CRYPTO_BINARY_RE = re.compile(
    r"will\s+(?:the\s+price\s+of\s+)?(?P<asset>bitcoin|ethereum|btc|eth)\s+be\s+"
    r"(?:at\s+or\s+)?(?:above|over)\s+\$?\s*"
    r"(?P<threshold>[\d,]+(?:\.\d+)?)\s*(?P<suffix>[kKmM])?",
    re.IGNORECASE,
)

_SUFFIX_MULT = {"k": 1_000.0, "K": 1_000.0, "m": 1_000_000.0, "M": 1_000_000.0}


# ── Public API ───────────────────────────────────────────────────────────────

def run_cycle(scanned_markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Snapshot every scanned market into `market_snapshots`, then evaluate each
    against our rules. Emits rows in `our_signals` for everything that fires.
    Returns the new signals list.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

    for market in scanned_markets:
        _record_snapshot(market, captured_at=now)

    fired: List[Dict[str, Any]] = []
    for market in scanned_markets:
        sig = _evaluate(market, captured_at=now)
        if sig is None:
            continue
        sig["id"] = _insert_our_signal(sig)
        fired.append(sig)

    if fired:
        db.log_message(
            "INFO",
            f"Our engine fired {len(fired)} signals "
            f"({sum(1 for s in fired if s['pattern']=='extreme_crash')} extreme, "
            f"{sum(1 for s in fired if s['pattern']=='momentum_crash')} momentum)"
        )
    # Cross-check against EdgeFinder pending list and tag agreement
    _tag_agreement(fired)
    return fired


# ── Snapshot recording ───────────────────────────────────────────────────────

def _record_snapshot(market: Dict[str, Any], captured_at: str) -> None:
    """Append one row per market per cycle. Cheap; ML training fodder."""
    try:
        with sqlite3.connect(config.DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO market_snapshots
                  (market_id, token_id, question, outcome,
                   yes_price, no_price, spread, volume_24hr, liquidity,
                   bid_depth, ask_depth, end_date, captured_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    market.get("market_id", ""),
                    market.get("token_id", ""),
                    market.get("question", ""),
                    market.get("outcome", ""),
                    _yes_price_of(market),
                    _no_price_of(market),
                    market.get("spread"),
                    market.get("volume_24hr"),
                    market.get("liquidity"),
                    market.get("top3_bid_depth"),
                    market.get("top3_ask_depth"),
                    market.get("end_date", ""),
                    captured_at,
                ),
            )
            conn.commit()
    except sqlite3.OperationalError as e:
        logger.warning("market_snapshots not present (run migration 002): %s", e)


def _yes_price_of(market: Dict[str, Any]) -> Optional[float]:
    """Polymarket markets index YES at outcomes[0]. Our scanner stores midpoint per token."""
    outcome = (market.get("outcome") or "").strip().lower()
    mid = market.get("midpoint")
    if mid is None:
        return None
    if outcome in ("yes", "true"):
        return float(mid)
    if outcome in ("no", "false"):
        return round(1.0 - float(mid), 4)
    return float(mid)   # unknown — pass through


def _no_price_of(market: Dict[str, Any]) -> Optional[float]:
    yes = _yes_price_of(market)
    return round(1.0 - yes, 4) if yes is not None else None


# ── Rule evaluation ──────────────────────────────────────────────────────────

@dataclass
class _CrashCheck:
    crashed: bool
    pct_change_7d: float        # negative = YES dropped
    severity: str               # 'extreme_crash' | 'momentum_crash' | 'flat'


def _evaluate(market: Dict[str, Any], captured_at: str) -> Optional[Dict[str, Any]]:
    """Return a signal dict if this market trips our rules, else None."""
    yes_price = _yes_price_of(market)
    if yes_price is None:
        return None

    # Try the crypto-binary path first (M75). Distinct band + TTR + needs spot lookup.
    cb_features = _check_crypto_binary(market, yes_price)
    if cb_features is not None:
        return {
            "market_id":   market.get("market_id", ""),
            "token_id":    market.get("token_id", ""),
            "question":    market.get("question", ""),
            "direction":   "NO",
            "yes_price":   yes_price,
            "signal_kind": "rule_v1_shadow",   # shadow-only — never feeds paper_trader
            "pattern":     "crypto_binary",
            "features_json": json.dumps(cb_features),
            "confidence":  _BASE_CONFIDENCE["crypto_binary"],
            "status":      "pending",
            "detected_at": captured_at,
        }

    # Fall through to the original v1 path (band 0.30-0.55 + crash gate).
    if not (YES_ENTRY_MIN <= yes_price <= YES_ENTRY_MAX):
        return None

    spread = market.get("spread")
    if spread is not None and float(spread) > MAX_SPREAD:
        return None

    volume = float(market.get("volume_24hr") or 0.0)
    if volume < config.MIN_VOLUME_24HR:
        return None

    hours_left = _hours_to_resolution(market.get("end_date", ""))
    if hours_left is None or not (MIN_TIME_TO_RESOLVE_H <= hours_left <= MAX_TIME_TO_RESOLVE_H):
        return None

    crash = _check_crash(market.get("token_id", ""), yes_price)
    if crash.severity == "flat":
        # No crash → only fire the (weakest) "structural" signal if YES is high
        if yes_price < 0.45:
            return None
        pattern = "structural"
    else:
        pattern = crash.severity   # 'extreme_crash' or 'momentum_crash'

    confidence = _BASE_CONFIDENCE[pattern]

    features = {
        "yes_price":       yes_price,
        "spread":          float(spread) if spread is not None else None,
        "volume_24hr":     volume,
        "hours_to_resolve": round(hours_left, 2),
        "pct_change_7d":   round(crash.pct_change_7d, 4),
        "crash_severity":  crash.severity,
    }

    return {
        "market_id":   market.get("market_id", ""),
        "token_id":    market.get("token_id", ""),
        "question":    market.get("question", ""),
        "direction":   "NO",
        "yes_price":   yes_price,
        "signal_kind": "rule_v1",
        "pattern":     pattern,
        "features_json": json.dumps(features),
        "confidence":  confidence,
        "status":      "pending",
        "detected_at": captured_at,
    }


# ── Crypto-binary detection (M75) — SHADOW MODE ONLY ─────────────────────────
# Signals fired here are tagged signal_kind='rule_v1_shadow' and pattern='crypto_binary'.
# signal_engine.py and paper_trader.py do NOT read our_signals — promotion to real
# trading is gated on accumulating enough fired+resolved shadow signals to confirm
# the rule reproduces EdgeFinder's published WR.

def _parse_crypto_binary_title(question: str) -> Optional[Tuple[str, float]]:
    """Extract (asset, threshold_usd) from a Polymarket crypto-binary title, or None."""
    if not question:
        return None
    m = _CRYPTO_BINARY_RE.search(question)
    if not m:
        return None
    asset = m.group("asset").lower()
    try:
        threshold = float(m.group("threshold").replace(",", ""))
    except (TypeError, ValueError):
        return None
    suffix = m.group("suffix")
    if suffix:
        threshold *= _SUFFIX_MULT[suffix]
    if threshold <= 0:
        return None
    return asset, threshold


def _check_crypto_binary(market: Dict[str, Any], yes_price: float) -> Optional[Dict[str, Any]]:
    """
    Return a features dict if the market matches the M75 crypto-binary pattern, else None.

    Gates (all must pass):
      - Title matches "Will <BTC|ETH> be above $X..."
      - YES price ∈ [0.30, 0.40]
      - Resolution in 1–48h
      - Liquidity ≥ $100K notional (uses volume_24hr as proxy)
      - Spread ≤ MAX_SPREAD (same quality gate as v1)
      - Threshold sits ≥5% above current CoinGecko spot
    """
    if not (CRYPTO_BINARY_YES_MIN <= yes_price <= CRYPTO_BINARY_YES_MAX):
        return None

    parsed = _parse_crypto_binary_title(market.get("question", ""))
    if parsed is None:
        return None
    asset, threshold_usd = parsed

    hours_left = _hours_to_resolution(market.get("end_date", ""))
    if hours_left is None or not (CRYPTO_BINARY_MIN_TTR_H <= hours_left <= CRYPTO_BINARY_MAX_TTR_H):
        return None

    volume = float(market.get("volume_24hr") or 0.0)
    if volume < CRYPTO_BINARY_MIN_VOL_USD:
        return None

    spread = market.get("spread")
    if spread is not None and float(spread) > MAX_SPREAD:
        return None

    spot = get_spot_price(asset)
    if spot is None or spot <= 0:
        return None

    gap_pct = (threshold_usd - spot) / spot
    if gap_pct < CRYPTO_BINARY_MIN_GAP_PCT:
        return None

    return {
        "yes_price":        yes_price,
        "asset":            asset,
        "threshold_usd":    threshold_usd,
        "spot_usd":         round(spot, 2),
        "gap_pct":          round(gap_pct, 4),
        "hours_to_resolve": round(hours_left, 2),
        "volume_24hr":      volume,
        "spread":           float(spread) if spread is not None else None,
        "shadow_only":      True,
    }


def _check_crash(token_id: str, current_yes_price: float) -> _CrashCheck:
    """
    Look up the YES price ~7 days ago in market_snapshots; compute pct change.
    Returns 'flat' if we don't have enough history yet (which is the case until
    snapshots accumulate).
    """
    if not token_id:
        return _CrashCheck(False, 0.0, "flat")

    seven_days_ago = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)).isoformat(timespec="seconds")
    try:
        with sqlite3.connect(config.DB_PATH) as conn:
            row = conn.execute(
                """
                SELECT yes_price FROM market_snapshots
                WHERE token_id = ? AND captured_at <= ?
                ORDER BY captured_at DESC LIMIT 1
                """,
                (token_id, seven_days_ago),
            ).fetchone()
    except sqlite3.OperationalError:
        return _CrashCheck(False, 0.0, "flat")

    if not row or row[0] is None:
        return _CrashCheck(False, 0.0, "flat")

    yes_then = float(row[0])
    if yes_then <= 0:
        return _CrashCheck(False, 0.0, "flat")

    pct = (current_yes_price - yes_then) / yes_then
    if pct <= -EXTREME_CRASH_PCT:
        return _CrashCheck(True, pct, "extreme_crash")
    if pct <= -CRASH_PCT_7D:
        return _CrashCheck(True, pct, "momentum_crash")
    return _CrashCheck(False, pct, "flat")


# ── DB write ─────────────────────────────────────────────────────────────────

def _insert_our_signal(sig: Dict[str, Any]) -> int:
    with sqlite3.connect(config.DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO our_signals
              (market_id, token_id, question, direction, yes_price,
               signal_kind, pattern, features_json, confidence,
               status, detected_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sig["market_id"], sig["token_id"], sig["question"], sig["direction"],
                sig["yes_price"], sig["signal_kind"], sig["pattern"],
                sig["features_json"], sig["confidence"],
                sig["status"], sig["detected_at"],
            ),
        )
        conn.commit()
        return cur.lastrowid


# ── Agreement tagging ────────────────────────────────────────────────────────

def _tag_agreement(our_signals: List[Dict[str, Any]]) -> None:
    """
    For each new signal we fired, check whether EdgeFinder has a pending signal
    on the same Polymarket market (matching by question substring, since
    EdgeFinder's market_id format differs).
    """
    if not our_signals:
        return
    ef_pending = db.get_edgefinder_signals(status="pending", limit=200)
    ef_questions = {(s.get("question") or "").strip().lower(): s.get("id") for s in ef_pending}

    with sqlite3.connect(config.DB_PATH) as conn:
        for sig in our_signals:
            q = (sig["question"] or "").strip().lower()
            agreed = q in ef_questions
            conn.execute(
                "UPDATE our_signals SET agreement=? WHERE id=?",
                ("agree" if agreed else "edgefinder_silent", sig["id"]),
            )
        conn.commit()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _hours_to_resolution(end_date_iso: str) -> Optional[float]:
    if not end_date_iso:
        return None
    try:
        dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        return max(0.0, delta.total_seconds() / 3600.0)
    except (ValueError, TypeError):
        return None
