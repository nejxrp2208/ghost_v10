"""
ZUGU v3.5 — state.json writer.

Pure observability. Writes a snapshot of runtime state to state.json every
1 second so the dashboard (and any external tooling) can read a single
atomically-replaced file instead of querying the DB every refresh.

Design
------
Decoupled by callable: caller passes a `build_state` function. This module
knows NOTHING about scanner internals — it just calls the function, adds
timestamps, and atomically writes the JSON. That keeps the writer reusable
and the scanner free to evolve its state shape.

Atomicity: writes to .tmp then os.replace() — readers never see partial JSON.

Failure mode: every iteration wrapped in try/except. A single bad
build_state() call won't kill the supervisor; it just skips that tick.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict


SD         = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SD, "state.json")

POLL_SECS = float(os.getenv("FL_STATE_POLL_SECS", "1.0"))


def _atomic_write(payload: Dict[str, Any]) -> None:
    """Write JSON atomically: tmp → os.replace."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        # default=str so datetime / Decimal / unknown types serialize gracefully
        json.dump(payload, f, default=str, separators=(",", ":"))
    os.replace(tmp, STATE_FILE)


async def state_writer_loop(build_state: Callable[[], Dict[str, Any]]) -> None:
    """Background task: every POLL_SECS, call build_state() and write state.json.

    build_state() must return a JSON-serializable dict. ts/ts_iso are added
    here (caller does not need to provide them)."""
    while True:
        try:
            payload = build_state() or {}
            payload["ts"]     = time.time()
            payload["ts_iso"] = datetime.now(timezone.utc).isoformat()
            _atomic_write(payload)
        except Exception:
            # silent — never let observability break trading
            pass
        await asyncio.sleep(POLL_SECS)
