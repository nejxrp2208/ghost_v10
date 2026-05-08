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
import sys
import time
import urllib.parse

import aiosqlite
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp
import websockets

# ─────────────────────────── CONSTANTS ────────────────────────────────────────

ASSETS     = ["BTC", "ETH", "SOL", "XRP", "BNB"]
CLOB_URL   = "https://clob.polymarket.com"
GAMMA_URL  = "https://gamma-api.polymarket.com"
WS_URL     = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_URL   = "wss://ws-live-data.polymarket.com"
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream?streams="

SCAN_INTERVAL        = 0.5   # seconds
DISCOVERY_INTERVAL   = 10    # seconds
DB_SUMMARY_INTERVAL  = 1     # seconds – write summary row every N seconds
DB_BOOK_INTERVAL     = 30    # seconds – write full book snapshot every N seconds
DB_PATH              = "scanner.db"
MAX_BACKOFF          = 8     # seconds
STALE_MS             = 3_000 # orderbook stale after this many ms
PRICE_STALE_MS       = 3_000 # current-price stream stale after this many ms
PRICE_NO_TICK_TIMEOUT = 15   # reconnect current-price WS if no Polymarket tick arrives
PRICE_RECONNECT_DELAY = 3    # reconnect current-price WS after this many seconds
PRICE_PING_INTERVAL   = 5    # Polymarket RTDS requires client PING messages
BINANCE_NO_TICK_TIMEOUT = 15 # reconnect Binance WS if no trade arrives
BINANCE_RECONNECT_DELAY = 3  # reconnect Binance WS after this many seconds
SUPERVISOR_RESTART_DELAY = 1 # restart crashed background loops after this many seconds

SIGNAL_WINDOWS = {"5min": 60, "15min": 90}

CHAINLINK_PRICE_SYMBOLS: Dict[str, str] = {
    "BTC": "btc/usd",
    "ETH": "eth/usd",
    "SOL": "sol/usd",
    "XRP": "xrp/usd",
    "BNB": "bnb/usd",
}

_CHAINLINK_SYMBOL_TO_ASSET = {symbol: asset for asset, symbol in CHAINLINK_PRICE_SYMBOLS.items()}

BINANCE_PRICE_SYMBOLS: Dict[str, str] = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
    "BNB": "bnbusdt",
}

_BINANCE_SYMBOL_TO_ASSET = {symbol: asset for asset, symbol in BINANCE_PRICE_SYMBOLS.items()}

# ── Output / logging config ───────────────────────────────────────────────────
QUIET_MODE             = True   # suppress INFO spam, WS debug, ACCEPT lines, etc.
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
    "SOL Up or Down",
    "Solana Up or Down",
    "XRP Up or Down",
    "BNB Up or Down",
    "crypto up or down",
    "up or down",
]

# ── Slug-based discovery config ───────────────────────────────────────────────

# asset_slug -> canonical asset
_SLUG_ASSET_MAP: Dict[str, str] = {
    "btc": "BTC",
    "eth": "ETH",
    "sol": "SOL",
    "xrp": "XRP",
    "bnb": "BNB",
}

# timeframe_slug -> (canonical_tf, seconds)
_SLUG_TF_MAP: Dict[str, Tuple[str, int]] = {
    "5m":  ("5min",  300),
    "15m": ("15min", 900),
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
    "sol-up-or-down",
    "solana-up-or-down",
    "xrp-up-or-down",
    "bnb-up-or-down",
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
    "15min": [
        r"\b15[\s\-_]?min(?:ute)?s?\b",
        r"\b15\s*m\b",
        r"\bfifteen[\s\-]?min(?:ute)?s?\b",
        r"\b15min\b",
        r"\b15[\-_]minute\b",
        r"\b15m[\-_]up[\-_]or[\-_]down\b",
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
    "SOL": "SOL", "SOLANA": "SOL", "SOL-USD": "SOL",
    "SOLANA-UP": "SOL", "SOLANA-DOWN": "SOL",
    "XRP": "XRP", "RIPPLE": "XRP", "XRP-USD": "XRP",
    "RIPPLE-UP": "XRP", "RIPPLE-DOWN": "XRP",
    "BNB": "BNB", "BINANCE COIN": "BNB", "BINANCE": "BNB",
    "BNB-USD": "BNB", "BNB-UP": "BNB", "BNB-DOWN": "BNB",
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
    r"|\b15\s*m\b"
    r")",
    re.I,
)

# ─────────────────────────── LOGGING ──────────────────────────────────────────

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

# ─────────────────────────── DATA CLASSES ─────────────────────────────────────

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


# ─────────────────────────── MARKET TRACKER ───────────────────────────────────

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
        return self.yes_book.pct()

    def no_pct(self) -> Optional[int]:
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
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10, open_timeout=15) as ws:
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


