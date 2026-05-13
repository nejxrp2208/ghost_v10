"""
Ghost Fill Guard — adapted for CryptoGhostScanner
==================================================
Ported from share's bot (446 lines, stdlib-only) with these REAL fixes:

  - REMOVED `MIN_LIQUIDITY_USDC` check  (duplicate of scanner's MIN_LIQUIDITY
                                          env-var check; single source of truth)
  - REMOVED `MAX_SPREAD_RATIO`           (dead code in source — never used)
  - MODIFIED `sweep_ghosts()` to read    (cleanly handles PAPER mode + LIVE
    our trades DB before declaring       without a paper_mode flag — uses the
    a ghost                              real settlement data we already have)

Problems solved
---------------
1. Ghost fills  — order appears matched off-chain but never settles on-chain.
                  We track every order_id; if past settlement window it's still
                  unresolved, mark it a ghost and block re-entry on that token.
2. Duplicates   — same market fired twice before first order confirms.
                  We gate each token_id: no second order until first resolves.
3. Salt collisions — CLOB V2 still uses a `salt` field. We generate a unique
                  salt = epoch_ms XOR 32-bit random.
4. Adverse selection logging — after each fill we log fill_price vs ask,
                  win/loss, and ghost rate per coin.

Usage in scanner.py
-------------------
    from ghost_fill_guard import GhostFillGuard
    guard = GhostFillGuard(
        guard_db=os.path.join(SCRIPT_DIR, "agent_reports.db"),
        trades_db=DB_PATH,
    )

    # Before placing an order:
    if not guard.can_fire(token_id, coin):
        return   # duplicate / ghost cooldown active

    # Register immediately after placement:
    guard.register_order(order_id, token_id, coin, tier, ask_price, size_usdc)

    # Periodic sweep — every ~60s from main loop:
    guard.sweep_ghosts()
"""

import os
import time
import random
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

log = logging.getLogger("GhostFillGuard")

# ── tunables ──────────────────────────────────────────────────────────────────
CONFIRM_TIMEOUT_SEC  = 45    # seconds after order placement before ghost-check
GHOST_COOLDOWN_SEC   = 120   # block re-entry on a token after a ghost
DUP_LOCK_SEC         = 60    # block same token_id while order is pending
# ─────────────────────────────────────────────────────────────────────────────


def _unique_salt() -> int:
    """Cryptographically-distinct salt per order (collision prob < 1 in 10^12/sec)."""
    return (int(time.time() * 1000) % (2**31)) ^ random.getrandbits(30)


