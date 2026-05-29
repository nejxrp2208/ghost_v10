"""
Polymarket lookup helper — resolve an EdgeFinder signal to a tradeable market.

EdgeFinder gives us `market_url` and `question`. To paper-fill cleanly we need:
  - the CLOB `token_id` for the NO outcome
  - the current best ask on NO (for price-drift gate + paper fill)
  - the 24h volume on the underlying market (for the volume gate)

This module bridges that. Cached for 60s per market so a single signal-evaluation
pass doesn't hammer Gamma + CLOB.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

import config
from clob_client import fetch_order_book

logger = logging.getLogger(__name__)

# In-process cache: slug → (timestamp, MarketDetail)
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_S = 60.0


@dataclass
class MarketDetail:
    """Everything we need to evaluate one EdgeFinder signal against Polymarket."""
    event_slug: str
    question: str
    token_id_yes: str = ""
    token_id_no:  str = ""
    volume_24hr: float = 0.0
    liquidity:   float = 0.0
    current_yes_price: Optional[float] = None
    current_no_ask:    Optional[float] = None
    current_no_bid:    Optional[float] = None
    raw_market: Dict[str, Any] = field(default_factory=dict)


# ── Public API ───────────────────────────────────────────────────────────────

def resolve_signal(market_url: str, question: str) -> Optional[MarketDetail]:
    """
    Look up the Polymarket market for an EdgeFinder signal.
    Returns None on any failure — caller decides what to do (typically: skip the signal).
    """
    slug = _extract_slug(market_url)
    if not slug:
        return None

    # Cache hit?
    cached = _CACHE.get(slug)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_S:
        return cached[1]

    event = _fetch_event_by_slug(slug)
    if not event:
        return None

    market = _pick_market_for_question(event, question)
    if not market:
        return None

    detail = MarketDetail(
        event_slug=slug,
        question=market.get("question", question),
        volume_24hr=_safe_float(market.get("volumeNum")),
        liquidity=_safe_float(market.get("liquidityNum")),
        raw_market=market,
    )

    token_ids = _decode_token_ids(market.get("clobTokenIds"))
    outcomes  = _decode_outcomes(market.get("outcomes"))

    # Polymarket convention: outcomes[0] = Yes token, outcomes[1] = No token
    if len(token_ids) >= 2 and len(outcomes) >= 2:
        yes_idx = _index_of(outcomes, ("yes", "true"))
        no_idx  = _index_of(outcomes, ("no", "false"))
        if yes_idx is not None:
            detail.token_id_yes = token_ids[yes_idx]
        if no_idx is not None:
            detail.token_id_no = token_ids[no_idx]

    # Live order-book prices
    if detail.token_id_no:
        book_no = fetch_order_book(detail.token_id_no)
        if book_no:
            detail.current_no_ask = _best_ask(book_no)
            detail.current_no_bid = _best_bid(book_no)
    if detail.token_id_yes:
        book_yes = fetch_order_book(detail.token_id_yes)
        if book_yes:
            ask = _best_ask(book_yes)
            bid = _best_bid(book_yes)
            if ask is not None and bid is not None:
                detail.current_yes_price = round((ask + bid) / 2, 4)

    _CACHE[slug] = (time.time(), detail)
    return detail


def current_yes_price(market_url: str, question: str) -> Optional[float]:
    """Lightweight helper for live stop-loss checks."""
    detail = resolve_signal(market_url, question)
    return detail.current_yes_price if detail else None


# ── Gamma fetch ──────────────────────────────────────────────────────────────

def _fetch_event_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    url = f"{config.GAMMA_API_BASE}/events"
    try:
        resp = requests.get(
            url,
            params={"slug": slug, "limit": 1},
            timeout=config.REQUEST_TIMEOUT_GAMMA,
        )
        resp.raise_for_status()
        events = resp.json()
        if isinstance(events, list) and events:
            return events[0]
        if isinstance(events, dict) and events.get("data"):
            return events["data"][0] if events["data"] else None
    except (requests.RequestException, ValueError) as e:
        logger.debug("Gamma lookup failed for slug=%s: %s", slug, e)
    return None


def _pick_market_for_question(event: Dict[str, Any], question: str) -> Optional[Dict[str, Any]]:
    markets = event.get("markets", []) or []
    if not markets:
        return None
    if len(markets) == 1:
        return markets[0]
    # Multi-market event — pick the one whose question matches best
    qn = question.strip().lower()
    for m in markets:
        if m.get("question", "").strip().lower() == qn:
            return m
    # Fuzzy fallback: longest substring overlap
    best, best_score = markets[0], 0
    for m in markets:
        mq = m.get("question", "").lower()
        score = sum(1 for word in qn.split() if word in mq)
        if score > best_score:
            best, best_score = m, score
    return best


# ── Parsing helpers ──────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"/event/([a-zA-Z0-9\-]+)")

def _extract_slug(market_url: str) -> str:
    if not market_url:
        return ""
    m = _SLUG_RE.search(market_url)
    if m:
        return m.group(1)
    # Fallback: last path component
    path = urlparse(market_url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def _decode_token_ids(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        return [str(x) for x in json.loads(raw)]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _decode_outcomes(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw]
    try:
        return [str(x).strip() for x in json.loads(raw)]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _index_of(outcomes: List[str], wanted: tuple) -> Optional[int]:
    for i, o in enumerate(outcomes):
        if o.lower() in wanted:
            return i
    return None


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _best_ask(book: Dict[str, Any]) -> Optional[float]:
    asks = book.get("asks", []) or []
    prices = []
    for lvl in asks:
        try:
            prices.append(float(lvl.get("price", 0)))
        except (TypeError, ValueError):
            continue
    return min(prices) if prices else None


def _best_bid(book: Dict[str, Any]) -> Optional[float]:
    bids = book.get("bids", []) or []
    prices = []
    for lvl in bids:
        try:
            prices.append(float(lvl.get("price", 0)))
        except (TypeError, ValueError):
            continue
    return max(prices) if prices else None