# ─────────────────────────── DISCOVERY ────────────────────────────────────────

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
    # ── Gamma format ───────────────────────────────────────────────────────────
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

    # ── CLOB format: tokens list ───────────────────────────────────────────────
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
    if secs_remaining > tf_secs + _CURRENT_MARKET_BUFFER_SEC:
        return False

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

    tf_order = {"5min": 0, "15min": 1}
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
    if raw.get("_forced_updown"):
        return True

    slug       = str(raw.get("slug", "")).lower()
    event_slug = str(raw.get("_event_slug", "")).lower()
    for pat in _UPDOWN_SLUG_PATTERNS:
        if pat in slug or pat in event_slug:
            return True

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

    asset = _detect_asset(combined)
    if not asset and raw.get("_forced_asset"):
        asset = raw["_forced_asset"]

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


# ─────────────────────────── HTTP HELPERS ─────────────────────────────────────

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


# ─────────────────────────── PRICE TO BEAT HELPERS ────────────────────────────

_PTB_CRYPTO_PRICE_SOURCE = "polymarket_crypto_price_api"
_PTB_GAMMA_SOURCE = "gamma_event_metadata"
_PTB_PREV_FINAL_SOURCE = "gamma_previous_event_final_price"
_PTB_EQUITY_SOURCE = "polymarket_equity_price_to_beat_api"
_PTB_DEBUG_FILE = "price_to_beat_api_debug.jsonl"
_PTB_DEBUG_ENABLED = False

_PTB_ASSET_RANGES: Dict[str, Tuple[float, float]] = {
    "BTC": (10000.0, 300000.0),
    "ETH": (500.0, 20000.0),
    "SOL": (5.0, 1000.0),
    "BNB": (10.0, 5000.0),
    "XRP": (0.1, 20.0),
}

_PTB_NUMERIC_RE = re.compile(
    r"[-+]?\$?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
)

_PTB_CRYPTO_UPDOWN_SLUG_RE = re.compile(
    r"^(?P<asset>btc|eth|sol|xrp|bnb)-updown-(?P<tf>5m|15m|1h)-(?P<ts>\d+)$",
    re.I,
)

