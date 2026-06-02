"""
ZUGU v3.5 — Live trading layer.

Adds real-money order placement on Polymarket CLOB. Active ONLY when
PAPER_TRADE=false in .env. Otherwise this module is a no-op and the bot
behaves identically to paper-only v3.5.

What it does
------------
  - place_gtc(token_id, ask, shares)   place a GTC limit order, return order_id
  - fill_watch_loop()                  background task: cancel un-filled
                                       orders after FL_FILL_TIMEOUT_SEC
  - prewarm_loop()                     background task (live only): pings
                                       the CLOB every few seconds during the
                                       snipe window to keep TLS hot
                                       (~25ms saved per fire vs cold handshake)

What it does NOT do
-------------------
  - Pre-sign orders. ZUGU's ask price comes from the live order book and
    varies per fire, so a pre-signed order would go stale instantly. Pre-warm
    alone gives us most of the latency win (~25ms) without that complexity.

Auth model
----------
Uses py_clob_client_v2 with signature_type=3 (POLY_1271 deposit wallet flow)
— same setup validated working in ghost_predator. Credentials read from .env:
  PRIVATE_KEY                  — your EOA private key (signer)
  POLYMARKET_PROXY_ADDRESS     — proxy wallet that actually holds USDC
  CLOB_API_KEY / SECRET / PASSPHRASE  — API creds (one per wallet)

Safety
------
Lazy client init (only constructed on first live use) — paper mode never
loads py_clob_client at all, so missing dependency does NOT break paper.
Every call wrapped in try/except so a CLOB failure logs the error and the
bot continues; it never crashes the supervisor.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple


log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
PAPER_TRADE      = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true", "1", "yes")
FILL_TIMEOUT_SEC = int(  os.getenv("FL_FILL_TIMEOUT_SEC",       "30"))
PREWARM_NEAR_SEC = int(  os.getenv("FL_PREWARM_NEAR_CLOSE_SEC", "60"))
PREWARM_INTERVAL = int(  os.getenv("FL_PREWARM_INTERVAL_SEC",   "5"))

CLOB_HOST = "https://clob.polymarket.com"

_SD          = os.path.dirname(os.path.abspath(__file__))
_DB_PATH     = os.path.join(_SD, "crypto_ghost_PAPER.db" if PAPER_TRADE else "crypto_ghost.db")

# Lazy CLOB client (only built on first real fire — avoids importing py_clob
# in paper mode where it's not strictly required)
_client = None


def is_live() -> bool:
    """Single source of truth — used by scanner.py to branch fire path."""
    return not PAPER_TRADE


def _client_singleton():
    """Lazy-init the CLOB client. Same signature_type/auth pattern that works
    in ghost_predator (verified 2026-05-28)."""
    global _client
    if _client is not None:
        return _client
    from py_clob_client_v2.client    import ClobClient
    from py_clob_client_v2.constants import POLYGON
    from py_clob_client_v2.clob_types import ApiCreds
    creds = ApiCreds(
        api_key        = os.getenv("CLOB_API_KEY", "").strip(),
        api_secret     = os.getenv("CLOB_SECRET", "").strip(),
        api_passphrase = os.getenv("CLOB_PASSPHRASE", "").strip(),
    )
    _client = ClobClient(
        CLOB_HOST,
        key            = os.getenv("PRIVATE_KEY", "").strip(),
        chain_id       = POLYGON,
        creds          = creds,
        signature_type = 3,
        funder         = os.getenv("POLYMARKET_PROXY_ADDRESS", "").strip(),
    )
    return _client


# ── Order placement (sync helper run in to_thread) ──────────────────────────

def _place_gtc_sync(token_id: str, ask: float, shares: float) -> Tuple[bool, Optional[str], str]:
    """Place a GTC limit BUY order. Returns (placed_ok, order_id, error_msg)."""
    try:
        from py_clob_client_v2.clob_types         import OrderArgs, OrderType
        from py_clob_client_v2.order_builder.constants import BUY
        cl = _client_singleton()
        args  = OrderArgs(token_id=token_id, price=float(ask), size=float(shares), side=BUY)
        order = cl.create_order(args)
        resp  = cl.post_order(order, OrderType.GTC)
        oid   = (resp or {}).get("orderID") or (resp or {}).get("orderId")
        if oid:
            return True, str(oid), ""
        return False, None, f"no orderID in response: {resp}"
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


async def place_gtc(token_id: str, ask: float, shares: float) -> Tuple[bool, Optional[str], str]:
    """Async wrapper — runs the blocking sign+POST in a worker thread so the
    event loop is free for the next snipe decision."""
    return await asyncio.to_thread(_place_gtc_sync, token_id, ask, shares)


# ── Fill watch ──────────────────────────────────────────────────────────────

def _cancel_sync(order_id: str) -> bool:
    try:
        cl = _client_singleton()
        cl.cancel(order_id)
        return True
    except Exception as exc:
        log.debug(f"cancel {order_id[:12]}… failed: {exc}")
        return False


async def fill_watch_loop() -> None:
    """Periodically cancel orders that haven't filled within FILL_TIMEOUT_SEC.

    Looks for trades with status='open' and an order_id, where the ts is older
    than the timeout. Marks them as 'cancelled' so they don't sit forever.
    Paper trades have no order_id so they are untouched."""
    if not is_live():
        return
    while True:
        try:
            cutoff = (datetime.now() - timedelta(seconds=FILL_TIMEOUT_SEC)).strftime("%Y-%m-%d %H:%M:%S")
            conn = sqlite3.connect(_DB_PATH, timeout=5)
            rows = conn.execute(
                "SELECT id, order_id, ts FROM trades "
                "WHERE status='open' AND order_id IS NOT NULL AND order_id != '' "
                "AND ts < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                trade_id, order_id, ts = row
                ok = await asyncio.to_thread(_cancel_sync, str(order_id))
                conn.execute(
                    "UPDATE trades SET status='cancelled', closed_at=? WHERE id=?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(trade_id)),
                )
                log.info(f"fill-watch: cancelled order #{trade_id} ({order_id[:14]}…) — {'ok' if ok else 'remote err (db marked anyway)'}")
            conn.commit()
            conn.close()
        except Exception as exc:
            log.debug(f"fill_watch_loop tick err: {exc}")
        await asyncio.sleep(10)


# ── Pre-warm TLS connection (live only) ─────────────────────────────────────

async def prewarm_loop(get_market_close_times) -> None:
    """Keep CLOB TLS connection hot when any market is near close.

    get_market_close_times: callable returning iterable of (epoch_seconds_until_close)
    for currently tracked markets. We ping the CLOB only when at least one
    market is within PREWARM_NEAR_SEC of close — no point holding a connection
    open 24/7.

    Saves ~25ms cold-TLS handshake on the actual fire — meaningful for ZUGU
    where fire window is only 5-15s."""
    if not is_live():
        return
    log.info(f"prewarm: live mode active — ping CLOB every {PREWARM_INTERVAL}s when near close")
    while True:
        try:
            near = False
            try:
                for secs_left in get_market_close_times():
                    if 0 < secs_left <= PREWARM_NEAR_SEC:
                        near = True
                        break
            except Exception:
                pass
            if near:
                # cheap GET to keep TLS pool warm
                await asyncio.to_thread(_ping_clob)
        except Exception as exc:
            log.debug(f"prewarm tick err: {exc}")
        await asyncio.sleep(PREWARM_INTERVAL)


def _ping_clob() -> None:
    try:
        cl = _client_singleton()
        # get_server_time is a cheap GET that exercises the same TLS pool
        # used by post_order, so the next fire reuses a warm connection.
        if hasattr(cl, "get_server_time"):
            cl.get_server_time()
        elif hasattr(cl, "get_address"):
            cl.get_address()
    except Exception:
        pass
