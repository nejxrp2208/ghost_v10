"""Polymarket CLOB API client — fetches order books per token."""

import logging
from typing import Any, Dict, Optional

import requests

import config

logger = logging.getLogger(__name__)


def fetch_order_book(token_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch the live order book for a CLOB token ID.
    Returns None on any error so callers can cleanly reject the market.
    """
    url = f"{config.CLOB_API_BASE}/book"
    try:
        resp = requests.get(
            url,
            params={"token_id": token_id},
            timeout=config.REQUEST_TIMEOUT_CLOB,
        )
        resp.raise_for_status()
        data = resp.json()

        # Sanity check: must have at least one side
        if not data.get("bids") and not data.get("asks"):
            logger.debug("Empty order book for token %s", token_id)
            return None

        return data

    except requests.exceptions.Timeout:
        logger.debug("CLOB timeout for token %s", token_id)
        return None
    except requests.exceptions.HTTPError as exc:
        logger.debug("CLOB HTTP error %s for token %s", exc.response.status_code, token_id)
        return None
    except requests.exceptions.RequestException as exc:
        logger.debug("CLOB request error for token %s: %s", token_id, exc)
        return None
    except ValueError:
        logger.debug("CLOB JSON parse error for token %s", token_id)
        return None


def fetch_midpoint(token_id: str) -> Optional[float]:
    """Quick helper to get the current mid-price for a token."""
    book = fetch_order_book(token_id)
    if not book:
        return None

    bids = book.get("bids", [])
    asks = book.get("asks", [])

    best_bid = _best_price(bids, highest=True)
    best_ask = _best_price(asks, highest=False)

    if best_bid is not None and best_ask is not None:
        return round((best_bid + best_ask) / 2, 4)
    return None


def _best_price(levels: list, highest: bool) -> Optional[float]:
    prices = []
    for lvl in levels:
        try:
            prices.append(float(lvl.get("price", 0)))
        except (TypeError, ValueError):
            continue
    if not prices:
        return None
    return max(prices) if highest else min(prices)


def estimate_sell_proceeds(token_id: str, size: float) -> Optional[Dict[str, Any]]:
    """
    Walk the live bid book to estimate what selling `size` shares yields right now.
    Returns None on fetch failure or empty bid side.

    Result fields:
      proceeds        dollars received
      avg_price       weighted avg fill price (= proceeds / shares_filled)
      best_bid        top-of-book bid
      shares_filled   shares actually filled (may be < size if book is thin)
      fully_filled    True iff the book had enough depth to absorb `size`
      depth_available total shares resting on the bid side
      levels_consumed how many price levels we walked to fill
    """
    if size <= 0:
        return None
    book = fetch_order_book(token_id)
    if not book:
        return None

    raw_bids = book.get("bids") or []
    bids: list = []
    for lvl in raw_bids:
        try:
            bids.append((float(lvl.get("price")), float(lvl.get("size"))))
        except (TypeError, ValueError):
            continue
    if not bids:
        return None

    bids.sort(key=lambda x: x[0], reverse=True)
    depth_available = sum(s for _, s in bids)

    remaining = float(size)
    proceeds = 0.0
    levels_consumed = 0
    for price, avail in bids:
        if remaining <= 0:
            break
        take = min(avail, remaining)
        proceeds += take * price
        remaining -= take
        levels_consumed += 1

    shares_filled = float(size) - remaining
    avg_price = proceeds / shares_filled if shares_filled > 0 else 0.0

    return {
        "proceeds":        round(proceeds, 4),
        "avg_price":       round(avg_price, 6),
        "best_bid":        round(bids[0][0], 4),
        "shares_filled":   round(shares_filled, 4),
        "fully_filled":    remaining <= 1e-6,
        "depth_available": round(depth_available, 4),
        "levels_consumed": levels_consumed,
    }
