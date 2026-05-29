"""Polymarket Gamma API client — fetches and parses active events."""

import json
import logging
from typing import Any, Dict, List

import requests

import config

logger = logging.getLogger(__name__)


def fetch_events() -> List[Dict[str, Any]]:
    """Fetch up to EVENTS_LIMIT active, open events ordered by 24h volume."""
    url = f"{config.GAMMA_API_BASE}/events"
    params = {
        "active":    "true",
        "closed":    "false",
        "order":     "volume_24hr",
        "ascending": "false",
        "limit":     config.EVENTS_LIMIT,
    }
    try:
        resp = requests.get(url, params=params, timeout=config.REQUEST_TIMEOUT_GAMMA)
        resp.raise_for_status()
        data = resp.json()
        # API may return a list directly or {"events": [...]}
        if isinstance(data, list):
            return data
        return data.get("events", data.get("data", []))
    except requests.exceptions.Timeout:
        logger.error("Gamma API request timed out")
        return []
    except requests.exceptions.RequestException as exc:
        logger.error("Gamma API request failed: %s", exc)
        return []
    except ValueError as exc:
        logger.error("Gamma API JSON parse error: %s", exc)
        return []


def parse_markets(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Flatten events → individual market-token records.
    Each YES/NO token becomes its own record so the CLOB can be queried per token.
    """
    records: List[Dict[str, Any]] = []

    for event in events:
        event_category = event.get("category", "")
        event_tags = _parse_tags(event.get("tags", []))

        for market in event.get("markets", []):
            # clobTokenIds may arrive as a JSON string or a real list
            token_ids = _parse_json_field(market.get("clobTokenIds", "[]"))
            outcomes  = _parse_json_field(market.get("outcomes",     "[]"))

            if not token_ids:
                continue

            try:
                vol   = float(market.get("volume24hr", 0) or 0)
                liq   = float(market.get("liquidity",  0) or 0)
            except (TypeError, ValueError):
                vol, liq = 0.0, 0.0

            active = market.get("active", True)
            closed = market.get("closed", False)
            # Some fields use different key names across API versions
            end_date = (
                market.get("endDate")
                or market.get("end_date_iso")
                or market.get("endDateIso")
                or ""
            )

            for i, token_id in enumerate(token_ids):
                outcome = outcomes[i] if i < len(outcomes) else f"Outcome {i + 1}"
                records.append(
                    {
                        "market_id":   str(market.get("id", "")),
                        "question":    market.get("question", "").strip(),
                        "category":    event_category,
                        "tags":        event_tags,
                        "token_id":    str(token_id).strip(),
                        "outcome":     str(outcome),
                        "volume_24hr": vol,
                        "liquidity":   liq,
                        "end_date":    end_date,
                        "active":      bool(active),
                        "closed":      bool(closed),
                    }
                )

    return records


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_json_field(value: Any) -> List:
    """Return a list whether value is already a list or a JSON string."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _parse_tags(tags: Any) -> List[str]:
    """Normalise tags into a flat list of lowercase label strings."""
    if isinstance(tags, list):
        result = []
        for t in tags:
            if isinstance(t, dict):
                result.append(t.get("label", t.get("slug", "")).lower())
            elif isinstance(t, str):
                result.append(t.lower())
        return result
    return []
