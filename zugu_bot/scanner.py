"""
scanner.py – Polymarket Crypto UP/DOWN Scanner
Run: python scanner.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional, Tuple

import aiohttp
import websockets

try:
    from dotenv import load_dotenv as _load_dotenv
    _SD = os.path.dirname(os.path.abspath(__file__))
    _ENV_PATH = os.path.join(_SD, ".env")
    if os.path.exists(_ENV_PATH):
        _load_dotenv(_ENV_PATH, override=True)
except Exception:
    pass

# ─────────────────────────── CONSTANTS ───────────────────────────────────────

ASSETS     = ["BTC", "ETH"]
CLOB_URL   = "https://clob.polymarket.com"
GAMMA_URL  = "https://gamma-api.polymarket.com"
WS_URL     = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_URL   = "wss://ws-live-data.polymarket.com"
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream?streams="

SCAN_INTERVAL        = 0.5   # seconds
DISCOVERY_INTERVAL   = 10    # seconds
MAX_BACKOFF          = 8     # seconds
STALE_MS             = 3_000 # orderbook stale after this many ms
PRICE_STALE_MS       = 3_000 # current-price stream stale after this many ms
PRICE_NO_TICK_TIMEOUT = 15   # reconnect current-price WS if no Polymarket tick arrives
PRICE_RECONNECT_DELAY = 3    # reconnect current-price WS after this many seconds
PRICE_PING_INTERVAL   = 5    # Polymarket RTDS requires client PING messages
BINANCE_NO_TICK_TIMEOUT = 15 # reconnect Binance WS if no trade arrives
BINANCE_RECONNECT_DELAY = 3  # reconnect Binance WS after this many seconds
SUPERVISOR_RESTART_DELAY = 1 # restart crashed background loops after this many seconds

SIGNAL_WINDOWS = {"5min": 60}

CHAINLINK_PRICE_SYMBOLS: Dict[str, str] = {
    "BTC": "btc/usd",
    "ETH": "eth/usd",
}

_CHAINLINK_SYMBOL_TO_ASSET = {symbol: asset for asset, symbol in CHAINLINK_PRICE_SYMBOLS.items()}

BINANCE_PRICE_SYMBOLS: Dict[str, str] = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
}

_BINANCE_SYMBOL_TO_ASSET = {symbol: asset for asset, symbol in BINANCE_PRICE_SYMBOLS.items()}

# ── Output / logging config ───────────────────────────────────────────────────
QUIET_MODE             = os.getenv("QUIET_MODE", "true").strip().lower() not in ("false", "0", "no")
SHOW_DISCOVERY_SUMMARY = True   # print clean discovery summary on each refresh
SHOW_ACCEPT_LOGS       = False  # print individual ACCEPT lines (only if not QUIET_MODE)
DEBUG_LOGS             = False  # full debug logs (overridden by QUIET_MODE)

# ── Fast startup config ───────────────────────────────────────────────────────
FAST_STARTUP                    = True   # skip broad discovery if slug discovery finds markets
ENABLE_BROAD_DISCOVERY_FALLBACK = False  # run broad discovery even when slug finds markets

# Targeted search queries – ONLY up-or-down style
TARGETED_QUERIES = [
    "BTC Up or Down",
    "Bitcoin Up or Down",
    "ETH Up or Down",
    "Ethereum Up or Down",
]

# ── Slug-based discovery config ───────────────────────────────────────────────

# asset_slug -> canonical asset
_SLUG_ASSET_MAP: Dict[str, str] = {
    "btc": "BTC",
    "eth": "ETH",
}

# timeframe_slug -> (canonical_tf, seconds)
_SLUG_TF_MAP: Dict[str, Tuple[str, int]] = {
    "5m":  ("5min",  300),
}

# Slug discovery must target only the current live candle.
# Keep this at 0: do not generate old/future ± candles for display/tracking.
_SLUG_CANDLE_EXTRA = 0

# Allow tiny clock/API drift, but reject future candles like +5m/+10m/+15m.
_CURRENT_MARKET_BUFFER_SEC = 30

# Timeout for slug fetches (keep short)
_SLUG_FETCH_TIMEOUT = 5  # seconds

# ── Strict UP/DOWN patterns ───────────────────────────────────────────────────

_UPDOWN_PHRASE_RE = re.compile(
    r"(up[\s\-_/]?or[\s\-_/]?down"
    r"|up[\-_/]down"
    r"|updown"
    r"|higher[\s\-_/]?or[\s\-_/]?lower"
    r"|above[\s\-_/]?or[\s\-_/]?below)",
    re.I,
)

_UPDOWN_SLUG_PATTERNS = [
    "btc-up-or-down",
    "bitcoin-up-or-down",
    "eth-up-or-down",
    "ethereum-up-or-down",
]

_WRONG_TYPE_RE = re.compile(
    r"(will\s+\w+\s+hit"
    r"|will\s+\w+\s+reach"
    r"|\w+\s+above\s+\$"
    r"|\w+\s+price\s+by"
    r"|\w+\s+to\s+\$"
    r"|btc\s+to\s+\$"
    r"|bitcoin\s+above\s+"
    r"|ethereum\s+price\s+by"
    r"|will\s+btc"
    r"|will\s+eth"
    r"|will\s+sol"
    r"|will\s+xrp"
    r"|will\s+bnb"
    r"|\$\d+k?\s+(by|before|end)"
    r"|\d+k?\s+by\s+(end|june|july|aug|sep|oct|nov|dec|jan|feb|mar|apr|may))",
    re.I,
)

TF_PATTERNS = {
    "5min":  [
        r"\b5[\s\-_]?min(?:ute)?s?\b",
        r"\b5\s*m\b",
        r"\bfive[\s\-]?min(?:ute)?s?\b",
        r"\b5min\b",
        r"\b5[\-_]minute\b",
        r"\b5m[\-_]up[\-_]or[\-_]down\b",
    ],
}
_TF_RE = {tf: [re.compile(p, re.I) for p in pats] for tf, pats in TF_PATTERNS.items()}

_REJECT_RE = re.compile(
    r"\b(weather|election|sports|nfl|nba|soccer|baseball|football|cricket|tennis|golf|president|senate|vote)\b",
    re.I,
)

_ASSET_ALIASES: Dict[str, str] = {
    "BTC": "BTC", "BITCOIN": "BTC", "BTC-USD": "BTC",
    "BITCOIN-UP": "BTC", "BITCOIN-DOWN": "BTC",
    "ETH": "ETH", "ETHEREUM": "ETH", "ETH-USD": "ETH",
    "ETHEREUM-UP": "ETH", "ETHEREUM-DOWN": "ETH",
}
_sorted_aliases = sorted(_ASSET_ALIASES.keys(), key=len, reverse=True)
_ASSET_RE = re.compile(
    r"(?<![A-Z0-9\-])(" + "|".join(re.escape(a) for a in _sorted_aliases) + r")(?![A-Z0-9\-])",
    re.I,
)

_POSSIBLE_UPDOWN_RE = re.compile(
    r"(up[\s\-_/]?or[\s\-_/]?down"
    r"|up[\-_/]down"
    r"|updown"
    r"|higher[\s\-_]or[\s\-_]lower"
    r"|above[\s\-_]or[\s\-_]below"
    r"|\b5\s*m\b"
    r")",
    re.I,
)

# ─────────────────────────── LOGGING ─────────────────────────────────────────

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
)

logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.client").setLevel(logging.WARNING)
logging.getLogger("websockets.server").setLevel(logging.WARNING)
logging.getLogger("websockets.protocol").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("aiohttp.client").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

log = logging.getLogger("scanner")
log.setLevel(logging.WARNING if QUIET_MODE else (logging.DEBUG if DEBUG_LOGS else logging.INFO))

# ─────────────────────────── DATA CLASSES ────────────────────────────────────

@dataclass
class TokenBook:
    token_id: str
    bids: Dict[str, float] = field(default_factory=dict)
    asks: Dict[str, float] = field(default_factory=dict)
    last_update: Optional[datetime] = None

    @property
    def best_bid(self) -> Optional[float]:
        return max((float(p) for p in self.bids), default=None)

    @property
    def best_ask(self) -> Optional[float]:
        return min((float(p) for p in self.asks), default=None)

    @property
    def mid(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        if b is not None and a is not None:
            return (b + a) / 2.0
        return a if a is not None else b

    def pct(self) -> Optional[int]:
        m = self.mid
        return round(m * 100) if m is not None else None

    def apply_snapshot(self, bids: list, asks: list) -> None:
        self.bids = {e["price"]: float(e["size"]) for e in bids if float(e["size"]) > 0}
        self.asks = {e["price"]: float(e["size"]) for e in asks if float(e["size"]) > 0}
        self.last_update = datetime.now(timezone.utc)

    def apply_change(self, side: str, price: str, size: float) -> None:
        bucket = self.bids if side.upper() == "BUY" else self.asks
        if size == 0.0:
            bucket.pop(price, None)
        else:
            bucket[price] = size
        self.last_update = datetime.now(timezone.utc)


@dataclass
class MarketInfo:
    market_id:        str
    title:            str
    asset:            str
    timeframe:        str
    candle_end:       datetime
    yes_token_id:     str
    no_token_id:      str
    accepting_orders: bool
    slug:             Optional[str] = None
    event_slug:       Optional[str] = None
    price_to_beat: Optional[float] = None
    price_to_beat_error: str = "not_loaded"
    price_to_beat_source: str = "missing"
    price_to_beat_url: Optional[str] = None
    price_to_beat_updated: Optional[datetime] = None


# ─────────────────────────── MARKET TRACKER ──────────────────────────────────

class MarketTracker:
    def __init__(self, market: MarketInfo) -> None:
        self.market   = market
        self.yes_book = TokenBook(market.yes_token_id)
        self.no_book  = TokenBook(market.no_token_id)
        self.status   = "starting"
        self.reconnect_count = 0
        self.last_ws_msg: Optional[datetime] = None
        self._running = True
        self._task: Optional[asyncio.Task] = None
        self._label = f"{market.asset} {market.timeframe}"
        self.price_to_beat = market.price_to_beat
        self.price_to_beat_error = market.price_to_beat_error
        self.price_to_beat_source = market.price_to_beat_source
        self.price_to_beat_url = market.price_to_beat_url
        self.price_to_beat_updated = market.price_to_beat_updated
        self._ptb_task: Optional[asyncio.Task] = None
        price_symbol = CHAINLINK_PRICE_SYMBOLS.get(str(market.asset or "").upper().strip())
        self.current_price: Optional[float] = None
        self.current_price_source = "POLY-CHAINLINK" if price_symbol else "UNSUPPORTED"
        self.current_price_status = "starting" if price_symbol else "unsupported"
        self.current_price_updated: Optional[datetime] = None
        binance_symbol = BINANCE_PRICE_SYMBOLS.get(str(market.asset or "").upper().strip())
        self.binance_price: Optional[float] = None
        self.binance_price_status = "starting" if binance_symbol else "unsupported"
        self.binance_price_updated: Optional[datetime] = None

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self._safe_loop(), name=f"t-{self.market.market_id[:8]}")
        self._ptb_task = asyncio.create_task(
            self._price_to_beat_loop(),
            name=f"ptb-{self.market.market_id[:8]}",
        )
        return self._task

    async def stop(self) -> None:
        self._running = False

        tasks = []
        if self._task and not self._task.done():
            self._task.cancel()
            tasks.append(self._task)

        if self._ptb_task and not self._ptb_task.done():
            self._ptb_task.cancel()
            tasks.append(self._ptb_task)

        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    def is_stale(self) -> bool:
        candidates = [self.last_ws_msg, self.yes_book.last_update, self.no_book.last_update]
        valid = [ts for ts in candidates if ts is not None]
        if not valid:
            return True
        latest = max(valid)
        ms = (datetime.now(timezone.utc) - latest).total_seconds() * 1000
        return ms > STALE_MS

    def yes_pct(self) -> Optional[int]:
        # Do not hide the last known price just because the book is stale.
        # Staleness is displayed separately as metadata in scan_printer().
        return self.yes_book.pct()

    def no_pct(self) -> Optional[int]:
        # Do not hide the last known price just because the book is stale.
        # Staleness is displayed separately as metadata in scan_printer().
        return self.no_book.pct()

    async def _price_to_beat_loop(self) -> None:
        while self._running:
            try:
                if self.price_to_beat is None:
                    self.price_to_beat_error = "loading"
                    self.market.price_to_beat_error = "loading"

                price, source, err, url = await fetch_price_to_beat_for_market(self.market)

                if price is not None:
                    updated = datetime.now(timezone.utc)

                    self.price_to_beat = price
                    self.price_to_beat_source = source
                    self.price_to_beat_error = "ok"
                    self.price_to_beat_url = url
                    self.price_to_beat_updated = updated

                    self.market.price_to_beat = price
                    self.market.price_to_beat_source = source
                    self.market.price_to_beat_error = "ok"
                    self.market.price_to_beat_url = url
                    self.market.price_to_beat_updated = updated

                    await asyncio.sleep(60)
                else:
                    self.price_to_beat_error = err or "api_failed"
                    self.price_to_beat_source = source
                    self.price_to_beat_url = url

                    self.market.price_to_beat_error = self.price_to_beat_error
                    self.market.price_to_beat_source = source
                    self.market.price_to_beat_url = url

                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.price_to_beat_error = f"ptb_loop_error_{exc.__class__.__name__}"
                self.market.price_to_beat_error = self.price_to_beat_error
                log.debug(f"[{self._label}] PTB loop error: {exc}", exc_info=True)
                await asyncio.sleep(5)

    async def _safe_loop(self) -> None:
        while self._running:
            try:
                await self._reconnect_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status = "restarting"
                self.reconnect_count += 1
                log.debug(f"[{self._label}] Orderbook WS supervisor restart after error: {exc}", exc_info=True)
                await asyncio.sleep(SUPERVISOR_RESTART_DELAY)

    async def _reconnect_loop(self) -> None:
        backoff = 1
        while self._running:
            try:
                await self._run_ws()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status = "reconnecting"
                self.reconnect_count += 1
                log.warning(f"[{self._label}] WS error ({exc}). Retry #{self.reconnect_count} in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def _run_ws(self) -> None:
        if not QUIET_MODE:
            log.info(f"[{self._label}] Connecting …")
        async with websockets.connect(WS_URL, ping_interval=None, open_timeout=15) as ws:
            self.status = "ok"
            if not QUIET_MODE:
                log.info(f"[{self._label}] Connected")
            await self._rest_snapshot()
            await ws.send(json.dumps({
                "type": "market",
                "assets_ids": [self.market.yes_token_id, self.market.no_token_id],
            }))
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    raise RuntimeError("orderbook recv timeout – forcing reconnect")
                self.last_ws_msg = datetime.now(timezone.utc)
                try:
                    self._handle(raw)
                except Exception as exc:
                    log.debug(f"[{self._label}] Ignored orderbook payload error: {exc}", exc_info=True)

    async def _rest_snapshot(self) -> None:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            for token_id, book in ((self.market.yes_token_id, self.yes_book),
                                   (self.market.no_token_id,  self.no_book)):
                try:
                    async with sess.get(f"{CLOB_URL}/book?token_id={token_id}") as r:
                        if r.status == 200:
                            d = await r.json(content_type=None)
                            book.apply_snapshot(d.get("bids", []), d.get("asks", []))
                except Exception as exc:
                    log.debug(f"[{self._label}] REST snapshot failed: {exc}")

    def _handle(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            return
        events = payload if isinstance(payload, list) else [payload]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype    = ev.get("event_type", "")
            asset_id = ev.get("asset_id", "")
            book     = (self.yes_book if asset_id == self.market.yes_token_id
                        else self.no_book if asset_id == self.market.no_token_id
                        else None)
            if book is None:
                continue
            if etype == "book":
                book.apply_snapshot(ev.get("bids", []), ev.get("asks", []))
            elif etype == "price_change":
                for ch in ev.get("changes", []):
                    try:
                        book.apply_change(ch["side"], ch["price"], float(ch["size"]))
                    except (KeyError, ValueError):
                        pass


# ─────────────────────────── DISCOVERY ───────────────────────────────────────

def _detect_asset(text: str) -> Optional[str]:
    m = _ASSET_RE.search(text)
    if m:
        return _ASSET_ALIASES.get(m.group(0).upper())
    return None

def _detect_timeframe(text: str) -> Optional[str]:
    for tf, pats in _TF_RE.items():
        for p in pats:
            if p.search(text):
                return tf
    return None

def _parse_field_as_list(value) -> list:
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return value if isinstance(value, list) else []

def _parse_tokens(raw: dict) -> Optional[Tuple[str, str]]:
    # ── Gamma format ──────────────────────────────────────────────────────────
    outcomes  = _parse_field_as_list(raw.get("outcomes", []))
    token_ids = _parse_field_as_list(raw.get("clobTokenIds", []))

    if len(outcomes) == len(token_ids) and len(outcomes) >= 2:
        yes_id = no_id = None
        for i, outcome in enumerate(outcomes):
            outcome_upper = str(outcome).strip().upper()
            token_id      = str(token_ids[i]).strip()
            if not token_id:
                continue
            if outcome_upper in ("YES", "UP"):
                yes_id = token_id
            elif outcome_upper in ("NO", "DOWN"):
                no_id = token_id
        if yes_id and no_id:
            return (yes_id, no_id)

    # ── CLOB format: tokens list ──────────────────────────────────────────────
    tokens = _parse_field_as_list(raw.get("tokens", []))
    if tokens:
        yes_id = no_id = None
        for tok in tokens:
            if not isinstance(tok, dict):
                continue
            outcome_upper = str(tok.get("outcome", "")).strip().upper()
            token_id      = str(tok.get("token_id", "")).strip()
            if not token_id:
                continue
            if outcome_upper in ("YES", "UP"):
                yes_id = token_id
            elif outcome_upper in ("NO", "DOWN"):
                no_id = token_id
        if yes_id and no_id:
            return (yes_id, no_id)

    return None

def _parse_dt(raw: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S",  "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(raw.split("+")[0].rstrip("Z"), fmt.rstrip("Z")).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        ts = float(raw)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, OSError):
        pass
    return None

def _resolve_end_time(raw: dict) -> Optional[datetime]:
    field_names = [
        "end_date_iso", "endDateIso", "endDate", "end_date",
        "end_time", "endTime", "close_time", "closeTime",
        "resolutionTime", "eventEndTime",
        "_event_endDate",
    ]
    for name in field_names:
        val = raw.get(name)
        if val:
            dt = _parse_dt(str(val))
            if dt is not None:
                return dt
    # Fallback: slug-inferred candle end
    forced = raw.get("_forced_candle_end")
    if forced:
        try:
            return datetime.fromtimestamp(int(forced), tz=timezone.utc)
        except (ValueError, OSError):
            pass
    return None


def _timeframe_seconds(tf: str) -> Optional[int]:
    for _tf_slug, (tf_canonical, tf_secs) in _SLUG_TF_MAP.items():
        if tf == tf_canonical:
            return tf_secs
    return None


def _current_tracker_key(m: MarketInfo) -> Tuple[str, str]:
    return (m.asset, m.timeframe)


def _is_current_live_market(m: MarketInfo, now: Optional[datetime] = None) -> bool:
    """Return True only for the current active candle, not future generated candles."""
    if now is None:
        now = datetime.now(timezone.utc)

    tf_secs = _timeframe_seconds(m.timeframe)
    if tf_secs is None:
        return False

    secs_remaining = (m.candle_end - now).total_seconds()
    if secs_remaining <= 0:
        return False

    # If remaining is larger than one timeframe, this is a future candle.
    # Example: 5min with 460s/760s/1060s remaining must be rejected.
    if secs_remaining > tf_secs + _CURRENT_MARKET_BUFFER_SEC:
        return False

    # If the API explicitly says it is not accepting orders, do not track it.
    if not m.accepting_orders:
        return False

    return True


def _market_selection_rank(m: MarketInfo, now: Optional[datetime] = None) -> Tuple[int, float]:
    """Lower rank is better when multiple markets exist for one asset/timeframe."""
    if now is None:
        now = datetime.now(timezone.utc)
    secs_remaining = (m.candle_end - now).total_seconds()
    accepting_rank = 0 if m.accepting_orders else 1
    return (accepting_rank, secs_remaining)


def _select_current_markets(markets: List[MarketInfo]) -> List[MarketInfo]:
    """Deduplicate to max one current live market per (asset, timeframe)."""
    now = datetime.now(timezone.utc)
    best: Dict[Tuple[str, str], MarketInfo] = {}

    for m in markets:
        if not _is_current_live_market(m, now=now):
            continue

        key = _current_tracker_key(m)
        existing = best.get(key)
        if existing is None or _market_selection_rank(m, now) < _market_selection_rank(existing, now):
            best[key] = m

    tf_order = {"5min": 0}
    asset_order = {asset: i for i, asset in enumerate(ASSETS)}
    return sorted(
        best.values(),
        key=lambda m: (tf_order.get(m.timeframe, 99), asset_order.get(m.asset, 99), m.candle_end),
    )

def _dedup_key(raw: dict) -> Optional[str]:
    for k in ("condition_id", "conditionId", "id", "slug"):
        v = raw.get(k)
        if v:
            return str(v).strip()
    return None

def _combined_text(raw: dict) -> str:
    _FIELDS = (
        "question", "title",
        "slug", "marketSlug",
        "eventSlug", "event_slug",
        "groupItemTitle", "seriesSlug",
        "category", "description",
        "_event_title", "_event_slug", "_event_category",
        "_event_source_slug",
    )
    return " ".join(str(raw.get(k, "")) for k in _FIELDS)


def _is_short_term_updown_market(combined: str, raw: dict) -> bool:
    # Forced flag from slug discovery
    if raw.get("_forced_updown"):
        return True

    # Slug-based patterns
    slug       = str(raw.get("slug", "")).lower()
    event_slug = str(raw.get("_event_slug", "")).lower()
    for pat in _UPDOWN_SLUG_PATTERNS:
        if pat in slug or pat in event_slug:
            return True

    # Also match generated source slug containing "-updown-"
    source_slug = str(raw.get("_event_source_slug", "")).lower()
    if "-updown-" in source_slug:
        return True

    if _UPDOWN_PHRASE_RE.search(combined):
        return True

    return False


def _parse_market(
    raw: dict,
    reject_counts: dict,
    source: str = "unknown",
    debug_wrong_crypto: Optional[list] = None,
    debug_updown_candidates: Optional[list] = None,
) -> Optional[MarketInfo]:
    cid      = (raw.get("condition_id") or raw.get("conditionId") or "").strip()
    question = (raw.get("question") or raw.get("title") or "").strip()
    if not cid or not question:
        return None

    if not raw.get("active") or raw.get("closed"):
        reject_counts["closed"] += 1
        return None

    combined = _combined_text(raw)

    if _REJECT_RE.search(combined):
        reject_counts["unrelated_category"] += 1
        return None

    # Detect asset from text; fallback to forced asset from slug
    asset = _detect_asset(combined)
    if not asset and raw.get("_forced_asset"):
        asset = raw["_forced_asset"]

    # ── Collect wrong crypto market sample ───────────────────────────────────
    if asset and debug_wrong_crypto is not None:
        is_updown = _is_short_term_updown_market(combined, raw)
        if not is_updown:
            debug_wrong_crypto.append({
                "source":   source,
                "question": question,
                "slug":     raw.get("slug", ""),
            })

    if not asset:
        reject_counts["wrong_asset"] += 1
        return None

    # ── Strict UP/DOWN filter ────────────────────────────────────────────────
    is_updown = _is_short_term_updown_market(combined, raw)

    if _POSSIBLE_UPDOWN_RE.search(combined) and debug_updown_candidates is not None:
        tf  = _detect_timeframe(combined) or raw.get("_forced_timeframe")
        debug_updown_candidates.append({
            "source":         source,
            "question":       question,
            "slug":           raw.get("slug", ""),
            "event_title":    raw.get("_event_title", ""),
            "event_slug":     raw.get("_event_slug", ""),
            "combined_300":   combined[:300],
            "outcomes":       raw.get("outcomes", ""),
            "clobTokenIds":   raw.get("clobTokenIds", ""),
            "tokens":         raw.get("tokens", ""),
            "endDate":        raw.get("endDate", ""),
            "active":         raw.get("active"),
            "closed":         raw.get("closed"),
            "detected_asset": asset,
            "detected_tf":    tf,
            "is_updown":      is_updown,
        })

    if not is_updown:
        reject_counts["wrong_market_type"] += 1
        return None

    # Detect timeframe from text; fallback to forced timeframe from slug
    tf = _detect_timeframe(combined)
    if not tf and raw.get("_forced_timeframe"):
        tf = raw["_forced_timeframe"]

    if not tf:
        log.debug(f"UPDOWN but unknown_timeframe: {question[:80]}")
        reject_counts["unknown_timeframe"] += 1
        return None

    tok = _parse_tokens(raw)
    if tok is None:
        log.debug(f"REJECT (bad tokens): {question[:60]}")
        reject_counts["missing_tokens"] += 1
        return None

    yes_id, no_id = tok

    end = _resolve_end_time(raw)
    if end is None:
        log.debug(f"REJECT (no end time): {question[:60]}")
        reject_counts["missing_end_time"] += 1
        return None

    if end <= datetime.now(timezone.utc):
        reject_counts["closed"] += 1
        return None

    if "accepting_orders" in raw:
        accepting_orders = bool(raw.get("accepting_orders"))
    elif "acceptingOrders" in raw:
        accepting_orders = bool(raw.get("acceptingOrders"))
    else:
        accepting_orders = bool(raw.get("active")) and not bool(raw.get("closed"))

    slug = (
        raw.get("_event_source_slug")
        or raw.get("eventSlug")
        or raw.get("event_slug")
        or raw.get("_event_slug")
        or raw.get("slug")
        or raw.get("marketSlug")
    )

    event_slug = (
        raw.get("eventSlug")
        or raw.get("event_slug")
        or raw.get("_event_slug")
    )

    if not QUIET_MODE and SHOW_ACCEPT_LOGS:
        log.info(f"ACCEPT: {asset} {tf} | {question[:60]}")
    return MarketInfo(
        market_id=cid, title=question, asset=asset, timeframe=tf,
        candle_end=end, yes_token_id=yes_id, no_token_id=no_id,
        accepting_orders=accepting_orders,
        slug=str(slug).strip() if slug else None,
        event_slug=str(event_slug).strip() if event_slug else None,
    )


# ─────────────────────────── HTTP HELPERS ────────────────────────────────────

async def _get_json(sess: aiohttp.ClientSession, url: str) -> Optional[object]:
    try:
        async with sess.get(url) as r:
            if r.status != 200:
                log.debug(f"HTTP {r.status} for {url}")
                return None
            return await r.json(content_type=None)
    except Exception as exc:
        log.debug(f"HTTP error for {url}: {exc}")
        return None


# ─────────────────────────── PRICE TO BEAT HELPERS ───────────────────────────

_PTB_CRYPTO_PRICE_SOURCE = "polymarket_crypto_price_api"
_PTB_GAMMA_SOURCE = "gamma_event_metadata"
_PTB_PREV_FINAL_SOURCE = "gamma_previous_event_final_price"
_PTB_EQUITY_SOURCE = "polymarket_equity_price_to_beat_api"
_PTB_DEBUG_FILE = "price_to_beat_api_debug.jsonl"
_PTB_DEBUG_ENABLED = False

_PTB_ASSET_RANGES: Dict[str, Tuple[float, float]] = {
    "BTC": (10000.0, 300000.0),
    "ETH": (500.0, 20000.0),
}

_PTB_NUMERIC_RE = re.compile(
    r"[-+]?\$?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
)

_PTB_CRYPTO_UPDOWN_SLUG_RE = re.compile(
    r"^(?P<asset>btc|eth)-updown-(?P<tf>5m)-(?P<ts>\d+)$",
    re.I,
)

_PTB_CRYPTO_VARIANTS: Dict[str, str] = {
    "5min": "fiveminute",
}

_PTB_VALUE_CACHE: Dict[Tuple[str, str, int], Tuple[float, str, datetime]] = {}


def _is_finite_number(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _numbers_from_string(text: str) -> List[float]:
    s = text.strip()
    if not s:
        return []

    direct = s.replace("$", "").replace(",", "").strip()
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", direct):
        try:
            v = float(direct)
            return [v] if _is_finite_number(v) else []
        except ValueError:
            return []

    out: List[float] = []
    for m in _PTB_NUMERIC_RE.finditer(text):
        token = m.group(0).replace("$", "").replace(",", "").replace(" ", "").strip()
        try:
            v = float(token)
            if _is_finite_number(v):
                out.append(v)
        except ValueError:
            pass
    return out


def _extract_ptb_numeric_candidates(value, depth: int = 0) -> List[float]:
    if depth > 12:
        return []

    if value is None or isinstance(value, bool):
        return []

    if isinstance(value, (int, float)):
        v = float(value)
        return [v] if _is_finite_number(v) else []

    if isinstance(value, str):
        return _numbers_from_string(value)

    out: List[float] = []

    if isinstance(value, dict):
        for _k, v in value.items():
            out.extend(_extract_ptb_numeric_candidates(v, depth + 1))
        return out

    if isinstance(value, list):
        for item in value:
            out.extend(_extract_ptb_numeric_candidates(item, depth + 1))
        return out

    return []


def _pick_ptb_candidate_for_asset(asset: str, candidates: List[float]) -> Tuple[Optional[float], str]:
    asset_key = str(asset or "").upper().strip()
    lo, hi = _PTB_ASSET_RANGES.get(asset_key, (0.0000001, 1_000_000_000.0))

    if not candidates:
        return None, "no_numeric_value_in_response"

    for c in candidates:
        if lo <= c <= hi:
            return c, "ok"

    return None, "outside_asset_range"


def _write_ptb_debug(record: dict) -> None:
    if not _PTB_DEBUG_ENABLED:
        return
    try:
        with open(_PTB_DEBUG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        log.debug(f"Could not write PTB debug file: {exc}")


def _ptb_candle_start_ts(market: MarketInfo) -> Optional[int]:
    tf_secs = _timeframe_seconds(market.timeframe)
    if tf_secs is None:
        return None
    return int(market.candle_end.timestamp()) - tf_secs


def _ptb_cache_key(market: MarketInfo) -> Optional[Tuple[str, str, int]]:
    start_ts = _ptb_candle_start_ts(market)
    if start_ts is None:
        return None
    return (market.asset, market.timeframe, start_ts)


def _iso_z_from_ts(ts: int) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _unique_nonempty(values: List[Optional[str]]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        s = str(value or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _current_slug_candidates_for_ptb(market: MarketInfo) -> List[str]:
    return _unique_nonempty([market.slug, market.event_slug])


def _previous_slug_candidates_for_ptb(market: MarketInfo) -> List[str]:
    tf_secs = _timeframe_seconds(market.timeframe)
    start_ts = _ptb_candle_start_ts(market)
    if tf_secs is None or start_ts is None:
        return []

    asset_slug = next((k for k, v in _SLUG_ASSET_MAP.items() if v == market.asset), None)
    tf_slug = next((k for k, (v, _secs) in _SLUG_TF_MAP.items() if v == market.timeframe), None)
    candidates: List[Optional[str]] = []

    if asset_slug and tf_slug:
        candidates.append(f"{asset_slug}-updown-{tf_slug}-{start_ts - tf_secs}")

    for slug in _current_slug_candidates_for_ptb(market):
        m = _PTB_CRYPTO_UPDOWN_SLUG_RE.match(slug)
        if not m:
            continue
        candidates.append(
            f"{m.group('asset').lower()}-updown-{m.group('tf').lower()}-{start_ts - tf_secs}"
        )

    return _unique_nonempty(candidates)


def _event_metadata_dict(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    meta = raw.get("eventMetadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            return None
    return meta if isinstance(meta, dict) else None


def _coerce_ptb_price(asset: str, raw_value) -> Tuple[Optional[float], str]:
    candidates = _extract_ptb_numeric_candidates(raw_value)
    return _pick_ptb_candidate_for_asset(asset, candidates)


def _extract_event_metadata_price(
    raw,
    asset: str,
    fields: List[str],
) -> Tuple[Optional[float], Optional[str], str]:
    items = raw if isinstance(raw, list) else [raw]
    found_any_metadata = False
    found_any_field = False

    for item in items:
        meta = _event_metadata_dict(item)
        if meta is None:
            continue
        found_any_metadata = True
        for field in fields:
            aliases = [field]
            if field == "priceToBeat":
                aliases.append("price_to_beat")
            elif field == "finalPrice":
                aliases.append("final_price")
            for alias in aliases:
                if alias not in meta:
                    continue
                found_any_field = True
                price, reason = _coerce_ptb_price(asset, meta.get(alias))
                if price is not None:
                    return price, alias, "ok"
                return None, alias, reason

    if not found_any_metadata:
        return None, None, "missing_event_metadata"
    if not found_any_field:
        return None, None, "missing_metadata_field"
    return None, None, "metadata_price_rejected"


async def _get_json_text_status(
    sess: aiohttp.ClientSession,
    url: str,
) -> Tuple[Optional[int], Optional[object], str]:
    try:
        async with sess.get(url) as r:
            status = r.status
            text = await r.text()
        try:
            return status, json.loads(text), text
        except Exception:
            return status, None, text
    except Exception as exc:
        return None, None, f"{exc.__class__.__name__}: {exc}"


async def _fetch_gamma_metadata_price(
    sess: aiohttp.ClientSession,
    market: MarketInfo,
    slugs: List[str],
    fields: List[str],
    source: str,
) -> Tuple[Optional[float], str, str, Optional[str]]:
    last_url: Optional[str] = None
    last_reason = "no_slug"

    for slug in slugs:
        url = f"{GAMMA_URL}/events/slug/{urllib.parse.quote(slug, safe='')}"
        last_url = url
        status, parsed, raw_text = await _get_json_text_status(sess, url)

        if status != 200 or parsed is None:
            last_reason = "gamma_404" if status == 404 else f"gamma_status_{status}"
            _write_ptb_debug({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "asset": market.asset,
                "timeframe": market.timeframe,
                "slug": slug,
                "url": url,
                "http_status": status,
                "source": source,
                "fields": fields,
                "raw_response_first_500": raw_text[:500],
                "accepted_price": None,
                "error_reason": last_reason,
            })
            continue

        price, field, reason = _extract_event_metadata_price(parsed, market.asset, fields)
        _write_ptb_debug({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "asset": market.asset,
            "timeframe": market.timeframe,
            "slug": slug,
            "url": url,
            "http_status": status,
            "source": source,
            "fields": fields,
            "matched_field": field,
            "accepted_price": price,
            "error_reason": reason,
        })

        if price is not None:
            return price, source, "ok", url
        last_reason = reason

    return None, source, last_reason, last_url


async def _fetch_crypto_price_open_price(
    sess: aiohttp.ClientSession,
    market: MarketInfo,
) -> Tuple[Optional[float], str, str, Optional[str]]:
    start_ts = _ptb_candle_start_ts(market)
    tf_secs = _timeframe_seconds(market.timeframe)
    variant = _PTB_CRYPTO_VARIANTS.get(market.timeframe)

    if start_ts is None or tf_secs is None or variant is None:
        return None, _PTB_CRYPTO_PRICE_SOURCE, "unsupported_timeframe", None

    params = {
        "symbol": market.asset,
        "eventStartTime": _iso_z_from_ts(start_ts),
        "variant": variant,
        "endDate": _iso_z_from_ts(start_ts + tf_secs),
    }
    url = f"https://polymarket.com/api/crypto/crypto-price?{urllib.parse.urlencode(params)}"

    status, parsed, raw_text = await _get_json_text_status(sess, url)
    price: Optional[float] = None
    reason = "crypto_price_not_loaded"

    if status != 200 or not isinstance(parsed, dict):
        reason = "crypto_price_404" if status == 404 else f"crypto_price_status_{status}"
    elif "openPrice" not in parsed or parsed.get("openPrice") is None:
        reason = "crypto_price_open_missing"
    else:
        price, reason = _coerce_ptb_price(market.asset, parsed.get("openPrice"))

    _write_ptb_debug({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "asset": market.asset,
        "timeframe": market.timeframe,
        "slug": market.slug,
        "url": url,
        "http_status": status,
        "source": _PTB_CRYPTO_PRICE_SOURCE,
        "raw_response_first_500": raw_text[:500],
        "accepted_price": price,
        "error_reason": "ok" if price is not None else reason,
    })

    if price is not None:
        return price, _PTB_CRYPTO_PRICE_SOURCE, "ok", url

    return None, _PTB_CRYPTO_PRICE_SOURCE, reason, url


async def fetch_price_to_beat_for_market(
    market: MarketInfo,
) -> Tuple[Optional[float], str, str, Optional[str]]:
    """
    Fetch Price To Beat using the same crypto-price route as Polymarket's UI.
    For crypto UP/DOWN, openPrice is the price-to-beat.

    Returns:
        price, source, error_reason, url_used
    """
    cache_key = _ptb_cache_key(market)
    if cache_key is not None:
        cached = _PTB_VALUE_CACHE.get(cache_key)
        if cached is not None:
            price, source, updated = cached
            return price, f"{source}:cache", "ok", None

    current_slugs = _current_slug_candidates_for_ptb(market)
    if not current_slugs:
        _write_ptb_debug({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "asset": getattr(market, "asset", None),
            "timeframe": getattr(market, "timeframe", None),
            "slug": None,
            "url": None,
            "http_status": None,
            "raw_response_first_500": "",
            "parsed_candidates": [],
            "accepted_price": None,
            "error_reason": "no_slug",
        })
        return None, _PTB_GAMMA_SOURCE, "no_slug", None

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
    }

    timeout = aiohttp.ClientTimeout(total=6)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        price, source, err, url = await _fetch_crypto_price_open_price(sess, market)
        if price is not None:
            if cache_key is not None:
                _PTB_VALUE_CACHE[cache_key] = (price, source, datetime.now(timezone.utc))
            return price, source, "ok", url

        last_source, last_err, last_url = source, err, url

        price, source, err, url = await _fetch_gamma_metadata_price(
            sess,
            market,
            current_slugs,
            ["priceToBeat"],
            _PTB_GAMMA_SOURCE,
        )
        if price is not None:
            if cache_key is not None:
                _PTB_VALUE_CACHE[cache_key] = (price, source, datetime.now(timezone.utc))
            return price, source, "ok", url
        last_source, last_err, last_url = source, err, url

        previous_slugs = _previous_slug_candidates_for_ptb(market)
        if previous_slugs:
            price, source, err, url = await _fetch_gamma_metadata_price(
                sess,
                market,
                previous_slugs,
                ["finalPrice"],
                _PTB_PREV_FINAL_SOURCE,
            )
            if price is not None:
                if cache_key is not None:
                    _PTB_VALUE_CACHE[cache_key] = (price, source, datetime.now(timezone.utc))
                return price, source, "ok", url
            last_source, last_err, last_url = source, err, url

    # The /api/equity/price-to-beat endpoint rejects crypto slugs like
    # btc-updown-5m-1778064300, but keep it as a fallback for other markets.
    first_slug = current_slugs[0]
    if not _PTB_CRYPTO_UPDOWN_SLUG_RE.match(first_slug):
        url = f"https://polymarket.com/api/equity/price-to-beat/{urllib.parse.quote(first_slug, safe='')}"
        status: Optional[int] = None
        raw_text = ""
        candidates: List[float] = []
        accepted_price: Optional[float] = None
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
                async with sess.get(url) as r:
                    status = r.status
                    raw_text = await r.text()

            if status == 200:
                try:
                    parsed = json.loads(raw_text)
                except Exception:
                    parsed = raw_text
                candidates = _extract_ptb_numeric_candidates(parsed)
                accepted_price, err = _pick_ptb_candidate_for_asset(market.asset, candidates)
            else:
                err = "equity_api_404" if status == 404 else f"equity_api_status_{status}"

            _write_ptb_debug({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "asset": market.asset,
                "timeframe": market.timeframe,
                "slug": first_slug,
                "url": url,
                "http_status": status,
                "source": _PTB_EQUITY_SOURCE,
                "raw_response_first_500": raw_text[:500],
                "parsed_candidates": candidates,
                "accepted_price": accepted_price,
                "error_reason": "ok" if accepted_price is not None else err,
            })
            if accepted_price is not None:
                if cache_key is not None:
                    _PTB_VALUE_CACHE[cache_key] = (
                        accepted_price,
                        _PTB_EQUITY_SOURCE,
                        datetime.now(timezone.utc),
                    )
                return accepted_price, _PTB_EQUITY_SOURCE, "ok", url
            return None, _PTB_EQUITY_SOURCE, err, url
        except Exception as exc:
            err = f"equity_api_failed_{exc.__class__.__name__}"
            _write_ptb_debug({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "asset": getattr(market, "asset", None),
                "timeframe": getattr(market, "timeframe", None),
                "slug": first_slug,
                "url": url,
                "http_status": status,
                "source": _PTB_EQUITY_SOURCE,
                "raw_response_first_500": raw_text[:500],
                "parsed_candidates": candidates,
                "accepted_price": accepted_price,
                "error_reason": err,
            })
            return None, _PTB_EQUITY_SOURCE, err, url

    return None, last_source, last_err, last_url



# ─────────────────────────── SLUG-BASED DISCOVERY ────────────────────────────

def _generate_slug_candidates(now_ts: Optional[int] = None) -> List[Tuple[str, str, str, int]]:
    """
    Generate only the current live candle slugs for each asset/timeframe.

    Polymarket has used both start-timestamp and end-timestamp slug styles, so
    for one current candle we try exactly two slugs:
      <asset>-updown-<tf>-<current_start>
      <asset>-updown-<tf>-<current_end>

    We intentionally do NOT generate old/future ± candles here, because those
    created duplicate ETH/SOL/XRP/etc rows with 460s/760s/1060s remaining.
    Returns list of (slug, asset_canonical, tf_canonical, current_candle_end_ts).
    """
    if now_ts is None:
        now_ts = int(time.time())

    results: List[Tuple[str, str, str, int]] = []

    for asset_slug, asset_canonical in _SLUG_ASSET_MAP.items():
        for tf_slug, (tf_canonical, tf_secs) in _SLUG_TF_MAP.items():
            current_start = (now_ts // tf_secs) * tf_secs
            current_end   = current_start + tf_secs

            for ts in (current_start, current_end):
                slug = f"{asset_slug}-updown-{tf_slug}-{ts}"
                results.append((slug, asset_canonical, tf_canonical, current_end))

    # Deduplicate slugs while preserving order
    seen: set[str] = set()
    deduped: List[Tuple[str, str, str, int]] = []
    for entry in results:
        if entry[0] not in seen:
            seen.add(entry[0])
            deduped.append(entry)

    return deduped

def _inject_slug_meta(mkt: dict, asset: str, tf: str, candle_end_ts: int, source_slug: str) -> None:
    """Inject forced metadata fields into a raw market dict."""
    mkt["_forced_asset"]       = asset
    mkt["_forced_timeframe"]   = tf
    mkt["_forced_candle_end"]  = candle_end_ts
    mkt["_forced_updown"]      = True
    mkt["_event_source_slug"]  = source_slug


def _flatten_event_inline(
    event: dict,
    asset: str,
    tf: str,
    candle_end_ts: int,
    source_slug: str,
    source_tag: str,
    seen: set,
    out: list,
) -> int:
    """Flatten a Gamma event's nested markets, injecting slug meta. Returns count added."""
    added = 0
    nested = event.get("markets") or []
    if not isinstance(nested, list):
        return 0
    for mkt in nested:
        if not isinstance(mkt, dict):
            continue
        mkt["_event_title"]    = event.get("title", "")
        mkt["_event_slug"]     = event.get("slug", "")
        mkt["_event_endDate"]  = event.get("endDate", "")
        mkt["_event_category"] = event.get("category", "")
        mkt["_source"]         = source_tag
        if not mkt.get("endDate") and event.get("endDate"):
            mkt["endDate"] = event["endDate"]
        _inject_slug_meta(mkt, asset, tf, candle_end_ts, source_slug)
        k = _dedup_key(mkt)
        if k and k not in seen:
            seen.add(k)
            out.append(mkt)
            added += 1
    return added


