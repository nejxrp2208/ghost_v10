"""
Spot-price reader for crypto-binary signal detection.

Source: CoinGecko free API (no auth).
  - get_spot_price(asset)              -> live price, 60s in-process cache
  - get_historical_spot_price(asset, date) -> daily close on that UTC date, persistent cache
"""

import json
import logging
import os
import time
from datetime import date as date_cls
from typing import Dict, Optional, Tuple, Union

import requests

logger = logging.getLogger(__name__)


_COINGECKO_LIVE_URL = "https://api.coingecko.com/api/v3/simple/price"
_COINGECKO_HIST_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/history"
_REQUEST_TIMEOUT = 15
_CACHE_TTL_S    = 60.0

# Persistent on-disk cache so the 44-row dataset build doesn't re-hit CoinGecko
# every run (avoids the free-tier rate limit and makes the build deterministic).
_HIST_CACHE_PATH = os.path.join(os.path.dirname(__file__), ".crypto_spot_history_cache.json")

# Asset alias → CoinGecko id
_ASSET_IDS: Dict[str, str] = {
    "bitcoin": "bitcoin",
    "btc":     "bitcoin",
    "ethereum": "ethereum",
    "eth":      "ethereum",
}

# (asset_id, vs_currency) → (price, fetched_at_epoch)
_cache: Dict[Tuple[str, str], Tuple[float, float]] = {}


def get_spot_price(asset: str, vs_currency: str = "usd") -> Optional[float]:
    """
    Return the current spot price for `asset` (e.g. 'bitcoin', 'eth') in
    `vs_currency`. Returns None on any error so callers can cleanly skip.
    """
    asset_id = _ASSET_IDS.get(asset.strip().lower())
    if asset_id is None:
        return None

    key = (asset_id, vs_currency)
    now = time.time()
    cached = _cache.get(key)
    if cached is not None and (now - cached[1]) < _CACHE_TTL_S:
        return cached[0]

    try:
        resp = requests.get(
            _COINGECKO_LIVE_URL,
            params={"ids": asset_id, "vs_currencies": vs_currency},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        price = float(data.get(asset_id, {}).get(vs_currency))
        _cache[key] = (price, now)
        return price
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        logger.debug("CoinGecko fetch failed for %s/%s: %s", asset_id, vs_currency, exc)
        return None


# ── Historical price (daily close, on-disk cache) ────────────────────────────

def _load_hist_cache() -> Dict[str, float]:
    if not os.path.exists(_HIST_CACHE_PATH):
        return {}
    try:
        with open(_HIST_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_hist_cache(cache: Dict[str, float]) -> None:
    try:
        with open(_HIST_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError as exc:
        logger.warning("Failed to write historical-spot cache: %s", exc)


# Free-tier CoinGecko allows ~10-30 calls/min. Sleep between uncached calls to
# stay under the limit. Backoff on 429s.
_HIST_INTER_CALL_SLEEP_S = 2.5
_HIST_429_BACKOFF_S      = 65.0
_HIST_MAX_RETRIES        = 3


def get_historical_spot_price(
    asset: str,
    on_date: Union[str, date_cls],
    vs_currency: str = "usd",
) -> Optional[float]:
    """
    Return the closing spot price for `asset` on the given UTC date.

    `on_date` may be a `date` object or an ISO string ('YYYY-MM-DD' or
    full ISO datetime — only the date portion is used).

    Cached on disk — repeat lookups for the same (asset, date) are free and
    don't sleep. On a cache miss, this sleeps `_HIST_INTER_CALL_SLEEP_S`
    seconds before the HTTP call and backs off on 429 rate-limit responses.

    Returns None on any non-recoverable failure.
    """
    asset_id = _ASSET_IDS.get(asset.strip().lower())
    if asset_id is None:
        return None

    # Normalize to dd-mm-yyyy (CoinGecko's expected format).
    if isinstance(on_date, str):
        try:
            d = date_cls.fromisoformat(on_date[:10])
        except ValueError:
            return None
    else:
        d = on_date
    cg_date = d.strftime("%d-%m-%Y")

    cache_key = f"{asset_id}|{vs_currency}|{cg_date}"
    cache = _load_hist_cache()
    if cache_key in cache:
        return cache[cache_key]

    url = _COINGECKO_HIST_URL.format(coin_id=asset_id)
    params = {"date": cg_date, "localization": "false"}

    for attempt in range(_HIST_MAX_RETRIES):
        time.sleep(_HIST_INTER_CALL_SLEEP_S)
        try:
            resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.debug("CoinGecko network error %s on %s: %s", asset_id, cg_date, exc)
            return None

        if resp.status_code == 429:
            logger.info(
                "CoinGecko rate-limited (429) for %s on %s, sleeping %.0fs (attempt %d/%d)",
                asset_id, cg_date, _HIST_429_BACKOFF_S, attempt + 1, _HIST_MAX_RETRIES,
            )
            time.sleep(_HIST_429_BACKOFF_S)
            continue

        if not resp.ok:
            logger.debug("CoinGecko HTTP %s for %s on %s", resp.status_code, asset_id, cg_date)
            return None

        try:
            data = resp.json()
            price = float(data["market_data"]["current_price"][vs_currency])
        except (ValueError, TypeError, KeyError) as exc:
            logger.debug("CoinGecko parse error for %s on %s: %s", asset_id, cg_date, exc)
            return None

        cache[cache_key] = price
        _save_hist_cache(cache)
        return price

    return None