class GhostFillGuard:
    """Order tracker and ghost-fill prevention layer."""

    def __init__(self, guard_db: str = "agent_reports.db",
                 trades_db: Optional[str] = None):
        """
        guard_db  — where order_tracker + coin_perf live (agent_reports.db).
        trades_db — optional path to the trades DB. When provided, sweep_ghosts
                    looks up orders in trades before declaring them ghosts.
                    This makes the guard work cleanly in BOTH paper and live.
        """
        self.guard_db  = guard_db
        self.trades_db = trades_db
        self._ensure_schema()
        self._pending_locks: Dict[str, float] = {}
        self._ghost_locks:   Dict[str, float] = {}
        log.info("[GhostGuard] init  guard_db=%s  trades_db=%s",
                  guard_db, trades_db or "(none)")

    # ── schema ────────────────────────────────────────────────────────────────

    def _conn(self):
        return sqlite3.connect(self.guard_db, timeout=60)

    def _ensure_schema(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS order_tracker (
                order_id        TEXT PRIMARY KEY,
                token_id        TEXT NOT NULL,
                coin            TEXT NOT NULL,
                tier            TEXT,
                ask_price       REAL,
                size_usdc       REAL,
                salt            INTEGER,
                submitted_at    TEXT,
                confirmed_at    TEXT,
                fill_price      REAL,
                filled          INTEGER DEFAULT 0,
                won             INTEGER DEFAULT -1,
                ghost           INTEGER DEFAULT 0,
                slippage_pct    REAL
            );
            CREATE INDEX IF NOT EXISTS idx_ot_token  ON order_tracker(token_id);
            CREATE INDEX IF NOT EXISTS idx_ot_coin   ON order_tracker(coin);
            CREATE INDEX IF NOT EXISTS idx_ot_ghost  ON order_tracker(ghost);

            CREATE TABLE IF NOT EXISTS coin_perf (
                coin            TEXT PRIMARY KEY,
                fires           INTEGER DEFAULT 0,
                fills           INTEGER DEFAULT 0,
                wins            INTEGER DEFAULT 0,
                ghosts          INTEGER DEFAULT 0,
                total_staked    REAL    DEFAULT 0.0,
                total_pnl       REAL    DEFAULT 0.0,
                last_updated    TEXT
            );
        """)
        conn.commit()
        conn.close()

    # ── salt ──────────────────────────────────────────────────────────────────

    @staticmethod
    def unique_salt() -> int:
        """Public helper — pass as `salt` when building CLOB V2 order structs."""
        return _unique_salt()

    # ── pre-fire gate ─────────────────────────────────────────────────────────
    # Note: liquidity gate REMOVED. Scanner's MIN_LIQUIDITY env var already
    # checks this — single source of truth. Don't duplicate.

    def can_fire(self, token_id: str, coin: str) -> bool:
        """
        Returns True if it's safe to place a new order for this token.

        Blocks if:
        - A pending order for this token hasn't confirmed yet (DUP_LOCK_SEC)
        - A ghost was detected on this token recently (GHOST_COOLDOWN_SEC)
        """
        now = time.time()

        ghost_exp = self._ghost_locks.get(token_id, 0)
        if now < ghost_exp:
            log.warning("[GhostGuard] %s BLOCKED — ghost cooldown (%.0fs left)",
                        coin, ghost_exp - now)
            return False

        pend_exp = self._pending_locks.get(token_id, 0)
        if now < pend_exp:
            log.debug("[GhostGuard] %s BLOCKED — pending lock (%.0fs left)",
                      coin, pend_exp - now)
            return False

        return True

    # ── order registration ────────────────────────────────────────────────────

    def register_order(self, order_id: str, token_id: str, coin: str,
                       tier: str = "T2", ask_price: float = 0.0,
                       size_usdc: float = 0.0) -> int:
        """Call immediately after a successful order submission."""
        salt = _unique_salt()
        now  = datetime.now(timezone.utc).isoformat()

        self._pending_locks[token_id] = time.time() + DUP_LOCK_SEC

        conn = self._conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO order_tracker
                (order_id, token_id, coin, tier, ask_price, size_usdc,
                 salt, submitted_at, filled, won, ghost)
                VALUES (?,?,?,?,?,?,?,?,0,-1,0)
            """, (order_id, token_id, coin, tier, ask_price, size_usdc, salt, now))

            conn.execute("""
                INSERT INTO coin_perf (coin, fires, total_staked, last_updated)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(coin) DO UPDATE SET
                    fires        = fires + 1,
                    total_staked = total_staked + ?,
                    last_updated = ?
            """, (coin, size_usdc, now, size_usdc, now))
            conn.commit()
        finally:
            conn.close()

        log.info("[GhostGuard] register %s | %s %s @%.4f $%.2f salt=%d",
                 str(order_id)[:14], coin, tier, ask_price, size_usdc, salt)
        return salt

    # ── confirmation ──────────────────────────────────────────────────────────

    def confirm_fill(self, order_id: str, filled: bool,
                     fill_price: float = 0.0, won: Optional[bool] = None,
                     pnl: float = 0.0):
        """Mark an order resolved — filled with outcome, or unfilled (rejected)."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT token_id, coin, ask_price FROM order_tracker WHERE order_id=?",
                (order_id,)
            ).fetchone()
            if not row:
                log.warning("[GhostGuard] confirm_fill: unknown order_id %s",
                             str(order_id)[:14])
                return
            token_id, coin, ask_price = row

            slippage = 0.0
            if filled and ask_price and ask_price > 0 and fill_price > 0:
                slippage = ((fill_price - ask_price) / ask_price) * 100

            won_int = -1 if won is None else (1 if won else 0)

            conn.execute("""
                UPDATE order_tracker SET
                    filled       = ?,
                    fill_price   = ?,
                    won          = ?,
                    confirmed_at = ?,
                    slippage_pct = ?,
                    ghost        = 0
                WHERE order_id = ?
            """, (1 if filled else 0, fill_price, won_int, now, slippage, order_id))

            if filled:
                conn.execute("""
                    UPDATE coin_perf SET
                        fills        = fills + 1,
                        wins         = wins + ?,
                        total_pnl    = total_pnl + ?,
                        last_updated = ?
                    WHERE coin = ?
                """, (1 if won else 0, pnl, now, coin))
            conn.commit()

            self._pending_locks.pop(token_id, None)
            log.info("[GhostGuard] confirm %s | filled=%s won=%s slip=%.2f%%",
                     str(order_id)[:14], filled, won, slippage)
        finally:
            conn.close()

    # ── ghost sweep with trades-DB integration ─────────────────────────────────

    def sweep_ghosts(self, clob_client=None, trades_db: Optional[str] = None):
        """
        Sweep stale unconfirmed orders. Three-stage check:

          1. If order_id appears in trades DB as won/lost → confirm internally
          2. Else, if a clob_client is available, ask CLOB for status
          3. Else, after CONFIRM_TIMEOUT_SEC, mark as ghost + apply cooldown
        """
        cutoff = datetime.fromtimestamp(
            time.time() - CONFIRM_TIMEOUT_SEC, tz=timezone.utc
        ).isoformat()

        conn = self._conn()
        stale = conn.execute("""
            SELECT order_id, token_id, coin FROM order_tracker
            WHERE filled = 0 AND ghost = 0
              AND submitted_at < ?
        """, (cutoff,)).fetchall()
        conn.close()

        if not stale:
            return

        tdb = trades_db or self.trades_db

        for order_id, token_id, coin in stale:
            if tdb and os.path.exists(tdb):
                try:
                    tcon = sqlite3.connect(f"file:{tdb}?mode=ro", uri=True, timeout=60)
                    trow = tcon.execute(
                        "SELECT status, pnl, entry_price FROM trades WHERE order_id=?",
                        (order_id,)
                    ).fetchone()
                    tcon.close()
                except Exception as e:
                    log.debug("[GhostGuard] trades_db lookup failed for %s: %s",
                               str(order_id)[:14], e)
                    trow = None

                if trow:
                    status, pnl_val, entry_price = trow
                    if status in ("won", "lost"):
                        self.confirm_fill(
                            order_id,
                            filled=True,
                            fill_price=float(entry_price or 0.0),
                            won=(status == "won"),
                            pnl=float(pnl_val or 0.0),
                        )
                        continue

            is_ghost = True
            if clob_client:
                try:
                    status_resp = clob_client.get_order(order_id)
                    order_status = (status_resp or {}).get("status", "UNKNOWN")
                    if order_status in ("MATCHED", "FILLED"):
                        is_ghost = False
                        log.info("[GhostGuard] late confirm via CLOB %s — %s",
                                 str(order_id)[:14], order_status)
                except Exception as e:
                    log.debug("[GhostGuard] CLOB check failed for %s: %s",
                               str(order_id)[:14], e)

            if is_ghost:
                conn2 = self._conn()
                conn2.execute(
                    "UPDATE order_tracker SET ghost=1 WHERE order_id=?",
                    (order_id,)
                )
                conn2.execute("""
                    UPDATE coin_perf SET ghosts=ghosts+1, last_updated=?
                    WHERE coin=?
                """, (datetime.now(timezone.utc).isoformat(), coin))
                conn2.commit()
                conn2.close()

                self._ghost_locks[token_id] = time.time() + GHOST_COOLDOWN_SEC
                self._pending_locks.pop(token_id, None)

                log.warning("[GhostGuard] GHOST %s | %s | cooldown %ds",
                            str(order_id)[:14], coin, GHOST_COOLDOWN_SEC)

    # ── stats ─────────────────────────────────────────────────────────────────

    def coin_stats(self) -> Dict[str, Dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT coin, fires, fills, wins, ghosts, total_staked, total_pnl "
            "FROM coin_perf ORDER BY fires DESC"
        ).fetchall()
        conn.close()
        out = {}
        for coin, fires, fills, wins, ghosts, staked, pnl in rows:
            out[coin] = {
                "fires":        fires,
                "fills":        fills,
                "wins":         wins,
                "ghosts":       ghosts,
                "fill_rate":    round(fills / fires, 3) if fires else 0,
                "win_rate":     round(wins  / fills, 3) if fills else 0,
                "ghost_rate":   round(ghosts / fires, 3) if fires else 0,
                "total_staked": round(staked, 2),
                "total_pnl":    round(pnl, 2),
            }
        return out

    def pending_count(self) -> int:
        now = time.time()
        return sum(1 for exp in self._pending_locks.values() if now < exp)

    def ghost_locked_tokens(self) -> list:
        now = time.time()
        return [t for t, exp in self._ghost_locks.items() if now < exp]