async def _fetch_one_slug(
    sess: aiohttp.ClientSession,
    slug: str,
    asset: str,
    tf: str,
    candle_end_ts: int,
    seen: set,
    out: list,
    slug_events_found: list,
    slug_markets_found: list,
    slug_nested_found: list,
) -> None:
    """
    Try all 4 endpoints for a single slug.
    Mutates out with any found markets.
    """
    # Endpoints to try: (url, is_event_endpoint)
    endpoints = [
        (f"{GAMMA_URL}/events?slug={slug}", True),
        (f"{GAMMA_URL}/events/slug/{slug}", True),
        (f"{GAMMA_URL}/markets?slug={slug}", False),
        (f"{GAMMA_URL}/markets/slug/{slug}", False),
    ]

    for url, is_event in endpoints:
        data = await _get_json(sess, url)
        if data is None:
            continue

        # Normalise to list
        items: list = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        items = [i for i in items if isinstance(i, dict)]
        if not items:
            continue

        if is_event:
            for event in items:
                nested_count = _flatten_event_inline(
                    event, asset, tf, candle_end_ts, slug,
                    "slug_event_market", seen, out,
                )
                if nested_count > 0:
                    slug_events_found.append(slug)
                    slug_nested_found.append(nested_count)
                    if not QUIET_MODE:
                        print(
                            f"\nSLUG HIT:\n"
                            f"  slug:               {slug}\n"
                            f"  source:             events/slug\n"
                            f"  asset:              {asset}\n"
                            f"  timeframe:          {tf}\n"
                            f"  markets inside event: {nested_count}"
                        )
                elif event.get("id") or event.get("slug"):
                    # Event found but no nested markets – treat event itself as candidate
                    event["_event_title"]    = event.get("title", "")
                    event["_event_slug"]     = event.get("slug", "")
                    event["_event_endDate"]  = event.get("endDate", "")
                    event["_event_category"] = event.get("category", "")
                    event["_source"]         = "slug_event"
                    _inject_slug_meta(event, asset, tf, candle_end_ts, slug)
                    k = _dedup_key(event)
                    if k and k not in seen:
                        seen.add(k)
                        out.append(event)
                        slug_events_found.append(slug)
                        if not QUIET_MODE:
                            print(
                                f"\nSLUG HIT:\n"
                                f"  slug:               {slug}\n"
                                f"  source:             events/slug (no nested markets)\n"
                                f"  asset:              {asset}\n"
                                f"  timeframe:          {tf}\n"
                                f"  markets inside event: 0"
                            )
        else:
            for mkt in items:
                _inject_slug_meta(mkt, asset, tf, candle_end_ts, slug)
                mkt["_source"] = "slug_market"
                k = _dedup_key(mkt)
                if k and k not in seen:
                    seen.add(k)
                    out.append(mkt)
                    slug_markets_found.append(slug)
                    if not QUIET_MODE:
                        print(
                            f"\nSLUG HIT:\n"
                            f"  slug:               {slug}\n"
                            f"  source:             markets/slug\n"
                            f"  asset:              {asset}\n"
                            f"  timeframe:          {tf}\n"
                            f"  markets inside event: 1"
                        )


