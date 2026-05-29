"""
Market scanner — orchestrates the full pipeline:
  Gamma API → filter → CLOB enrichment → quality gate → DB upsert
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import config
import database as db
from gamma_client import fetch_events, parse_markets
from clob_client import fetch_order_book
from liquidity_score import calculate_book_metrics

logger = logging.getLogger(__name__)


# ── Category filtering ────────────────────────────────────────────────────────

def _text(market: Dict[str, Any]) -> str:
    """Single lowercase text blob for keyword matching."""
    tags = " ".join(market.get("tags", []))
    return " ".join([
        market.get("question",  ""),
        market.get("category",  ""),
        tags,
    ]).lower()


def _is_excluded(market: Dict[str, Any]) -> bool:
    text = _text(market)
    return any(kw in text for kw in config.EXCLUDE_KEYWORDS)


def _is_targeted(market: Dict[str, Any]) -> bool:
    text = _text(market)
    return any(kw in text for kw in config.TARGET_KEYWORDS)


# ── Resolution window filter ──────────────────────────────────────────────────

def _parse_end_date(end_date_str: str) -> Optional[datetime]:
    """Parse ISO end date string into UTC-aware datetime. Returns None on failure."""
    if not end_date_str:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(end_date_str[:26], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _resolves_within_window(market: Dict[str, Any]) -> tuple[bool, str]:
    """
    Returns (passes: bool, reason: str).
    If MAX_HOURS_TO_RESOLVE == 0 the filter is disabled (all pass).
    """
    if config.MAX_HOURS_TO_RESOLVE == 0:
        return True, ""

    end_date_str = market.get("end_date", "")
    end_dt = _parse_end_date(end_date_str)

    if end_dt is None:
        return False, f"Cannot parse end_date '{end_date_str}'"

    now     = datetime.now(timezone.utc)
    cutoff  = now + timedelta(hours=config.MAX_HOURS_TO_RESOLVE)

    if end_dt <= now:
        return False, f"Market already expired ({end_date_str})"

    if end_dt > cutoff:
        hours_away = (end_dt - now).total_seconds() / 3600
        return False, f"Resolves in {hours_away:.1f}h > {config.MAX_HOURS_TO_RESOLVE}h window"

    return True, ""


# ── Main scan pipeline ────────────────────────────────────────────────────────

def scan_markets() -> List[Dict[str, Any]]:
    """
    Full market scan.  Returns a list of enriched, validated market dicts
    that passed every filter.  Also persists results to SQLite.
    """
    db.log_message("INFO", "Market scan started")

    events = fetch_events()
    if not events:
        db.log_message("WARNING", "No events returned from Gamma API")
        return []

    db.log_message("INFO", f"Fetched {len(events)} events from Gamma API")

    all_tokens = parse_markets(events)
    db.log_message("INFO", f"Parsed {len(all_tokens)} market tokens total")

    valid: List[Dict[str, Any]] = []
    rejected_count = 0

    for market in all_tokens:
        result = _process_market(market)
        if result is None:
            rejected_count += 1
        else:
            valid.append(result)
            db.upsert_market(result)

    db.log_message(
        "INFO",
        f"Scan complete — {len(valid)} valid, {rejected_count} rejected"
        f" out of {len(all_tokens)} tokens",
    )
    return valid


def _process_market(market: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Run a single market token through the full filter pipeline.
    Returns the enriched market dict on success, None on rejection.
    """
    question  = market.get("question", "(no question)")
    market_id = market.get("market_id", "")

    def reject(reason: str) -> None:
        db.log_rejected(
            market_id, question, reason,
            0, market.get("liquidity", 0), market.get("volume_24hr", 0),
        )

    # ── Hard validity checks ──
    if not market.get("token_id"):
        reject("No token ID")
        return None

    if market.get("closed") or not market.get("active", True):
        reject("Market closed or inactive")
        return None

    # ── Resolution window filter ──
    passes, reason = _resolves_within_window(market)
    if not passes:
        reject(reason)
        return None

    # ── Category filter ──
    if _is_excluded(market):
        reject("Excluded category (crypto/weather/sports)")
        return None

    # ── Volume pre-check (avoid unnecessary CLOB calls) ──
    if market.get("volume_24hr", 0) < config.MIN_VOLUME_24HR:
        reject(f"Volume ${market['volume_24hr']:.0f} < ${config.MIN_VOLUME_24HR:.0f}")
        return None

    # ── CLOB order book ──
    book = fetch_order_book(market["token_id"])
    if not book:
        reject("No order book returned from CLOB API")
        return None

    metrics = calculate_book_metrics(book)
    if not metrics:
        reject("Order book unusable (empty or crossed)")
        return None

    spread = metrics["spread"]
    if spread > config.MAX_SPREAD:
        reject(f"Spread {spread:.4f} > {config.MAX_SPREAD:.4f}")
        db.log_rejected(
            market_id, question,
            f"Spread {spread:.4f} > threshold",
            spread, market.get("liquidity", 0), market.get("volume_24hr", 0),
        )
        return None

    # ── Enrich and return ──
    enriched = {**market, **metrics, "is_targeted": _is_targeted(market)}
    return enriched
