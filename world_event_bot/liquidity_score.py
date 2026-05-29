"""
Pure calculation functions for order-book metrics, liquidity scores,
and market quality scores.  No I/O, no side effects.
"""

from typing import Any, Dict, List, Optional, Tuple


# ── Order book parsing ────────────────────────────────────────────────────────

def _parse_levels(raw: List[Any]) -> List[Tuple[float, float]]:
    """Convert raw bid/ask level dicts → sorted (price, size) tuples."""
    result: List[Tuple[float, float]] = []
    for lvl in raw:
        try:
            price = float(lvl.get("price", 0))
            size  = float(lvl.get("size",  0))
            if price > 0 and size > 0:
                result.append((price, size))
        except (TypeError, ValueError):
            continue
    return result


# ── Core metric calculation ───────────────────────────────────────────────────

def calculate_book_metrics(order_book: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Derive all relevant metrics from a raw CLOB order book dict.
    Returns None when the book is unusable (both sides empty after parsing).

    Returned keys:
        best_bid, best_ask, spread, midpoint
        top3_bid_depth, top3_ask_depth
        visible_bid_depth, visible_ask_depth, total_depth
        liquidity_score  (0–100)
        quality_score    (0–100)
    """
    bids_raw = order_book.get("bids", [])
    asks_raw = order_book.get("asks", [])

    bids = sorted(_parse_levels(bids_raw), key=lambda x: x[0], reverse=True)  # highest first
    asks = sorted(_parse_levels(asks_raw), key=lambda x: x[0])                 # lowest first

    if not bids or not asks:
        return None

    best_bid = bids[0][0]
    best_ask = asks[0][0]

    if best_ask <= best_bid:
        return None  # crossed book — stale or invalid

    spread   = round(best_ask - best_bid, 6)
    midpoint = round((best_bid + best_ask) / 2, 6)

    top3_bid_depth = round(sum(sz for _, sz in bids[:3]), 2)
    top3_ask_depth = round(sum(sz for _, sz in asks[:3]), 2)

    visible_bid_depth = round(sum(sz for _, sz in bids), 2)
    visible_ask_depth = round(sum(sz for _, sz in asks), 2)
    total_depth       = round(visible_bid_depth + visible_ask_depth, 2)

    liquidity_score = _liquidity_score(spread, total_depth, visible_bid_depth, visible_ask_depth)
    quality_score   = _quality_score(liquidity_score, spread, total_depth)

    return {
        "best_bid":           best_bid,
        "best_ask":           best_ask,
        "spread":             spread,
        "midpoint":           midpoint,
        "top3_bid_depth":     top3_bid_depth,
        "top3_ask_depth":     top3_ask_depth,
        "visible_bid_depth":  visible_bid_depth,
        "visible_ask_depth":  visible_ask_depth,
        "total_depth":        total_depth,
        "liquidity_score":    round(liquidity_score, 2),
        "quality_score":      round(quality_score, 2),
    }


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _liquidity_score(
    spread: float,
    total_depth: float,
    bid_depth: float,
    ask_depth: float,
) -> float:
    """
    Score 0–100 weighing:
      40 pts  tight spread  (0 spread = 40, spread=0.1 = 0)
      40 pts  total depth   (saturates at $10k each side)
      20 pts  book balance  (equal bid/ask depth = 20)
    """
    spread_score  = max(0.0, (1 - spread / 0.10)) * 40
    depth_score   = min(40.0, (total_depth / 20_000) * 40)
    balance       = 1 - abs(bid_depth - ask_depth) / max(total_depth, 1)
    balance_score = balance * 20
    return spread_score + depth_score + balance_score


def _quality_score(
    liquidity_score: float,
    spread: float,
    total_depth: float,
) -> float:
    """
    Market quality = liquidity_score with bonus points for excellence.
    Capped at 100.
    """
    score = liquidity_score
    if spread < 0.02:
        score += 8
    if spread < 0.01:
        score += 4
    if total_depth > 10_000:
        score += 5
    if total_depth > 50_000:
        score += 3
    return min(100.0, score)