async def _fetch_slug_markets(sess: aiohttp.ClientSession) -> Tuple[List[dict], dict]:
    """
    Main slug discovery entry point.
    Returns (list_of_raw_markets, stats_dict).
    """
    candidates = _generate_slug_candidates()
    log.info(f"  Slug candidates generated: {len(candidates)}")

    out: List[dict] = []
    seen: set[str] = set()
    slug_events_found:  List[str] = []
    slug_markets_found: List[str] = []
    slug_nested_found:  List[int] = []

    timeout = aiohttp.ClientTimeout(total=_SLUG_FETCH_TIMEOUT)

    # Run all slug fetches concurrently
    async def _safe_fetch(entry):
        slug, asset, tf, candle_end_ts = entry
        try:
            async with aiohttp.ClientSession(timeout=timeout) as slug_sess:
                await _fetch_one_slug(
                    slug_sess, slug, asset, tf, candle_end_ts,
                    seen, out,
                    slug_events_found, slug_markets_found, slug_nested_found,
                )
        except Exception as exc:
            log.debug(f"Slug fetch error [{slug}]: {exc}")

    await asyncio.gather(*[_safe_fetch(entry) for entry in candidates])

    stats = {
        "candidates_generated": len(candidates),
        "events_found":         len(slug_events_found),
        "markets_found":        len(slug_markets_found),
        "nested_markets_found": sum(slug_nested_found),
    }
    log.info(
        f"  Slug events found: {stats['events_found']} | "
        f"Slug markets found: {stats['markets_found']} | "
        f"Slug nested markets: {stats['nested_markets_found']}"
    )
    return out, stats


