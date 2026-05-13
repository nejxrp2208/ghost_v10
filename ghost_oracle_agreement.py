"""
ghost_oracle_agreement.py
=========================
Phase 5 — Binance/Coinbase Oracle Agreement Filter

Pre-fire gate. Skips trades when the Coinbase 5-min direction (the actual
PM oracle) is going against our bet, OR when the move is too tight for
direction to be locked.

Background
----------
Polymarket UMA oracle resolves UP/DOWN markets using Coinbase prices.
Our trend-snipe strategy fires based on Binance 1h trend prediction.
On TIGHT 5-min markets (move < 0.05%), Binance and Coinbase disagree on
direction ~43% of the time. Those fires are coin-flips, not edge.

Filter logic at fire time (per coin):
  1. Fetch in-progress 5m kline from Binance + Coinbase Exchange
  2. Compute direction-so-far: sign((close - open) / open)
  3. Decision tree:
     - Coinbase API failure       → FAIL_OPEN config (default: allow fire)
     - Coinbase dir = bet side    → FIRE (oracle aligned with bet)
     - Coinbase |move| < TIE thr  → SKIP (tight = oracle could flip)
     - Coinbase dir ≠ bet side    → SKIP (oracle already against us)
  4. Log decision + Binance dir for cross-reference

Per-coin 30s cache keeps API load light: 5 coins x 2 exchanges = 10 calls /
30s ceiling. Below all rate limits.

Public API (used by crypto_ghost_scanner.fire_trade):
  allow_fire(coin, bet_side) -> bool
  check_agreement(coin)      -> dict (telemetry-only; no side effects)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from typing import Optional, Tuple

# ── configuration ────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(SCRIPT_DIR, "agent_reports.db")

ENABLED              = os.getenv("ORACLE_AGREEMENT_GATE", "true").strip().lower() in ("true", "1", "yes")
CACHE_TTL_SEC        = int(os.getenv("ORACLE_CACHE_TTL_SEC", "30"))
TIE_THRESHOLD_PCT    = float(os.getenv("ORACLE_TIE_THRESHOLD_PCT", "0.02"))   # |move| below this = TIE / unstable
FAIL_OPEN            = os.getenv("ORACLE_FAIL_OPEN", "true").strip().lower() in ("true", "1", "yes")
HTTP_TIMEOUT_SEC     = float(os.getenv("ORACLE_HTTP_TIMEOUT_SEC", "3.0"))

BINANCE_SYMS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "BNB": "BNBUSDT",
}
COINBASE_SYMS = {
    "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
    "XRP": "XRP-USD", "BNB": "BNB-USD",
}

# Per-process cache: { coin: (fetch_ts, binance_dict_or_None, coinbase_dict_or_None) }
_cache: dict = {}
_cache_lock = threading.Lock()
_db_lock    = threading.Lock()

log = logging.getLogger("oracle_agreement")
if not log.handlers:
    log.setLevel(logging.INFO)
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s] [oracle] %(message)s",
                                     datefmt="%H:%M:%S"))
    log.addHandler(h)


# ── db schema (lazy init) ────────────────────────────────────────────────
_schema_inited = False

def _init_schema():
    global _schema_inited
    if _schema_inited:
        return
    with _db_lock:
        if _schema_inited:
            return
        try:
            con = sqlite3.connect(DB_PATH, timeout=60.0)
            con.execute("""
                CREATE TABLE IF NOT EXISTS oracle_agreement_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts              TEXT NOT NULL,
                    coin            TEXT NOT NULL,
                    bet_side        TEXT,
                    binance_dir     TEXT,
                    coinbase_dir    TEXT,
                    binance_pct     REAL,
                    coinbase_pct    REAL,
                    decision        TEXT,
                    skip_reason     TEXT
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_oal_ts   ON oracle_agreement_log(ts)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_oal_coin ON oracle_agreement_log(coin)")
            con.commit()
            con.close()
            _schema_inited = True
        except Exception as e:
            log.error(f"schema init failed: {e}")


