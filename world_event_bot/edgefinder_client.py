"""
EdgeFinder API client.

Consumes the public signal feed at https://api.edgefinderai.org and mirrors
it into our SQLite store. EdgeFinder is the brain; we are the execution +
sizing + accounting layer (see _config/signal-doctrine.md).

Design notes:
- No auth; both endpoints are public.
- Polite: User-Agent identifies us, 5-minute default poll interval, exponential
  backoff on failures, cache responses for the poll window.
- Defensive: every field has a default; empty / malformed responses are logged
  but do not raise. Existing rows are upserted by signal id.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

import config
import database as db

logger = logging.getLogger(__name__)

API_BASE = "https://api.edgefinderai.org"
SIGNALS_URL = f"{API_BASE}/api/signals"
STATS_URL = f"{API_BASE}/api/stats"

# The default API call returns only ~20 most recent signals.
# Use ?limit=N to pull the full history (verified 2026-05-27 — 317 returned with limit=1000).
BACKFILL_LIMIT = 1000

USER_AGENT = "Politicas-paper-bot/1.0 (paper-only; non-commercial; iragorri.julian@gmail.com)"
HTTP_TIMEOUT = 15  # seconds
MAX_RETRIES = 3
BACKOFF_BASE_S = 2.0  # exponential: 2, 4, 8


# ── In-process cache ─────────────────────────────────────────────────────────

@dataclass
class _Cache:
    signals: Optional[List[Dict[str, Any]]] = None
    signals_fetched_at: Optional[datetime] = None
    stats: Optional[Dict[str, Any]] = None
    stats_fetched_at: Optional[datetime] = None

_cache = _Cache()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_fresh(fetched_at: Optional[datetime], max_age_s: float) -> bool:
    if fetched_at is None:
        return False
    return (_now() - fetched_at).total_seconds() < max_age_s


# ── HTTP with retry/backoff ──────────────────────────────────────────────────

def _fetch_json(url: str) -> Optional[Any]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            logger.warning("EdgeFinder %s returned HTTP %s", url, r.status_code)
        except (requests.RequestException, json.JSONDecodeError) as e:
            logger.warning("EdgeFinder fetch failed (%s): %s", url, e)

        # Don't sleep on the last attempt
        if attempt < MAX_RETRIES - 1:
            time.sleep(BACKOFF_BASE_S * (2 ** attempt))

    db.log_message("WARNING", f"EdgeFinder fetch gave up after {MAX_RETRIES} retries: {url}")
    return None


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_signals(force: bool = False) -> List[Dict[str, Any]]:
    """
    Return the latest EdgeFinder signals list. Cached for `config.EDGEFINDER_POLL_INTERVAL_S`.
    Also mirrors any fresh rows into the `edgefinder_signals` table.
    """
    if not force and _is_fresh(_cache.signals_fetched_at, config.EDGEFINDER_POLL_INTERVAL_S):
        return _cache.signals or []

    payload = _fetch_json(SIGNALS_URL)
    if payload is None:
        # Fall back to whatever's cached (better stale than nothing)
        return _cache.signals or []

    signals = payload.get("signals", []) if isinstance(payload, dict) else []
    if not isinstance(signals, list):
        logger.warning("EdgeFinder /api/signals payload missing 'signals' list")
        return _cache.signals or []

    _cache.signals = signals
    _cache.signals_fetched_at = _now()

    upserted = 0
    for s in signals:
        try:
            db.upsert_edgefinder_signal(_normalize_signal(s))
            upserted += 1
        except Exception as e:
            logger.warning("Skipping malformed EdgeFinder signal: %s", e)

    db.log_message(
        "INFO",
        f"EdgeFinder /api/signals: {len(signals)} rows ({upserted} mirrored)"
    )
    return signals


def fetch_all_signals() -> List[Dict[str, Any]]:
    """
    Bulk backfill: pull the entire signal history from the API in one call.
    Used by ml/calibrator.py to build the training set.

    Does NOT touch the in-process cache (so it can be called alongside
    the normal 5-min fetch_signals without disrupting routine polling).
    Mirrors every row into `edgefinder_signals` (upsert preserves status updates).
    """
    payload = _fetch_json(f"{SIGNALS_URL}?limit={BACKFILL_LIMIT}")
    if payload is None or not isinstance(payload, dict):
        logger.warning("fetch_all_signals: empty or malformed payload")
        return []

    signals = payload.get("signals", []) or []
    upserted = 0
    for s in signals:
        try:
            db.upsert_edgefinder_signal(_normalize_signal(s))
            upserted += 1
        except Exception as e:
            logger.warning("fetch_all_signals: skip malformed row: %s", e)

    db.log_message(
        "INFO",
        f"EdgeFinder backfill: {len(signals)} rows in payload, {upserted} mirrored"
    )
    return signals


def fetch_stats(force: bool = False) -> Dict[str, Any]:
    """
    Return EdgeFinder's published aggregate stats. Cached for `config.EDGEFINDER_POLL_INTERVAL_S`.
    Shape: { overall: {wins, losses, pending, win_rate, total_signals},
             by_type: {ml: {...}, crash: {...}, combined: {...}},
             today, day, status, last_updated }
    """
    if not force and _is_fresh(_cache.stats_fetched_at, config.EDGEFINDER_POLL_INTERVAL_S):
        return _cache.stats or {}

    payload = _fetch_json(STATS_URL)
    if payload is None:
        return _cache.stats or {}

    if not isinstance(payload, dict):
        logger.warning("EdgeFinder /api/stats payload not a dict")
        return _cache.stats or {}

    _cache.stats = payload
    _cache.stats_fetched_at = _now()

    # Mirror a snapshot row to the DB so audits can trend the published win rate
    try:
        db.insert_edgefinder_stats_snapshot(payload)
    except Exception as e:
        logger.warning("Failed to snapshot EdgeFinder stats: %s", e)

    return payload


# ── Pending-signal helpers ───────────────────────────────────────────────────

def pending_signals() -> List[Dict[str, Any]]:
    """Signals that haven't resolved yet — actionable for paper trading."""
    return [s for s in fetch_signals() if s.get("status") == "pending"]