# ─────────────────────────── FETCH FUNCTIONS ─────────────────────────────────

async def _search_endpoint(
    sess: aiohttp.ClientSession,
    base_url: str,
    query: str,
    source_tag: str,
    seen: set,
    out: list,
) -> int:
    added = 0
    for param in ("search", "q", "query"):
        encoded = urllib.parse.quote_plus(query)
        url = f"{base_url}?active=true&closed=false&limit=500&{param}={encoded}"
        data = await _get_json(sess, url)
        items: list = data if isinstance(data, list) else (data or {}).get("data", []) if data else []
        count = 0
        for item in items:
            k = _dedup_key(item)
            if k and k not in seen:
                seen.add(k)
                item["_source"] = source_tag
                out.append(item)
                added += 1
                count += 1
        if count > 0:
            log.debug(f"  [{source_tag}] query={repr(query)} param={param}: {count} new items")
    return added


async def _fetch_gamma_markets(sess: aiohttp.ClientSession) -> Tuple[List[dict], int, int]:
    all_items: List[dict] = []
    seen: set[str] = set()

    general_count = 0
    offset = 0
    limit  = 500
    for _ in range(10):
        data = await _get_json(
            sess,
            f"{GAMMA_URL}/markets?active=true&closed=false&limit={limit}&offset={offset}",
        )
        items: list = data if isinstance(data, list) else (data or {}).get("data", []) if data else []
        for item in items:
            k = _dedup_key(item)
            if k and k not in seen:
                seen.add(k)
                item["_source"] = "gamma_market"
                all_items.append(item)
        general_count += len(items)
        if len(items) < limit:
            break
        offset += limit

    search_count = 0
    for query in TARGETED_QUERIES:
        added = await _search_endpoint(sess, f"{GAMMA_URL}/markets", query, "gamma_market_search", seen, all_items)
        search_count += added

    log.info(f"  Raw Gamma markets general: {general_count} | targeted search new: {search_count} | unique total: {len(all_items)}")
    return all_items, general_count, search_count