_PTB_CRYPTO_VARIANTS: Dict[str, str] = {
    "5min": "fiveminute",
    "15min": "fifteen",
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

    Tries both start-timestamp and end-timestamp slug styles:
      <asset>-updown-<tf>-<current_start>
      <asset>-updown-<tf>-<current_end>
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
    candidates = _generate_slug_candidates()
    log.info(f"  Slug candidates generated: {len(candidates)}")

    out: List[dict] = []
    seen: set[str] = set()
    slug_events_found:  List[str] = []
    slug_markets_found: List[str] = []
    slug_nested_found:  List[int] = []

    timeout = aiohttp.ClientTimeout(total=_SLUG_FETCH_TIMEOUT)

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


# ─────────────────────────── FETCH FUNCTIONS ──────────────────────────────────

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


# ─────────────────────────── MAIN FETCH ───────────────────────────────────────

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

    # ── Phase 1: Slug-based discovery ─────────────────────────────────────────
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

    # ── Phase 2: Broad / general discovery (conditional) ──────────────────────
    run_broad = True
    broad_skip_reason = ""
    if FAST_STARTUP and slug_valid > 0 and not ENABLE_BROAD_DISCOVERY_FALLBACK:
        run_broad = False
        broad_skip_reason = "FAST_STARTUP active"
    elif slug_valid == 0 and not ENABLE_BROAD_DISCOVERY_FALLBACK:
        print("Slug discovery found 0 markets, running broad fallback...")
        run_broad = True

    _last_broad_skipped = not run_broad

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

    if len(results) == 0:
        print("\n" + "=" * 60)
        print("WRONG CRYPTO MARKETS SAMPLE (first 20 – have asset, not up/down)")
        print("=" * 60)
        for i, c in enumerate(debug_wrong_crypto[:20], 1):
            print(f"  #{i:2d} [{c['source']}] slug={c['slug']!r}")
            print(f"       Q: {c['question'][:100]}")
        if not debug_wrong_crypto:
            print("  (none found)")
        print("=" * 60)

        print("\n" + "=" * 60)
        print("POSSIBLE UPDOWN CANDIDATES")
        print("=" * 60)
        if not debug_updown_candidates:
            print("  (none found – no raw market contained updown/5m/15m/hourly keywords)")
        for i, c in enumerate(debug_updown_candidates, 1):
            print(f"\n  CANDIDATE #{i}")
            print(f"    source:          {c['source']}")
            print(f"    question/title:  {c['question'][:120]}")
            print(f"    slug:            {c['slug']}")
            print(f"    event_title:     {c['event_title']}")
            print(f"    event_slug:      {c['event_slug']}")
            print(f"    combined(300):   {c['combined_300']}")
            print(f"    outcomes:        {str(c['outcomes'])[:120]}")
            print(f"    clobTokenIds:    {str(c['clobTokenIds'])[:120]}")
            print(f"    tokens:          {str(c['tokens'])[:120]}")
            print(f"    endDate:         {c['endDate']}")
            print(f"    active:          {c['active']}")
            print(f"    closed:          {c['closed']}")
            print(f"    detected_asset:  {c['detected_asset']}")
            print(f"    detected_tf:     {c['detected_tf']}")
            print(f"    is_updown:       {c['is_updown']}")
        print("=" * 60)

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


# ─────────────────────────── MAIN ORCHESTRATOR ────────────────────────────────

trackers:            Dict[str, MarketTracker] = {}
known_ids:           set[str]                 = set()
scan_num:            int                      = 0
last_discovery_ts:   Optional[datetime]       = None

_db: Optional[aiosqlite.Connection] = None
_db_last_summary: Dict[str, float] = {}
_db_last_book:    Dict[str, float] = {}


async def _init_db() -> None:
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA synchronous=NORMAL")
    await _db.execute("""
        CREATE TABLE IF NOT EXISTS summary (
            ts          INTEGER NOT NULL,
            asset       TEXT    NOT NULL,
            tf          TEXT    NOT NULL,
            up_ask      INTEGER,
            up_mid      INTEGER,
            up_bid      INTEGER,
            up_spread   REAL,
            down_ask    INTEGER,
            down_mid    INTEGER,
            down_bid    INTEGER,
            down_spread REAL,
            liq_usd     REAL,
            ptb_usd     REAL,
            poly_price  REAL,
            bin_price   REAL,
            secs_left   INTEGER,
            in_window   INTEGER,
            book_age_ms INTEGER,
            price_age_ms INTEGER,
            bin_age_ms  INTEGER,
            book_ws_ok  INTEGER,
            price_ws_ok INTEGER,
            bin_ws_ok   INTEGER
        )
    """)
    await _db.execute("""
        CREATE TABLE IF NOT EXISTS book (
            ts          INTEGER NOT NULL,
            asset       TEXT    NOT NULL,
            tf          TEXT    NOT NULL,
            side        TEXT    NOT NULL,
            order_type  TEXT    NOT NULL,
            price_cents INTEGER NOT NULL,
            shares      REAL    NOT NULL,
            total_usd   REAL    NOT NULL
        )
    """)
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_summary_asset_tf_ts ON summary(asset, tf, ts)")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_book_ts ON book(ts)")
    await _db.commit()
    print(f"Database: {DB_PATH}")


async def _close_db() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