# ── HTTP helpers ─────────────────────────────────────────────────────────
def _http_get_json(url: str) -> Optional[object]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
            return json.loads(r.read())
    except Exception as e:
        log.debug(f"GET {url} -> {e}")
        return None


def _fetch_binance_5m(coin: str) -> Optional[dict]:
    """Returns {open, close, ts} from the in-progress 5m kline on Binance."""
    sym = BINANCE_SYMS.get(coin)
    if not sym:
        return None
    url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=5m&limit=1"
    d = _http_get_json(url)
    if not isinstance(d, list) or not d:
        return None
    k = d[0]
    # [open_time_ms, open, high, low, close, volume, close_time_ms, ...]
    try:
        return {"open": float(k[1]), "close": float(k[4]), "ts": int(k[0])}
    except (ValueError, IndexError, TypeError):
        return None


def _fetch_coinbase_5m(coin: str) -> Optional[dict]:
    """Returns {open, close, ts} from the in-progress 5m candle on Coinbase Exchange."""
    sym = COINBASE_SYMS.get(coin)
    if not sym:
        return None
    url = f"https://api.exchange.coinbase.com/products/{sym}/candles?granularity=300"
    d = _http_get_json(url)
    if not isinstance(d, list) or not d:
        return None
    # Coinbase format: [time, low, high, open, close, volume]
    # Most recent (in-progress) candle is at index 0
    k = d[0]
    try:
        return {"open": float(k[3]), "close": float(k[4]), "ts": int(k[0])}
    except (ValueError, IndexError, TypeError):
        return None


# ── direction logic ─────────────────────────────────────────────────────
def _move_pct(o: float, c: float) -> Optional[float]:
    if o <= 0:
        return None
    return (c - o) / o * 100.0


def _direction(move_pct: Optional[float]) -> Optional[str]:
    """Returns 'UP' / 'DOWN' / 'TIE' / None (None = couldn't compute)."""
    if move_pct is None:
        return None
    if abs(move_pct) < TIE_THRESHOLD_PCT:
        return "TIE"
    return "UP" if move_pct > 0 else "DOWN"


# ── public API ───────────────────────────────────────────────────────────
def check_agreement(coin: str) -> dict:
    """Read-only telemetry. Returns current direction snapshot (or cached one)."""
    coin = (coin or "").upper()
    now = time.time()
    with _cache_lock:
        cached = _cache.get(coin)
        if cached and now - cached[0] < CACHE_TTL_SEC:
            bn, cb = cached[1], cached[2]
        else:
            bn = None
            cb = None
            cached = None
    if cached is None:
        bn = _fetch_binance_5m(coin)
        cb = _fetch_coinbase_5m(coin)
        with _cache_lock:
            _cache[coin] = (now, bn, cb)

    bn_pct = _move_pct(bn["open"], bn["close"]) if bn else None
    cb_pct = _move_pct(cb["open"], cb["close"]) if cb else None
    bn_dir = _direction(bn_pct)
    cb_dir = _direction(cb_pct)

    return {
        "coin": coin,
        "binance_dir":  bn_dir,
        "coinbase_dir": cb_dir,
        "binance_pct":  round(bn_pct, 4) if bn_pct is not None else None,
        "coinbase_pct": round(cb_pct, 4) if cb_pct is not None else None,
        "binance_ok":   bn is not None,
        "coinbase_ok":  cb is not None,
    }