async def _fetch_gamma_events(sess: aiohttp.ClientSession) -> Tuple[List[dict], int, int, int]:
    all_markets: List[dict] = []
    seen: set[str] = set()

    general_event_count  = 0
    search_event_count   = 0
    nested_market_count  = 0

    def _flatten_event(event: dict, source: str) -> None:
        nonlocal nested_market_count
        nested = event.get("markets") or []
        if not isinstance(nested, list):
            return
        for mkt in nested:
            if not isinstance(mkt, dict):
                continue
            mkt["_event_title"]    = event.get("title", "")
            mkt["_event_slug"]     = event.get("slug", "")
            mkt["_event_endDate"]  = event.get("endDate", "")
            mkt["_event_category"] = event.get("category", "")
            mkt["_source"]         = source
            if not mkt.get("endDate") and event.get("endDate"):
                mkt["endDate"] = event["endDate"]
            k = _dedup_key(mkt)
            if k and k not in seen:
                seen.add(k)
                all_markets.append(mkt)
                nested_market_count += 1

    # 1. General paginated events
    offset = 0
    limit  = 500
    for _ in range(10):
        data = await _get_json(
            sess,
            f"{GAMMA_URL}/events?active=true&closed=false&limit={limit}&offset={offset}",
        )
        items: list = data if isinstance(data, list) else (data or {}).get("data", []) if data else []
        for event in items:
            _flatten_event(event, "gamma_event_market")
        general_event_count += len(items)
        if len(items) < limit:
            break
        offset += limit

    # 2. Targeted search queries on events endpoint
    seen_events: set[str] = set()
    raw_events_for_search: list = []

    for query in TARGETED_QUERIES:
        for param in ("search", "q", "query"):
            encoded = urllib.parse.quote_plus(query)
            url = f"{GAMMA_URL}/events?active=true&closed=false&limit=500&{param}={encoded}"
            data = await _get_json(sess, url)
            items = data if isinstance(data, list) else (data or {}).get("data", []) if data else []
            count = 0
            for event in items:
                ek = event.get("id") or event.get("slug") or ""
                if ek and ek not in seen_events:
                    seen_events.add(ek)
                    raw_events_for_search.append(event)
                    count += 1
                    search_event_count += 1
            if count > 0:
                log.debug(f"  [gamma_event_search] query={repr(query)} param={param}: {count} new events")

    for event in raw_events_for_search:
        _flatten_event(event, "gamma_event_search_market")

    log.info(
        f"  Raw Gamma events general: {general_event_count} | targeted search events: {search_event_count}"
        f" | nested markets (unique): {nested_market_count}"
    )
    return all_markets, general_event_count, search_event_count, nested_market_count


async def _fetch_clob_markets(sess: aiohttp.ClientSession) -> Tuple[List[dict], int]:
    all_items: List[dict] = []
    seen: set[str] = set()
    cursor = "MA=="
    total  = 0

    for _ in range(20):
        data = await _get_json(sess, f"{CLOB_URL}/markets?next_cursor={cursor}")
        if not data or not isinstance(data, dict):
            break
        items = data.get("data", [])
        for item in items:
            item["_source"] = "clob_market"
            k = _dedup_key(item)
            if k and k not in seen:
                seen.add(k)
                all_items.append(item)
        total += len(items)
        next_c = data.get("next_cursor", "")
        if not next_c or next_c == cursor:
            break
        cursor = next_c

    log.info(f"  Raw CLOB markets fetched: {total} | unique: {len(all_items)}")
    return all_items, total


# ─────────────────────────── MAIN FETCH ──────────────────────────────────────

async def fetch_markets() -> List[MarketInfo]:
    global _last_broad_skipped
    results: List[MarketInfo] = []
    seen_ids: set[str] = set()
    debug_wrong_crypto: List[dict] = []
    debug_updown_candidates: List[dict] = []

    reject_counts = {
        "wrong_asset":        0,
        "wrong_market_type":  0,
        "wrong_timeframe":    0,
        "unknown_timeframe":  0,
        "missing_tokens":     0,
        "missing_end_time":   0,
        "closed":             0,
        "unrelated_category": 0,
    }
    duplicates_skipped = 0

    timeout = aiohttp.ClientTimeout(total=30)

    # ── Phase 1: Slug-based discovery (runs first, before broad scans) ────────
    log.info("Starting slug-based discovery …")
    slug_timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=slug_timeout) as slug_sess:
        slug_markets, slug_stats = await _fetch_slug_markets(slug_sess)

    slug_valid = 0
    for raw in slug_markets:
        key = _dedup_key(raw)
        if key in seen_ids:
            duplicates_skipped += 1
            continue
        if key:
            seen_ids.add(key)
        source = raw.get("_source", "slug_unknown")
        m = _parse_market(
            raw, reject_counts,
            source=source,
            debug_wrong_crypto=debug_wrong_crypto,
            debug_updown_candidates=debug_updown_candidates,
        )
        if m:
            if m.market_id not in {r.market_id for r in results}:
                results.append(m)
                slug_valid += 1

    log.info(f"  Slug valid markets accepted: {slug_valid}")

    # ── Phase 2: Broad / general discovery (conditional) ─────────────────────
    run_broad = True
    broad_skip_reason = ""
    if FAST_STARTUP and slug_valid > 0 and not ENABLE_BROAD_DISCOVERY_FALLBACK:
        run_broad = False
        broad_skip_reason = "FAST_STARTUP active"
    elif slug_valid == 0 and not ENABLE_BROAD_DISCOVERY_FALLBACK:
        # Safety: slug found 0 – run broad fallback regardless
        run_broad = True

    _last_broad_skipped = not run_broad

    # ── Initialise broad-discovery counters (needed for summary even when skipped) ─
    raw_gamma_general = raw_gamma_search = 0
    raw_event_general = raw_event_search = raw_nested = 0
    raw_clob = 0

    if run_broad:
        log.info("Starting broad/general discovery …")
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            (
                (gamma_mkts, raw_gamma_general, raw_gamma_search),
                (event_mkts, raw_event_general, raw_event_search, raw_nested),
                (clob_mkts,  raw_clob),
            ) = await asyncio.gather(
                _fetch_gamma_markets(sess),
                _fetch_gamma_events(sess),
                _fetch_clob_markets(sess),
            )

        all_raws = gamma_mkts + event_mkts + clob_mkts

        for raw in all_raws:
            key = _dedup_key(raw)
            if key in seen_ids:
                duplicates_skipped += 1
                continue
            if key:
                seen_ids.add(key)

            source = raw.get("_source", "unknown")
            m = _parse_market(
                raw, reject_counts,
                source=source,
                debug_wrong_crypto=debug_wrong_crypto,
                debug_updown_candidates=debug_updown_candidates,
            )
            if m:
                if m.market_id not in {r.market_id for r in results}:
                    results.append(m)

    # ── Discovery summary ─────────────────────────────────────────────────────
    if not QUIET_MODE:
        broad_note = f"  Broad discovery skipped: {broad_skip_reason}\n" if not run_broad else ""
        log.info(
            f"\nDiscovery summary:\n"
            f"  Slug candidates generated:            {slug_stats['candidates_generated']}\n"
            f"  Slug events found:                    {slug_stats['events_found']}\n"
            f"  Slug markets found:                   {slug_stats['markets_found']}\n"
            f"  Slug nested markets found:            {slug_stats['nested_markets_found']}\n"
            f"  Slug valid markets accepted:          {slug_valid}\n"
            f"{broad_note}"
            f"  Raw targeted market search fetched:   {raw_gamma_search}\n"
            f"  Raw targeted event search fetched:    {raw_event_search}\n"
            f"  Raw Gamma markets general:            {raw_gamma_general}\n"
            f"  Raw Gamma events general:             {raw_event_general}\n"
            f"  Raw nested event markets found:       {raw_nested}\n"
            f"  Raw CLOB markets fetched:             {raw_clob}\n"
            f"  Possible updown candidates:           {len(debug_updown_candidates)}\n"
            f"  Valid markets accepted:               {len(results)}\n"
            f"  Wrong market type rejected:           {reject_counts['wrong_market_type']}\n"
            f"  Rejected wrong_asset:                 {reject_counts['wrong_asset']}\n"
            f"  Rejected unknown_timeframe:           {reject_counts['unknown_timeframe']}\n"
            f"  Rejected missing_tokens:              {reject_counts['missing_tokens']}\n"
            f"  Rejected missing_end_time:            {reject_counts['missing_end_time']}\n"
            f"  Rejected closed:                      {reject_counts['closed']}\n"
            f"  Rejected unrelated_category:          {reject_counts['unrelated_category']}\n"
            f"  Duplicates skipped:                   {duplicates_skipped}"
        )
    current_results = _select_current_markets(results)

    if not QUIET_MODE:
        log.info(
            f"  Current-market filter: raw_valid={len(results)} | "
            f"current_unique={len(current_results)} | "
            f"old_or_future_skipped={len(results) - len(current_results)}"
        )

    return current_results


# ─────────────────────────── MAIN ORCHESTRATOR ───────────────────────────────

trackers:            Dict[str, MarketTracker] = {}
known_ids:           set[str]                 = set()
scan_num:            int                      = 0
last_discovery_ts:   Optional[datetime]       = None


_last_broad_skipped: bool                     = False
_poly_price_cache: Dict[str, Tuple[float, datetime, str]] = {}
_poly_price_status: str = "starting"
_poly_price_reconnect_count: int = 0
_poly_price_last_msg: Optional[datetime] = None
_poly_price_source_label: str = "POLY-CHAINLINK"
_binance_price_cache: Dict[str, Tuple[float, datetime]] = {}
_binance_price_status: str = "starting"
_binance_price_reconnect_count: int = 0
_binance_price_last_msg: Optional[datetime] = None


async def _poly_price_ping_loop(ws) -> None:
    while True:
        await asyncio.sleep(PRICE_PING_INTERVAL)
        try:
            await ws.send("PING")
        except Exception:
            return


def _poly_price_symbol_to_asset(symbol: str) -> Optional[str]:
    key = str(symbol or "").lower().strip()
    if not key:
        return None
    return _CHAINLINK_SYMBOL_TO_ASSET.get(key)


def _binance_price_stream_url() -> str:
    streams = "/".join(f"{symbol}@trade" for symbol in BINANCE_PRICE_SYMBOLS.values())
    return f"{BINANCE_WS_URL}{streams}"


def _extract_binance_price_update(raw: str | bytes) -> Dict[str, float]:
    try:
        data = json.loads(raw)
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}

    payload = data.get("data")
    if isinstance(payload, dict):
        data = payload

    if data.get("e") != "trade":
        return {}

    symbol = str(data.get("s") or "").lower()
    asset = _BINANCE_SYMBOL_TO_ASSET.get(symbol)
    if asset is None:
        return {}

    price, _reason = _coerce_ptb_price(asset, data.get("p"))
    if price is None:
        return {}

    accepted, _reason = _pick_ptb_candidate_for_asset(asset, [price])
    if accepted is None:
        return {}

    return {asset: accepted}


