"""
ZUGU v3.5 — Account-survival breakers.

Three independent halts, identical pattern to GHOST PREDATOR's halt_reason():

  - FL_DAILY_LOSS_LIMIT    Halt UTC day if today's realized PnL <= -this
  - FL_ROLLING_24H_LIMIT   Halt if trailing 24h realized PnL <= -this
  - FL_MAX_LOSS_STREAK     Halt if last N resolved trades were all losses

All limits default to 0 (DISABLED). The bot behaves IDENTICALLY to before until
operator explicitly enables a limit in .env.

Read-only on the trades table. No writes, no side effects. Never raises.

Note on ts convention: scanner.py writes ts via `datetime.now().strftime(
"%Y-%m-%d %H:%M:%S")` (local time, no timezone). On the VPS local=UTC, but we
deliberately use the SAME convention here so substr/string comparisons match.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional


DAILY_LOSS_LIMIT  = float(os.getenv("FL_DAILY_LOSS_LIMIT",  "0"))
ROLLING_24H_LIMIT = float(os.getenv("FL_ROLLING_24H_LIMIT", "0"))
MAX_LOSS_STREAK   = int(  os.getenv("FL_MAX_LOSS_STREAK",   "0"))


def halt_reason(db_path: str) -> Optional[str]:
    """Returns a human-readable halt string if any limit is tripped, else None.

    Safe to call every fire cycle — uses read-only SQLite connection and
    bounded queries (LIMIT MAX_LOSS_STREAK at most)."""
    # Fast exit when all limits are disabled.
    if DAILY_LOSS_LIMIT == 0 and ROLLING_24H_LIMIT == 0 and MAX_LOSS_STREAK == 0:
        return None

    try:
        # read-only URI prevents accidental writes from a guardrail bug
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
    except Exception:
        return None

    try:
        # ── Daily limit (UTC midnight reset, matches scanner ts format) ──────
        if DAILY_LOSS_LIMIT > 0:
            today = datetime.now().strftime("%Y-%m-%d")
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE status IN ('won','lost') AND substr(ts,1,10) = ?",
                (today,),
            ).fetchone()
            day_pnl = float(row[0] or 0)
            if day_pnl <= -DAILY_LOSS_LIMIT:
                return (f"DAILY LOSS LIMIT "
                        f"(today ${day_pnl:.2f} <= -${DAILY_LOSS_LIMIT:.2f})")

        # ── Rolling 24h limit (true rolling window, not UTC-day) ─────────────
        if ROLLING_24H_LIMIT > 0:
            cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE status IN ('won','lost') AND ts >= ?",
                (cutoff,),
            ).fetchone()
            rolling_pnl = float(row[0] or 0)
            if rolling_pnl <= -ROLLING_24H_LIMIT:
                return (f"ROLLING 24H LIMIT "
                        f"(${rolling_pnl:.2f} <= -${ROLLING_24H_LIMIT:.2f})")

        # ── Loss streak (last N resolved must NOT all be losses) ─────────────
        if MAX_LOSS_STREAK > 0:
            rows = conn.execute(
                "SELECT status FROM trades "
                "WHERE status IN ('won','lost') "
                "ORDER BY id DESC LIMIT ?",
                (MAX_LOSS_STREAK,),
            ).fetchall()
            if len(rows) >= MAX_LOSS_STREAK and all(r[0] == "lost" for r in rows):
                return (f"LOSS STREAK "
                        f"({MAX_LOSS_STREAK} consecutive losses — edge may be broken)")

    except Exception:
        # never raise — guardrails must be best-effort
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return None