def _decide(state: dict, bet_side: str) -> Tuple[bool, str, str]:
    """
    Pure decision function. Returns (allow, decision_label, skip_reason).
      allow            : True  → fire is allowed
                         False → block the fire
      decision_label   : 'FIRE' | 'SKIP' | 'FAIL_OPEN_FIRE' | 'FAIL_CLOSED_SKIP'
      skip_reason      : short string for telemetry / dashboard
    """
    bet = (bet_side or "").upper()
    cb_dir = state.get("coinbase_dir")
    cb_pct = state.get("coinbase_pct")

    # Coinbase API failure
    if not state.get("coinbase_ok"):
        if FAIL_OPEN:
            return True, "FAIL_OPEN_FIRE", "coinbase_api_fail_fail_open"
        return False, "FAIL_CLOSED_SKIP", "coinbase_api_fail_fail_closed"

    # TIE / too tight to call
    if cb_dir == "TIE" or (cb_pct is not None and abs(cb_pct) < TIE_THRESHOLD_PCT):
        return False, "SKIP", f"coinbase_tight_{cb_pct:.4f}pct"

    # Coinbase already against our bet
    if cb_dir != bet:
        return False, "SKIP", f"coinbase_against_bet_cb={cb_dir}_bet={bet}"

    # Coinbase aligned with our bet → fire
    return True, "FIRE", f"coinbase_agrees_cb={cb_dir}_bet={bet}_move={cb_pct:.4f}pct"


def allow_fire(coin: str, bet_side: str) -> bool:
    """
    Pre-fire gate. Returns True if scanner should proceed with the order,
    False if the fire should be skipped.

    Always logs the decision to oracle_agreement_log (telemetry).
    Honours ORACLE_AGREEMENT_GATE flag — if disabled, always returns True.
    """
    if not ENABLED:
        return True

    coin = (coin or "").upper()
    bet  = (bet_side or "").upper()
    state = check_agreement(coin)
    allow, label, reason = _decide(state, bet)

    # Telemetry
    _init_schema()
    try:
        with _db_lock:
            con = sqlite3.connect(DB_PATH, timeout=60.0)
            con.execute(
                """INSERT INTO oracle_agreement_log
                   (ts, coin, bet_side, binance_dir, coinbase_dir,
                    binance_pct, coinbase_pct, decision, skip_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.utcnow().isoformat(timespec="seconds"),
                    coin, bet,
                    state.get("binance_dir"),
                    state.get("coinbase_dir"),
                    state.get("binance_pct"),
                    state.get("coinbase_pct"),
                    label, reason,
                ),
            )
            con.commit()
            con.close()
    except Exception as e:
        log.warning(f"telemetry log failed: {e}")

    if not allow:
        log.info(
            f"SKIP {coin} bet={bet} cb={state.get('coinbase_dir')} "
            f"({state.get('coinbase_pct')}%) bn={state.get('binance_dir')} "
            f"({state.get('binance_pct')}%)  reason={reason}"
        )
    return allow


# ── module-level smoke test ──────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    print(f"[oracle] ENABLED={ENABLED} CACHE_TTL_SEC={CACHE_TTL_SEC} "
          f"TIE_THRESHOLD_PCT={TIE_THRESHOLD_PCT} FAIL_OPEN={FAIL_OPEN}")
    print()
    print("Live data smoke test:")
    print(f"  {'coin':<5} {'bn_dir':<6} {'bn_pct':<10} "
          f"{'cb_dir':<6} {'cb_pct':<10} {'agree?':<7}")
    print("  " + "-" * 60)
    for c in ["BTC", "ETH", "SOL", "XRP", "BNB"]:
        s = check_agreement(c)
        agree = (s["binance_dir"] == s["coinbase_dir"]
                 and s["binance_dir"] is not None
                 and s["binance_dir"] != "TIE")
        print(f"  {c:<5} "
              f"{str(s['binance_dir']):<6} "
              f"{(str(s['binance_pct']) + '%'):<10} "
              f"{str(s['coinbase_dir']):<6} "
              f"{(str(s['coinbase_pct']) + '%'):<10} "
              f"{('YES' if agree else 'no'):<7}")
    print()
    print("Decision tree dry-run for BTC bet=UP:")
    s = check_agreement("BTC")
    allow, label, reason = _decide(s, "UP")
    print(f"  allow={allow}  label={label}  reason={reason}")
    sys.exit(0)