async def binance_current_price_feed_loop() -> None:
    global _binance_price_status, _binance_price_reconnect_count, _binance_price_last_msg

    url = _binance_price_stream_url()
    while True:
        try:
            _binance_price_status = "connecting"
            async with websockets.connect(
                url,
                ping_interval=None,
                open_timeout=15,
                close_timeout=5,
            ) as ws:
                _binance_price_status = "waiting"
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=BINANCE_NO_TICK_TIMEOUT)
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError("no Binance trade ticks") from exc

                    if isinstance(raw, bytes):
                        raw_text = raw.decode("utf-8", errors="ignore")
                    else:
                        raw_text = str(raw)

                    if "serverShutdown" in raw_text:
                        raise RuntimeError("Binance server shutdown")

                    updates = _extract_binance_price_update(raw_text)
                    if updates:
                        now = datetime.now(timezone.utc)
                        _binance_price_last_msg = now
                        for asset, price in updates.items():
                            _binance_price_cache[asset] = (price, now)
                            _binance_history_record(asset, now, price)
                        _binance_price_status = "ok"
                        if _binance_price_event is not None:
                            _binance_price_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _binance_price_status = "reconnecting"
            _binance_price_reconnect_count += 1
            log.debug(
                f"Binance current-price feed error ({exc}). "
                f"Retry #{_binance_price_reconnect_count} in {BINANCE_RECONNECT_DELAY}s"
            )
            await asyncio.sleep(BINANCE_RECONNECT_DELAY)


def _extract_poly_price_updates(raw: str | bytes) -> Dict[str, Tuple[float, str]]:
    try:
        data = json.loads(raw)
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}
    if data.get("topic") != "crypto_prices_chainlink":
        return {}

    payload = data.get("payload")
    if not isinstance(payload, dict):
        return {}

    symbol = str(payload.get("symbol") or "").lower()
    asset = _poly_price_symbol_to_asset(symbol)
    if asset is None:
        return {}

    price, _reason = _coerce_ptb_price(asset, payload.get("value"))
    if price is None:
        return {}

    accepted, _reason = _pick_ptb_candidate_for_asset(asset, [price])
    if accepted is None:
        return {}

    return {asset: (accepted, "POLY-CHAINLINK")}


def _poly_price_subscription() -> dict:
    return {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_chainlink",
                "type": "update",
            }
        ],
    }


async def polymarket_current_price_feed_loop() -> None:
    global _poly_price_status, _poly_price_reconnect_count, _poly_price_last_msg, _poly_price_source_label

    while True:
        try:
            _poly_price_status = "connecting"
            subscription = _poly_price_subscription()

            async with websockets.connect(RTDS_URL, ping_interval=None, open_timeout=15, close_timeout=5) as ws:
                await ws.send(json.dumps(subscription))
                log.debug(f"Polymarket current-price subscribed: {subscription}")
                _poly_price_status = "waiting"
                ping_task = asyncio.create_task(_poly_price_ping_loop(ws), name="poly-price-ping")

                try:
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=PRICE_NO_TICK_TIMEOUT)
                        except asyncio.TimeoutError as exc:
                            raise RuntimeError("no Polymarket RTDS price ticks") from exc

                        if isinstance(raw, bytes):
                            raw_text = raw.decode("utf-8", errors="ignore")
                        else:
                            raw_text = str(raw)

                        if raw_text == "PING":
                            await ws.send("PONG")
                            continue
                        if raw_text == "PONG":
                            continue

                        updates = _extract_poly_price_updates(raw_text)
                        if updates:
                            now = datetime.now(timezone.utc)
                            _poly_price_last_msg = now
                            for asset, (price, source_label) in updates.items():
                                _poly_price_cache[asset] = (price, now, source_label)
                                _poly_price_source_label = source_label
                            _poly_price_status = "ok"
                finally:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _poly_price_status = "reconnecting"
            _poly_price_reconnect_count += 1
            log.debug(
                f"Polymarket current-price feed error ({exc}). "
                f"Retry #{_poly_price_reconnect_count} in {PRICE_RECONNECT_DELAY}s"
            )
            await asyncio.sleep(PRICE_RECONNECT_DELAY)


def _refresh_tracker_current_price(t: MarketTracker) -> None:
    asset = str(t.market.asset or "").upper().strip()
    cached = _poly_price_cache.get(asset)
    t.current_price_source = _poly_price_source_label

    if cached is None:
        t.current_price_status = _poly_price_status
        return

    price, updated, source_label = cached
    t.current_price_source = source_label
    t.current_price = price
    t.current_price_updated = updated
    age_ms = (datetime.now(timezone.utc) - updated).total_seconds() * 1000
    t.current_price_status = "ok" if age_ms <= PRICE_STALE_MS else _poly_price_status


def _refresh_tracker_binance_price(t: MarketTracker) -> None:
    asset = str(t.market.asset or "").upper().strip()
    cached = _binance_price_cache.get(asset)

    if cached is None:
        t.binance_price_status = _binance_price_status
        return

    price, updated = cached
    t.binance_price = price
    t.binance_price_updated = updated
    age_ms = (datetime.now(timezone.utc) - updated).total_seconds() * 1000
    t.binance_price_status = "ok" if age_ms <= PRICE_STALE_MS else _binance_price_status


def _secs_remaining(m: MarketInfo) -> float:
    return (m.candle_end - datetime.now(timezone.utc)).total_seconds()

def _in_window(m: MarketInfo) -> Tuple[bool, int]:
    secs = _secs_remaining(m)
    if secs <= 0:
        return False, 0
    win = SIGNAL_WINDOWS.get(m.timeframe, 0)
    return (secs <= win), int(secs)


def _display_pct(book: TokenBook) -> str:
    pct = book.pct()
    return str(pct) if pct is not None else "N/A"


def _display_bid(book: TokenBook) -> str:
    b = book.best_bid
    return str(round(b * 100)) if b is not None else "N/A"


def _display_ask(book: TokenBook) -> str:
    a = book.best_ask
    return str(round(a * 100)) if a is not None else "N/A"


def _display_spread(book: TokenBook) -> str:
    b, a = book.best_bid, book.best_ask
    if b is None or a is None:
        return "N/A"
    return str(round((a - b) * 100, 1))


def _book_usd_depth(book: TokenBook) -> float:
    total = sum(float(p) * s for p, s in book.bids.items())
    total += sum(float(p) * s for p, s in book.asks.items())
    return total


def _format_one_book(book: TokenBook, label: str) -> List[str]:
    def _valid(p: float) -> bool:
        return 1 <= round(p * 100) <= 99

    asks = sorted([(float(p), s) for p, s in book.asks.items() if _valid(float(p))], reverse=True)
    bids = sorted([(float(p), s) for p, s in book.bids.items() if _valid(float(p))], reverse=True)

    b, a = book.best_bid, book.best_ask
    last_str   = f"{round(a * 100)}¢"          if a is not None else "N/A"
    spread_str = f"{round((a - b) * 100, 1)}¢" if a is not None and b is not None else "N/A"

    lines = [f"    [{label}]"]
    for price, size in asks:
        lines.append(f"      {round(price*100):>3}¢ | {size:>8.2f} sh | ${price*size:>8.2f}")
    lines.append(f"    ── Last: {last_str} | Spread: {spread_str} ──")
    for price, size in bids:
        lines.append(f"      {round(price*100):>3}¢ | {size:>8.2f} sh | ${price*size:>8.2f}")
    return lines


def _format_combined_book(yes_book: TokenBook, no_book: TokenBook) -> List[str]:
    up_lines   = _format_one_book(yes_book, "Trade Up")
    down_lines = _format_one_book(no_book,  "Trade Down")

    col_w = max((len(l) for l in up_lines + down_lines), default=40) + 4
    rows  = max(len(up_lines), len(down_lines))

    result = []
    for i in range(rows):
        left  = up_lines[i]   if i < len(up_lines)   else ""
        right = down_lines[i] if i < len(down_lines) else ""
        result.append(f"{left:<{col_w}}{right}")
    return result


def _display_liq_combined(yes_book: TokenBook, no_book: TokenBook) -> str:
    total = _book_usd_depth(yes_book) + _book_usd_depth(no_book)
    if total == 0:
        return "N/A"
    if total >= 1000:
        return f"${total / 1000:.1f}k"
    return f"${total:.0f}"


def _format_usd_price(price: float) -> str:
    if price >= 100:
        return f"${price:,.2f}"

    return f"${price:,.4f}"


def _display_price_to_beat(t: MarketTracker) -> str:
    price = getattr(t, "price_to_beat", None)
    err = getattr(t, "price_to_beat_error", "not_loaded")

    if price is None:
        return f"N/A:{err}"

    return _format_usd_price(price)


def _display_current_price(t: MarketTracker) -> str:
    _refresh_tracker_current_price(t)
    price = getattr(t, "current_price", None)
    source = getattr(t, "current_price_source", "N/A")
    status = getattr(t, "current_price_status", "not_loaded")

    if price is None:
        return f"{source} N/A:{status}"

    return f"{source} {_format_usd_price(price)}"


def _display_binance_price(t: MarketTracker) -> str:
    _refresh_tracker_binance_price(t)
    price = getattr(t, "binance_price", None)
    status = getattr(t, "binance_price_status", "not_loaded")

    if price is None:
        return f"N/A:{status}"

    return _format_usd_price(price)


def _book_age_ms(t: MarketTracker) -> Optional[int]:
    candidates = [t.last_ws_msg, t.yes_book.last_update, t.no_book.last_update]
    valid = [ts for ts in candidates if ts is not None]
    if not valid:
        return None
    latest = max(valid)
    return int((datetime.now(timezone.utc) - latest).total_seconds() * 1000)


def _price_age_ms(t: MarketTracker) -> Optional[int]:
    ts = getattr(t, "current_price_updated", None)
    if ts is None:
        return None
    return int((datetime.now(timezone.utc) - ts).total_seconds() * 1000)


def _binance_age_ms(t: MarketTracker) -> Optional[int]:
    ts = getattr(t, "binance_price_updated", None)
    if ts is None:
        return None
    return int((datetime.now(timezone.utc) - ts).total_seconds() * 1000)


def _display_trackers() -> List[MarketTracker]:
    """Current, deduplicated trackers for printing only."""
    now = datetime.now(timezone.utc)
    best: Dict[Tuple[str, str], MarketTracker] = {}

    for t in trackers.values():
        if not _is_current_live_market(t.market, now=now):
            continue
        key = _current_tracker_key(t.market)
        existing = best.get(key)
        if existing is None or _market_selection_rank(t.market, now) < _market_selection_rank(existing.market, now):
            best[key] = t

    tf_order = {"5min": 0}
    asset_order = {asset: i for i, asset in enumerate(ASSETS)}
    return sorted(
        best.values(),
        key=lambda t: (
            tf_order.get(t.market.timeframe, 99),
            asset_order.get(t.market.asset, 99),
            t.market.candle_end,
        ),
    )


def _remove_tracker(mid: str, reason: str) -> bool:
    t = trackers.pop(mid, None)
    if t is None:
        return False
    known_ids.discard(mid)
    asyncio.create_task(t.stop())
    if not QUIET_MODE:
        log.info(f"Tracker removed ({reason}): {t.market.asset} {t.market.timeframe}")
    return True