def resolved_signals() -> List[Dict[str, Any]]:
    """Win / loss / settled signals — for trailing track-record validation."""
    return [s for s in fetch_signals() if s.get("status") in ("win", "loss")]


def stats_win_rate(signal_type: Optional[str] = None) -> float:
    """
    Published win rate. signal_type = None → overall, 'ml' / 'crash' / 'combined' for subset.
    Returns 0.0 if no stats cached.
    """
    s = fetch_stats()
    if signal_type is None:
        return float(s.get("overall", {}).get("win_rate", 0.0)) / 100.0
    return float(s.get("by_type", {}).get(signal_type, {}).get("win_rate", 0.0)) / 100.0


# ── Normalization (API shape → our DB row) ───────────────────────────────────

def _normalize_signal(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    EdgeFinder API row → our edgefinder_signals row.

    Fields we depend on (verified live 2026-05-27):
      id, question, direction, yes_price, signal_type, type, pattern,
      market_url, status, detected_at, resolved_at, free_signal_of_day
    """
    return {
        "id":                raw.get("id", ""),
        "question":          raw.get("question") or raw.get("market", ""),
        "direction":         raw.get("direction", "NO"),
        "yes_price":         _safe_float(raw.get("yes_price")),
        "signal_type":       raw.get("signal_type") or _infer_signal_type(raw.get("type")),
        "pattern":           raw.get("pattern") or "",
        "market_url":        raw.get("market_url") or raw.get("url", ""),
        "status":            raw.get("status", "pending"),
        "detected_at":       raw.get("detected_at") or raw.get("date_added", ""),
        "resolved_at":       raw.get("resolved_at"),  # may be None
        "free_signal_of_day": bool(raw.get("free_signal_of_day", False)),
        "fetched_at":        _now().isoformat(timespec="seconds"),
    }


def _infer_signal_type(type_field: Any) -> str:
    if not type_field:
        return "unknown"
    t = str(type_field).upper()
    if "CRASH" in t:
        return "crash"
    if "ML" in t:
        return "ml"
    return t.lower()


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default