async def _db_write_summary(ts_ms: int, asset: str, tf: str,
                             yes_book: TokenBook, no_book: TokenBook,
                             secs: int, in_win: bool,
                             ptb: Optional[float], poly_price: Optional[float],
                             bin_price: Optional[float],
                             book_age: Optional[int], price_age: Optional[int],
                             bin_age: Optional[int],
                             book_ws: str, price_ws: str, bin_ws: str) -> None:
    if _db is None:
        return

    def _ask(b: TokenBook) -> Optional[int]:
        a = b.best_ask
        return round(a * 100) if a is not None else None

    def _bid(b: TokenBook) -> Optional[int]:
        v = b.best_bid
        return round(v * 100) if v is not None else None

    def _mid(b: TokenBook) -> Optional[int]:
        m = b.mid
        return round(m * 100) if m is not None else None

    def _sprd(b: TokenBook) -> Optional[float]:
        bv, av = b.best_bid, b.best_ask
        return round((av - bv) * 100, 1) if bv is not None and av is not None else None

    liq = round(_book_usd_depth(yes_book) + _book_usd_depth(no_book), 2)

    await _db.execute(
        """INSERT INTO summary VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts_ms, asset, tf,
            _ask(yes_book), _mid(yes_book), _bid(yes_book), _sprd(yes_book),
            _ask(no_book),  _mid(no_book),  _bid(no_book),  _sprd(no_book),
            liq,
            round(ptb, 4) if ptb else None,
            round(poly_price, 2) if poly_price else None,
            round(bin_price, 2) if bin_price else None,
            secs,
            1 if in_win else 0,
            book_age, price_age, bin_age,
            1 if book_ws == "ok" else 0,
            1 if price_ws == "ok" else 0,
            1 if bin_ws == "ok" else 0,
        ),
    )
    await _db.commit()


async def _db_write_book(ts_ms: int, asset: str, tf: str,
                          book: TokenBook, side: str) -> None:
    if _db is None:
        return
    rows = []
    for p, s in book.asks.items():
        pc = round(float(p) * 100)
        if 1 <= pc <= 99:
            rows.append((ts_ms, asset, tf, side, "ask", pc, round(s, 2), round(float(p) * s, 2)))
    for p, s in book.bids.items():
        pc = round(float(p) * 100)
        if 1 <= pc <= 99:
            rows.append((ts_ms, asset, tf, side, "bid", pc, round(s, 2), round(float(p) * s, 2)))
    if rows:
        await _db.executemany("INSERT INTO book VALUES (?,?,?,?,?,?,?,?)", rows)
        await _db.commit()

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
                        _binance_price_status = "ok"
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
    last_str   = f"{round(a * 100)}c"          if a is not None else "N/A"
    spread_str = f"{round((a - b) * 100, 1)}c" if a is not None and b is not None else "N/A"

    lines = [f"    [{label}]"]
    for price, size in asks:
        lines.append(f"      {round(price*100):>3}c | {size:>8.2f} sh | ${price*size:>8.2f}")
    lines.append(f"    -- Last: {last_str} | Spread: {spread_str} --")
    for price, size in bids:
        lines.append(f"      {round(price*100):>3}c | {size:>8.2f} sh | ${price*size:>8.2f}")
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

    tf_order = {"5min": 0, "15min": 1}
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

    for mid, t in list(trackers.items()):
        if not _is_current_live_market(t.market, now=now):
            if _remove_tracker(mid, "expired/old/future"):
                removed_count += 1

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

    for key, m in incoming_by_key.items():
        same_key = [
            (mid, t) for mid, t in trackers.items()
            if _current_tracker_key(t.market) == key
        ]

        already_running = any(t.market.market_id == m.market_id for _mid, t in same_key)
        if already_running:
            for mid, t in same_key:
                if t.market.market_id != m.market_id:
                    if _remove_tracker(mid, "replaced by current market"):
                        removed_count += 1
                        replaced_count += 1
            continue

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

    if SHOW_DISCOVERY_SUMMARY:
        current_display = _display_trackers()
        total = len(current_display)
        tf_counts: Dict[str, int] = {"5min": 0, "15min": 0}
        asset_counts: Dict[str, int] = {}
        for t in current_display:
            tf = t.market.timeframe
            if tf in tf_counts:
                tf_counts[tf] += 1
            a = t.market.asset
            asset_counts[a] = asset_counts.get(a, 0) + 1

        ts_str = last_discovery_ts.strftime("%H:%M:%S")
        print(
            f"\n[{ts_str}] Discovery refresh: "
            f"accepted={len(incoming_by_key)} | new={new_count} | "
            f"removed={removed_count} | replaced={replaced_count} | total_trackers={total}"
        )
        print("Accepted by timeframe:")
        for tf in ["5min", "15min"]:
            print(f"  {tf}: {tf_counts[tf]}")
        print("Accepted by asset:")
        for asset in ASSETS:
            print(f"  {asset}: {asset_counts.get(asset, 0)}")
        if _last_broad_skipped:
            print("Broad discovery skipped: FAST_STARTUP active")
        if tf_counts["15min"] == 0:
            print("WARNING: No 15min markets found in current discovery refresh.")
        print()

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
    by_tf: Dict[str, list] = {"5min": [], "15min": []}
    for t in all_trackers:
        m = t.market
        secs_rem = _secs_remaining(m)
        if secs_rem <= 0:
            continue
        win = SIGNAL_WINDOWS.get(m.timeframe, 0)
        if secs_rem <= win:
            continue
        tf = m.timeframe
        if tf not in by_tf:
            continue
        window_in = int(secs_rem - win)
        end_str = m.candle_end.strftime("%H:%M:%S")
        by_tf[tf].append((window_in, int(secs_rem), m.asset, end_str))

    result: Dict[str, List[str]] = {}
    for tf in ["5min", "15min"]:
        entries = sorted(by_tf[tf], key=lambda x: x[0])[:max_per_tf]
        result[tf] = [
            f"  {asset} {tf}: window in {win_in}s | remaining: {rem}s | end: {end}"
            for win_in, rem, asset, end in entries
        ]
    return result


async def scan_printer() -> None:
    global scan_num
    while True:
        await asyncio.sleep(SCAN_INTERVAL)
        scan_num += 1
        try:
            await _scan_printer_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug(f"scan_printer tick error: {exc}", exc_info=True)


async def _scan_printer_tick() -> None:
    now   = datetime.now(timezone.utc)
    ts    = now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
    all_t = _display_trackers()
    for t in all_t:
        _refresh_tracker_current_price(t)
        _refresh_tracker_binance_price(t)
    total = len(all_t)
    conn  = sum(1 for t in all_t if t.status == "ok")
    price_conn = sum(1 for t in all_t if getattr(t, "current_price_status", "") == "ok")
    binance_conn = sum(1 for t in all_t if getattr(t, "binance_price_status", "") == "ok")

    tf_total: Dict[str, int] = {"5min": 0, "15min": 0}
    for t in all_t:
        tf = t.market.timeframe
        if tf in tf_total:
            tf_total[tf] += 1

    all_by_tf: Dict[str, List[tuple]] = {"5min": [], "15min": []}
    in_win_counts: Dict[str, int] = {"5min": 0, "15min": 0}

    for t in all_t:
        in_win, secs = _in_window(t.market)
        tf = t.market.timeframe
        if tf not in all_by_tf:
            continue

        if in_win:
            in_win_counts[tf] += 1

        up_ask = _display_ask(t.yes_book)
        up_mid = _display_pct(t.yes_book)
        up_bid = _display_bid(t.yes_book)
        down_ask = _display_ask(t.no_book)
        down_mid = _display_pct(t.no_book)
        down_bid = _display_bid(t.no_book)
        up_sprd = _display_spread(t.yes_book)
        down_sprd = _display_spread(t.no_book)
        liq  = _display_liq_combined(t.yes_book, t.no_book)
        ptb = _display_price_to_beat(t)
        cur = _display_current_price(t)
        binance = _display_binance_price(t)
        in_win_label = "YES" if in_win else "NO"
        age_ms = _book_age_ms(t)
        age_label = f"{age_ms}ms" if age_ms is not None else "N/A"
        price_age_ms = _price_age_ms(t)
        price_age_label = f"{price_age_ms}ms" if price_age_ms is not None else "N/A"
        binance_age_ms = _binance_age_ms(t)
        binance_age_label = f"{binance_age_ms}ms" if binance_age_ms is not None else "N/A"

        _key    = f"{t.market.asset}_{tf}"
        _now_ts = now.timestamp()
        _ts_ms  = int(now.timestamp() * 1000)

        if _now_ts - _db_last_book.get(_key, 0) >= DB_BOOK_INTERVAL:
            await _db_write_book(_ts_ms, t.market.asset, tf, t.yes_book, "UP")
            await _db_write_book(_ts_ms, t.market.asset, tf, t.no_book,  "DOWN")
            _db_last_book[_key] = _now_ts

        if _now_ts - _db_last_summary.get(_key, 0) >= DB_SUMMARY_INTERVAL:
            await _db_write_summary(
                _ts_ms, t.market.asset, tf,
                t.yes_book, t.no_book,
                secs, in_win,
                getattr(t, "price_to_beat", None),
                getattr(t, "current_price", None),
                getattr(t, "binance_price", None),
                _book_age_ms(t), _price_age_ms(t), _binance_age_ms(t),
                t.status, t.current_price_status, t.binance_price_status,
            )
            _db_last_summary[_key] = _now_ts

        market_line = (
            f"  {t.market.asset} {tf}: "
            f"UP [[ask: {up_ask}]; bid: {up_bid}; mid: {up_mid}] - DOWN [[ask: {down_ask}]; bid: {down_bid}; mid: {down_mid}] | "
            f"up_sprd:{up_sprd} | down_sprd:{down_sprd} | liq:{liq} | "
            f"PTB[{ptb}] | CUR[{cur}] | BIN[{binance}] | remaining: {secs}s | "
            f"in_window: {in_win_label} | "
            f"book_age: {age_label} | price_age: {price_age_label} | bin_age: {binance_age_label} | "
            f"ws: {t.status} | price_ws: {t.current_price_status} | bin_ws: {t.binance_price_status}"
        )
        book_lines = _format_combined_book(t.yes_book, t.no_book)
        all_by_tf[tf].append((market_line, book_lines))

    total_in_win = sum(in_win_counts.values())
    tf_in_win_str = " | ".join(f"{tf}={in_win_counts[tf]}" for tf in ["5min", "15min"])
    tf_total_str  = " | ".join(f"{tf}={tf_total[tf]}"      for tf in ["5min", "15min"])

    if last_discovery_ts is not None:
        elapsed = (now - last_discovery_ts).total_seconds()
        next_in = max(0, DISCOVERY_INTERVAL - elapsed)
        disc_line = (
            f"Last discovery refresh: {last_discovery_ts.strftime('%H:%M:%S')} "
            f"| next refresh in: {int(next_in)}s"
        )
    else:
        disc_line = "Last discovery refresh: pending…"

    print(f"\n[{ts}] SCAN #{scan_num}")
    print(f"Markets scanned: {total}")
    print(f"By timeframe: {tf_total_str}")
    print(f"Markets in window: {total_in_win}")
    print(f"In window by timeframe: {tf_in_win_str}")
    print(f"Trackers connected: {conn}/{total}")
    print(f"Price streams connected: {price_conn}/{total}")
    print(f"Binance streams connected: {binance_conn}/{total}")
    print(disc_line)

    print()
    print("All current Polymarket Up/Down markets:")
    for tf in ["5min", "15min"]:
        print(f"{tf}:")
        entries = all_by_tf[tf]
        if entries:
            for market_line, book_lines in entries:
                print(market_line)
                for bl in book_lines:
                    print(bl)
        else:
            print(f"  No {tf} markets currently tracked.")

    print("-" * 50)


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
    await _init_db()
    try:
        print("Starting scanner...")
        print("Running slug discovery...")
        await run_discovery()
        total_found = len(trackers)
        if _last_broad_skipped:
            print(f"Slug discovery done: valid={total_found}")
            print("Broad discovery skipped: FAST_STARTUP active")
        else:
            print(f"Slug discovery done: valid={total_found}")
        if not trackers:
            log.warning("No matching markets found at startup – will retry every 30s.")
        print("Starting trackers...")
        print("Scanner loop started.")
        await asyncio.gather(
            _run_forever(polymarket_current_price_feed_loop, "poly-price"),
            _run_forever(binance_current_price_feed_loop,    "binance-price"),
            _run_forever(discovery_loop,                     "discovery"),
            _run_forever(scan_printer,                       "scan-printer"),
        )
    finally:
        await _close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if not QUIET_MODE:
            log.info("Stopped.")