async def run_discovery() -> None:
    global trackers, known_ids, last_discovery_ts
    try:
        markets = await fetch_markets()
    except Exception as exc:
        log.error(f"Discovery failed: {exc}")
        return

    now = datetime.now(timezone.utc)

    # fetch_markets() already returns max one current market per asset/timeframe,
    # but keep this guard here so run_discovery() never starts duplicates.
    incoming_by_key: Dict[Tuple[str, str], MarketInfo] = {}
    for m in markets:
        if not _is_current_live_market(m, now=now):
            continue
        key = _current_tracker_key(m)
        existing = incoming_by_key.get(key)
        if existing is None or _market_selection_rank(m, now) < _market_selection_rank(existing, now):
            incoming_by_key[key] = m

    new_count = 0
    removed_count = 0
    replaced_count = 0

    # Remove any already-running trackers that are expired or clearly future candles.
    for mid, t in list(trackers.items()):
        if not _is_current_live_market(t.market, now=now):
            if _remove_tracker(mid, "expired/old/future"):
                removed_count += 1

    # If older code already started duplicate trackers for the same asset/timeframe,
    # keep only the best current one until the incoming current market replaces it.
    existing_by_key: Dict[Tuple[str, str], List[MarketTracker]] = {}
    for t in trackers.values():
        existing_by_key.setdefault(_current_tracker_key(t.market), []).append(t)

    for key, dupes in existing_by_key.items():
        if len(dupes) <= 1:
            continue
        keep = min(dupes, key=lambda tr: _market_selection_rank(tr.market, now))
        for tr in dupes:
            if tr.market.market_id != keep.market.market_id:
                if _remove_tracker(tr.market.market_id, "duplicate asset/timeframe"):
                    removed_count += 1

    # Start or replace exactly one tracker per incoming asset/timeframe key.
    for key, m in incoming_by_key.items():
        same_key = [
            (mid, t) for mid, t in trackers.items()
            if _current_tracker_key(t.market) == key
        ]

        already_running = any(t.market.market_id == m.market_id for _mid, t in same_key)
        if already_running:
            # Remove any extra same-key trackers that survived from older runs.
            for mid, t in same_key:
                if t.market.market_id != m.market_id:
                    if _remove_tracker(mid, "replaced by current market"):
                        removed_count += 1
                        replaced_count += 1
            continue

        # A better/current market exists for this asset/timeframe: remove old same-key tracker(s).
        for mid, _t in same_key:
            if _remove_tracker(mid, "replaced by current market"):
                removed_count += 1
                replaced_count += 1

        if m.market_id not in known_ids:
            known_ids.add(m.market_id)
            t = MarketTracker(m)
            trackers[m.market_id] = t
            t.start()
            new_count += 1
            if not QUIET_MODE:
                log.info(f"Tracker started: {m.asset} {m.timeframe} | {m.title[:60]}")

    last_discovery_ts = datetime.now(timezone.utc)


async def discovery_loop() -> None:
    await asyncio.sleep(DISCOVERY_INTERVAL)
    while True:
        if not QUIET_MODE:
            log.info("Discovery refresh …")
        try:
            await run_discovery()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(f"Discovery loop error: {exc}. Retrying in {DISCOVERY_INTERVAL}s")
        await asyncio.sleep(DISCOVERY_INTERVAL)


def _next_windows_by_tf(all_trackers: List[MarketTracker], max_per_tf: int = 5) -> Dict[str, List[str]]:
    """Return next-window lines grouped by timeframe, up to max_per_tf each."""
    by_tf: Dict[str, list] = {"5min": []}
    for t in all_trackers:
        m = t.market
        secs_rem = _secs_remaining(m)
        if secs_rem <= 0:
            continue
        win = SIGNAL_WINDOWS.get(m.timeframe, 0)
        if secs_rem <= win:
            continue  # already in window
        tf = m.timeframe
        if tf not in by_tf:
            continue
        window_in = int(secs_rem - win)
        end_str = m.candle_end.strftime("%H:%M:%S")
        by_tf[tf].append((window_in, int(secs_rem), m.asset, end_str))

    result: Dict[str, List[str]] = {}
    for tf in ["5min"]:
        entries = sorted(by_tf[tf], key=lambda x: x[0])[:max_per_tf]
        result[tf] = [
            f"  {asset} {tf}: window in {win_in}s | remaining: {rem}s | end: {end}"
            for win_in, rem, asset, end in entries
        ]
    return result


# ─────────────────────────── FOLLOW LEADER STRATEGY ─────────────────────────
# Late-candle momentum strategy. Backtest on ~465 5min candles:
#   86.9% winrate, +30.7c PnL/trade (parameters: lead>=0.01% rem<=15s ask<=75c).
#
# Logic: in the last ~15 seconds of a 5min candle, if the Binance price is
# clearly leading the price-to-beat (>=0.01% in either direction), the candle
# is very unlikely to flip in time. Buy the leading side at the order book ask.

FL_ENABLED = os.getenv("FL_ENABLED", "true").strip().lower() in ("true", "1", "yes")
FL_PAPER_ONLY = os.getenv("FL_PAPER_ONLY", "true").strip().lower() in ("true", "1", "yes")
FL_ALLOWED_ASSETS = {
    a.strip().upper()
    for a in os.getenv("FL_ALLOWED_ASSETS", "BTC,ETH").split(",")
    if a.strip()
}
FL_MIN_SECS_LEFT       = int(os.getenv("FL_MIN_SECS_LEFT", "5"))
FL_MAX_SECS_LEFT       = int(os.getenv("FL_MAX_SECS_LEFT", "15"))
FL_MIN_LEAD_PCT        = float(os.getenv("FL_MIN_LEAD_PCT", "0.0001"))
# Systematic Binance > PTB bias correction for DOWN signals (~15-19 bps measured).
# Binance trades above the Chainlink oracle (PTB) by ~18 bps on average, making
# DOWN signals structurally rare. This offset shifts the DOWN comparison point so
# DOWN has the same effective chance to fire as UP.
FL_DOWN_BIAS_OFFSET    = float(os.getenv("FL_DOWN_BIAS_OFFSET", "0.0015"))
FL_DOWN_REQUIRE_PM_AGREE = os.getenv("FL_DOWN_REQUIRE_PM_AGREE", "false").strip().lower() in ("true", "1", "yes")
FL_MIN_ENTRY_ASK       = float(os.getenv("FL_MIN_ENTRY_ASK", "0.35"))   # was 0.30; v3 data: 0.30-0.40 bucket = 31.5% WR, -$20.45
FL_MAX_ENTRY_ASK       = float(os.getenv("FL_MAX_ENTRY_ASK", "0.75"))
# ── Time filters (data-driven from v3 1113-trade analysis) ───────────────────
# Hours UTC to skip (comma-separated). v3 worst: 00:00 (45.1% WR, -$28.70), 02:00 (45.2%, -$31.30),
# 08:00 (51.5%, -$0.20), 17:00 (52.8%, -$14.35). Default skips 0,1,8,9 per user request.
_skip_hours_raw = os.getenv("FL_SKIP_HOURS_UTC", "0,1,8,9").strip()
FL_SKIP_HOURS_UTC = {
    int(h.strip()) for h in _skip_hours_raw.split(",") if h.strip().isdigit()
} if _skip_hours_raw else set()
# Skip weekend days (Saturday=5, Sunday=6). v3 data: Sat 54.5% WR +$1.95 (basically deadweight).
FL_SKIP_WEEKENDS = os.getenv("FL_SKIP_WEEKENDS", "true").strip().lower() in ("true", "1", "yes")
FL_FIXED_SHARES        = float(os.getenv("FL_FIXED_SHARES", "5"))
FL_MAX_OPEN_TRADES     = int(os.getenv("FL_MAX_OPEN_TRADES", "2"))
FL_TIER                = int(os.getenv("FL_TIER", "2"))
FL_SCAN_STATS_INTERVAL = float(os.getenv("FL_SCAN_STATS_INTERVAL", "1.0"))

_PAPER_TRADE = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true", "1", "yes")
_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "crypto_ghost_PAPER.db" if _PAPER_TRADE else "crypto_ghost.db",
)

_BINANCE_HISTORY_WINDOW_SEC = 60
_binance_price_history: Dict[str, Deque[Tuple[datetime, float]]] = {
    a: deque() for a in ASSETS
}

# Rolling Binance-PTB spread history for dynamic DOWN bias correction.
# Window: 5 minutes. Minimum 30 samples before dynamic bias kicks in.
_SPREAD_WINDOW_SEC      = int(os.getenv("FL_SPREAD_WINDOW_SEC", "60"))
_SPREAD_MIN_SAMPLES     = int(os.getenv("FL_SPREAD_MIN_SAMPLES", "30"))
_spread_history: Dict[str, Deque[Tuple[datetime, float]]] = {
    a: deque() for a in ASSETS
}

_last_scan_stats_ts: float = 0.0

# Event set by Binance feed on every new price tick.
# entry_watcher_loop waits on this instead of polling on SCAN_INTERVAL.
_binance_price_event: Optional[asyncio.Event] = None


def _strategy_db_connect() -> sqlite3.Connection:
    return sqlite3.connect(_DB_PATH, timeout=10)


_RESET_ON_START = os.getenv("FL_RESET_ON_START", "true").strip().lower() in ("true", "1", "yes")


def _strategy_ensure_schema() -> None:
    try:
        conn = _strategy_db_connect()
    except Exception:
        return
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, tier INTEGER, coin TEXT,
            market_id TEXT, token_id TEXT, question TEXT, entry_price REAL,
            size_usdc REAL, order_id TEXT, status TEXT DEFAULT 'open',
            pnl REAL DEFAULT 0, closed_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, icon TEXT, msg TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS scan_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
            markets_found INTEGER DEFAULT 0, signals_passed INTEGER DEFAULT 0,
            skip_ask INTEGER DEFAULT 0, skip_liq INTEGER DEFAULT 0,
            skip_certainty INTEGER DEFAULT 0, skip_window INTEGER DEFAULT 0,
            scan_ms INTEGER DEFAULT 0)""")
        conn.commit()

        if _RESET_ON_START:
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM scan_stats")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('trades','events','scan_stats')")
            conn.commit()
            log.warning("RESET_ON_START: cleared trades, events, scan_stats")
        for ddl in [
            "ALTER TABLE trades ADD COLUMN pm_result TEXT DEFAULT 'pending'",
            "ALTER TABLE trades ADD COLUMN outcome TEXT",
            "ALTER TABLE trades ADD COLUMN price_start REAL",
            "ALTER TABLE trades ADD COLUMN price_end REAL",
            "ALTER TABLE trades ADD COLUMN resolution_verified INTEGER DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN condition_id TEXT",
            "ALTER TABLE trades ADD COLUMN redeemed INTEGER DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN redeemed_at TEXT",
            "ALTER TABLE trades ADD COLUMN redeem_tx_hash TEXT",
            "ALTER TABLE trades ADD COLUMN binance_price REAL",
            "ALTER TABLE trades ADD COLUMN price_to_beat REAL",
        ]:
            try:
                conn.execute(ddl)
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


def _strategy_log_event(icon: str, msg: str) -> None:
    try:
        conn = _strategy_db_connect()
        conn.execute(
            "INSERT INTO events (ts, icon, msg) VALUES (?,?,?)",
            (datetime.now().strftime("%H:%M:%S"), icon, str(msg)[:120]),
        )
        conn.execute(
            "DELETE FROM events WHERE id NOT IN "
            "(SELECT id FROM events ORDER BY id DESC LIMIT 100)"
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _strategy_persist_scan_stats(
    *,
    markets_found: int,
    signals_passed: int,
    skip_ask: int,
    skip_liq: int,
    skip_certainty: int,
    skip_window: int,
    scan_ms: int,
) -> None:
    try:
        conn = _strategy_db_connect()
        conn.execute(
            "INSERT INTO scan_stats (ts, markets_found, signals_passed, skip_ask, "
            "skip_liq, skip_certainty, skip_window, scan_ms) VALUES (?,?,?,?,?,?,?,?)",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                markets_found,
                signals_passed,
                skip_ask,
                skip_liq,
                skip_certainty,
                skip_window,
                scan_ms,
            ),
        )
        conn.execute(
            "DELETE FROM scan_stats WHERE id NOT IN "
            "(SELECT id FROM scan_stats ORDER BY id DESC LIMIT 500)"
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _strategy_persist_trade(
    *,
    coin: str,
    market_id: str,
    token_id: str,
    question: str,
    entry_ask: float,
    outcome: str,
    binance_price: Optional[float] = None,
    price_to_beat: Optional[float] = None,
) -> bool:
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        size_usdc = round(FL_FIXED_SHARES * entry_ask, 4)
        conn = _strategy_db_connect()
        try:
            open_n = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status='open'"
            ).fetchone()[0]
            if open_n >= FL_MAX_OPEN_TRADES:
                return False
            dup = conn.execute(
                "SELECT 1 FROM trades WHERE market_id=? AND status='open' LIMIT 1",
                (market_id,),
            ).fetchone()
            if dup:
                return False
            conn.execute(
                "INSERT INTO trades (ts, tier, coin, market_id, token_id, question, "
                "entry_price, size_usdc, status, outcome, pm_result, binance_price, price_to_beat) "
                "VALUES (?,?,?,?,?,?,?,?, 'open', ?, 'pending', ?, ?)",
                (
                    ts,
                    FL_TIER,
                    coin,
                    market_id,
                    token_id,
                    question,
                    round(entry_ask, 4),
                    size_usdc,
                    outcome,
                    round(binance_price, 4) if binance_price is not None else None,
                    round(price_to_beat, 4) if price_to_beat is not None else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        _strategy_log_event(
            "🎯",
            f"OPEN T{FL_TIER} {coin} {outcome} ${size_usdc:.2f}@{entry_ask:.3f}",
        )
        return True
    except Exception as exc:
        log.debug(f"persist trade error: {exc}", exc_info=True)
        return False


def _binance_history_record(asset: str, ts: datetime, price: float) -> None:
    dq = _binance_price_history.get(asset)
    if dq is None:
        return
    dq.append((ts, price))
    cutoff = ts - timedelta(seconds=_BINANCE_HISTORY_WINDOW_SEC + 5)
    while dq and dq[0][0] < cutoff:
        dq.popleft()


def _binance_price_at_offset(
    asset: str, now: datetime, seconds_back: float, max_diff: float = 3.0
) -> Optional[float]:
    dq = _binance_price_history.get(asset)
    if not dq:
        return None
    target = now - timedelta(seconds=seconds_back)
    best_price: Optional[float] = None
    best_diff: Optional[float] = None
    for ts, p in dq:
        diff = abs((ts - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best_price = p
            best_diff = diff
    if best_diff is None or best_diff > max_diff:
        return None
    return best_price


def _binance_history_age_seconds(asset: str, now: datetime) -> Optional[float]:
    dq = _binance_price_history.get(asset)
    if not dq:
        return None
    return (now - dq[0][0]).total_seconds()


def _spread_record(asset: str, ts: datetime, bin_price: float, ptb: float) -> None:
    """Record one (bin - ptb) / ptb sample into the rolling spread history."""
    dq = _spread_history.get(asset)
    if dq is None:
        return
    spread = (bin_price - ptb) / ptb
    dq.append((ts, spread))
    cutoff = ts - timedelta(seconds=_SPREAD_WINDOW_SEC + 10)
    while dq and dq[0][0] < cutoff:
        dq.popleft()


def _get_dynamic_bias(asset: str) -> float:
    """Return rolling mean spread as DOWN bias offset.
    Falls back to FL_DOWN_BIAS_OFFSET if fewer than _SPREAD_MIN_SAMPLES exist."""
    dq = _spread_history.get(asset)
    if not dq or len(dq) < _SPREAD_MIN_SAMPLES:
        return FL_DOWN_BIAS_OFFSET
    values = [s for _, s in dq]
    return sum(values) / len(values)


def _evaluate_follow_leader(
    t: MarketTracker, now: datetime, secs_left: int
) -> Tuple[Optional[str], Optional[float], str]:
    """
    FOLLOW LEADER: in the last ~15s of a 5min candle, if the Binance price is
    clearly leading the price-to-beat by >= FL_MIN_LEAD_PCT, buy that side at
    the book ask (provided ask is in [FL_MIN_ENTRY_ASK, FL_MAX_ENTRY_ASK]).

    Returns (side, entry_ask, reason).  Reasons used by scan stats:
      - outside_entry_window_too_early / too_late
      - no_data / pm_current_missing / binance_current_missing / missing_entry_ask
      - prices_disagree / lead_too_small
      - entry_ask_too_low / entry_ask_too_high
      - ok
    """
    asset = (t.market.asset or "").upper().strip()
    if asset not in FL_ALLOWED_ASSETS:
        return None, None, "asset_not_allowed"
    if t.market.timeframe != "5min":
        return None, None, "not_5min_market"
    if secs_left > FL_MAX_SECS_LEFT:
        return None, None, "outside_entry_window_too_early"
    if secs_left < FL_MIN_SECS_LEFT:
        return None, None, "outside_entry_window_too_late"
    # ── Time filters (data-driven from v3 analysis) ──────────────────────────
    now_utc = now if now.tzinfo == timezone.utc else now.astimezone(timezone.utc)
    if FL_SKIP_WEEKENDS and now_utc.weekday() >= 5:   # 5=Sat, 6=Sun
        return None, None, "skip_weekend"
    if now_utc.hour in FL_SKIP_HOURS_UTC:
        return None, None, "skip_hour"

    ptb = t.price_to_beat
    if ptb is None or ptb <= 0:
        return None, None, "no_data"

    bin_now = t.binance_price
    if bin_now is None or t.binance_price_status != "ok":
        return None, None, "binance_current_missing"

    # Use the rolling mean of (bin-ptb)/ptb over the last _SPREAD_WINDOW_SEC as the
    # bias offset. This makes the UP/DOWN threshold track the actual live spread so
    # both directions have an equal effective chance regardless of market regime.
    # Falls back to static FL_DOWN_BIAS_OFFSET until enough samples are collected.
    bias = _get_dynamic_bias(asset)
    bin_adjusted = bin_now - ptb * bias

    if bin_adjusted > ptb:
        side = "UP"
        lead_pct = (bin_now - ptb) / ptb
    elif bin_adjusted < ptb:
        side = "DOWN"
        lead_pct = (ptb - bin_adjusted) / ptb
    else:
        return None, None, "lead_too_small"

    if lead_pct < FL_MIN_LEAD_PCT:
        log.info(
            f"SKIP FL {asset} reason=lead_too_small "
            f"ptb={ptb:.2f} bin={bin_now:.2f} bias={bias:.6f} bin_adj={bin_adjusted:.2f} "
            f"lead={lead_pct:.6f} min={FL_MIN_LEAD_PCT:.6f} side={side} secs_left={secs_left}"
        )
        return None, None, "lead_too_small"

    # Optional safety: if PM oracle is also available, it must agree on side.
    # For DOWN we skip this check — PM oracle has the same structural bias as PTB,
    # so it almost always shows UP even during genuine DOWN moves.
    cur = t.current_price
    if cur is not None and t.current_price_status == "ok":
        pm_side = "UP" if cur > ptb else "DOWN" if cur < ptb else None
        if pm_side is not None and pm_side != side:
            if side == "UP" or FL_DOWN_REQUIRE_PM_AGREE:
                log.info(
                    f"SKIP FL {asset} reason=prices_disagree "
                    f"ptb={ptb:.2f} pm={cur:.2f} bin={bin_now:.2f} "
                    f"pm_side={pm_side} bin_side={side} secs_left={secs_left}"
                )
                return None, None, "prices_disagree"

    book = t.yes_book if side == "UP" else t.no_book
    ask = book.best_ask
    if ask is None:
        return None, None, "missing_entry_ask"
    if ask < FL_MIN_ENTRY_ASK:
        log.info(
            f"SKIP FL {asset} reason=entry_ask_too_low "
            f"ask={ask:.3f} min={FL_MIN_ENTRY_ASK:.2f} side={side}"
        )
        return None, None, "entry_ask_too_low"
    if ask > FL_MAX_ENTRY_ASK:
        log.info(
            f"SKIP FL {asset} reason=entry_ask_too_high "
            f"ask={ask:.3f} max={FL_MAX_ENTRY_ASK:.2f} side={side}"
        )
        return None, None, "entry_ask_too_high"

    log.info(
        f"ENTRY FL {asset} {side} ask={ask:.3f} shares={FL_FIXED_SHARES:.0f} "
        f"cost={FL_FIXED_SHARES * ask:.2f} ptb={ptb:.2f} bin={bin_now:.2f} "
        f"bias={bias:.6f} bin_adj={bin_adjusted:.2f} lead={lead_pct:.6f} secs_left={secs_left}"
    )
    return side, float(ask), "ok"


def _strategy_open_trades_count() -> int:
    try:
        conn = _strategy_db_connect()
        n = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


def _strategy_market_already_open(market_id: str) -> bool:
    try:
        conn = _strategy_db_connect()
        row = conn.execute(
            "SELECT 1 FROM trades WHERE market_id=? AND status='open' LIMIT 1",
            (market_id,),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


# ─────────────────────────── SCAN PRINTER (keepalive) ────────────────────────
# Spread sampling is handled by entry_watcher_loop on every Binance tick.
# This loop is kept only as a named supervisor slot so _run_forever has
# something to watch; it does nothing meaningful on its own.

async def scan_printer() -> None:
    while True:
        await asyncio.sleep(3600)


# ─────────────────────────── ENTRY WATCHER ───────────────────────────────────
# Event-driven: fires immediately on every Binance price tick (~10-50ms).
# No polling delay — entry is checked as soon as new price data arrives.

async def entry_watcher_loop() -> None:
    global _last_scan_stats_ts
    assert _binance_price_event is not None

    markets_found = 0
    signals_passed = 0
    skip_ask = 0
    skip_liq = 0
    skip_certainty = 0
    skip_window = 0

    strategy_active = (
        FL_ENABLED
        and (not FL_PAPER_ONLY or _PAPER_TRADE)
    )

    while True:
        await _binance_price_event.wait()
        _binance_price_event.clear()

        tick_start = time.perf_counter()
        now = datetime.now(timezone.utc)
        all_t = _display_trackers()

        for t in all_t:
            _refresh_tracker_current_price(t)
            _refresh_tracker_binance_price(t)

        # Update spread history on every Binance tick.
        for t in all_t:
            asset = (t.market.asset or "").upper().strip()
            if asset not in FL_ALLOWED_ASSETS or t.market.timeframe != "5min":
                continue
            if (
                t.binance_price is not None
                and t.binance_price_status == "ok"
                and t.price_to_beat is not None
                and t.price_to_beat > 0
            ):
                _spread_record(asset, now, t.binance_price, t.price_to_beat)

        open_trades_count = _strategy_open_trades_count() if strategy_active else 0
        can_open_more = open_trades_count < FL_MAX_OPEN_TRADES

        for t in all_t:
            asset = (t.market.asset or "").upper().strip()
            if asset not in FL_ALLOWED_ASSETS or t.market.timeframe != "5min":
                continue
            markets_found += 1

            secs_left = int(_secs_remaining(t.market))
            side, entry_ask, reason = _evaluate_follow_leader(t, now, secs_left)

            if reason in ("outside_entry_window_too_early", "outside_entry_window_too_late"):
                skip_window += 1
            elif reason in ("pm_current_missing", "binance_current_missing",
                            "missing_entry_ask", "no_data"):
                skip_liq += 1
            elif reason in ("prices_disagree", "lead_too_small"):
                skip_certainty += 1
            elif reason in ("entry_ask_too_high", "entry_ask_too_low"):
                skip_ask += 1
            elif reason == "ok":
                signals_passed += 1
                if (
                    strategy_active
                    and can_open_more
                    and side is not None
                    and entry_ask is not None
                    and not _strategy_market_already_open(t.market.market_id)
                ):
                    placed = _strategy_persist_trade(
                        coin=asset,
                        market_id=t.market.market_id,
                        token_id=t.market.yes_token_id if side == "UP" else t.market.no_token_id,
                        question=t.market.title,
                        entry_ask=entry_ask,
                        outcome=side,
                        binance_price=t.binance_price,
                        price_to_beat=t.price_to_beat,
                    )
                    if placed:
                        open_trades_count += 1
                        can_open_more = open_trades_count < FL_MAX_OPEN_TRADES

        scan_ms = int((time.perf_counter() - tick_start) * 1000)
        now_ts = now.timestamp()
        if now_ts - _last_scan_stats_ts >= FL_SCAN_STATS_INTERVAL:
            _strategy_persist_scan_stats(
                markets_found=markets_found,
                signals_passed=signals_passed,
                skip_ask=skip_ask,
                skip_liq=skip_liq,
                skip_certainty=skip_certainty,
                skip_window=skip_window,
                scan_ms=scan_ms,
            )
            _last_scan_stats_ts = now_ts
            markets_found = signals_passed = skip_ask = 0
            skip_liq = skip_certainty = skip_window = 0



async def _run_forever(coro_fn, name: str) -> None:
    while True:
        try:
            await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(f"[supervisor] {name} crashed: {exc}. Restarting in 1s")
            await asyncio.sleep(1)


async def main() -> None:
    global _binance_price_event
    _binance_price_event = asyncio.Event()

    _strategy_ensure_schema()
    await run_discovery()
    if not trackers:
        log.warning("No matching markets found at startup – will retry every 30s.")
    await asyncio.gather(
        _run_forever(polymarket_current_price_feed_loop, "poly-price"),
        _run_forever(binance_current_price_feed_loop,    "binance-price"),
        _run_forever(discovery_loop,                     "discovery"),
        _run_forever(scan_printer,                       "spread-sampler"),
        _run_forever(entry_watcher_loop,                 "entry-watcher"),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if not QUIET_MODE:
            log.info("Stopped.")
    finally:
        pass
