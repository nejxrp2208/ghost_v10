"""SQLite persistence layer — all DB access lives here."""

import sqlite3
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS markets (
            id              TEXT PRIMARY KEY,
            question        TEXT,
            category        TEXT,
            tags            TEXT,
            token_id        TEXT,
            outcome         TEXT,
            volume_24hr     REAL DEFAULT 0,
            liquidity       REAL DEFAULT 0,
            end_date        TEXT,
            best_bid        REAL,
            best_ask        REAL,
            spread          REAL,
            midpoint        REAL,
            liquidity_score REAL,
            quality_score   REAL,
            bid_depth       REAL,
            ask_depth       REAL,
            total_depth     REAL,
            is_targeted     INTEGER DEFAULT 0,
            scanned_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id       TEXT,
            token_id        TEXT,
            question        TEXT,
            side            TEXT,
            entry_price     REAL,
            size            REAL,
            current_price   REAL,
            unrealized_pnl  REAL DEFAULT 0,
            opened_at       TEXT,
            status          TEXT DEFAULT 'open'
        );

        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id       TEXT,
            token_id        TEXT,
            question        TEXT,
            side            TEXT,
            entry_price     REAL,
            exit_price      REAL,
            size            REAL,
            realized_pnl    REAL,
            opened_at       TEXT,
            closed_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id   TEXT,
            token_id    TEXT,
            question    TEXT,
            signal      TEXT,
            price       REAL,
            confidence  REAL,
            reason      TEXT,
            created_at  TEXT,
            acted_on    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS rejected_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id   TEXT,
            question    TEXT,
            reason      TEXT,
            spread      REAL,
            liquidity   REAL,
            volume_24hr REAL,
            created_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS bot_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            level      TEXT,
            message    TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS account (
            id   INTEGER PRIMARY KEY DEFAULT 1,
            cash REAL
        );

        CREATE TABLE IF NOT EXISTS edgefinder_signals (
            id                  TEXT PRIMARY KEY,
            question            TEXT,
            direction           TEXT DEFAULT 'NO',
            yes_price           REAL,
            signal_type         TEXT,
            pattern             TEXT,
            market_url          TEXT,
            status              TEXT,
            detected_at         TEXT,
            resolved_at         TEXT,
            free_signal_of_day  INTEGER DEFAULT 0,
            fetched_at          TEXT,
            our_paper_action    TEXT
        );

        CREATE TABLE IF NOT EXISTS edgefinder_stats_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at      TEXT,
            day              INTEGER,
            overall_win_rate REAL,
            overall_wins     INTEGER,
            overall_losses   INTEGER,
            overall_pending  INTEGER,
            ml_win_rate      REAL,
            crash_win_rate   REAL,
            combined_win_rate REAL,
            raw_json         TEXT
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id   TEXT,
            token_id    TEXT,
            question    TEXT,
            outcome     TEXT,
            yes_price   REAL,
            no_price    REAL,
            spread      REAL,
            volume_24hr REAL,
            liquidity   REAL,
            bid_depth   REAL,
            ask_depth   REAL,
            end_date    TEXT,
            captured_at TEXT
        );

        CREATE TABLE IF NOT EXISTS our_signals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id     TEXT,
            token_id      TEXT,
            question      TEXT,
            direction     TEXT DEFAULT 'NO',
            yes_price     REAL,
            signal_kind   TEXT,
            pattern       TEXT,
            features_json TEXT,
            confidence    REAL,
            status        TEXT DEFAULT 'pending',
            detected_at   TEXT,
            resolved_at   TEXT,
            agreement     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_ef_signals_status   ON edgefinder_signals(status);
        CREATE INDEX IF NOT EXISTS idx_ef_signals_detected ON edgefinder_signals(detected_at);
        CREATE INDEX IF NOT EXISTS idx_snaps_token         ON market_snapshots(token_id, captured_at);
        CREATE INDEX IF NOT EXISTS idx_snaps_captured      ON market_snapshots(captured_at);
        """)
        conn.execute(
            "INSERT OR IGNORE INTO account (id, cash) VALUES (1, ?)",
            (config.STARTING_BALANCE,),
        )
        conn.commit()


# ── Logging ───────────────────────────────────────────────────────────────────

def log_message(level: str, message: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO bot_logs (level, message, created_at) VALUES (?,?,?)",
            (level, message, _now()),
        )
        conn.commit()


def get_logs(limit: int = 200) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_logs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Markets ───────────────────────────────────────────────────────────────────

def upsert_market(market: Dict[str, Any]) -> None:
    key = f"{market['market_id']}_{market['token_id']}"
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO markets
              (id, question, category, tags, token_id, outcome,
               volume_24hr, liquidity, end_date,
               best_bid, best_ask, spread, midpoint,
               liquidity_score, quality_score,
               bid_depth, ask_depth, total_depth,
               is_targeted, scanned_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key,
                market.get("question", ""),
                market.get("category", ""),
                json.dumps(market.get("tags", [])),
                market.get("token_id", ""),
                market.get("outcome", ""),
                market.get("volume_24hr", 0),
                market.get("liquidity", 0),
                market.get("end_date", ""),
                market.get("best_bid"),
                market.get("best_ask"),
                market.get("spread"),
                market.get("midpoint"),
                market.get("liquidity_score"),
                market.get("quality_score"),
                market.get("top3_bid_depth"),
                market.get("top3_ask_depth"),
                market.get("total_depth"),
                1 if market.get("is_targeted") else 0,
                _now(),
            ),
        )
        conn.commit()


def get_scanned_markets(limit: int = 200) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM markets ORDER BY quality_score DESC, volume_24hr DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def log_rejected(
    market_id: str,
    question: str,
    reason: str,
    spread: float,
    liquidity: float,
    volume_24hr: float,
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO rejected_signals
              (market_id, question, reason, spread, liquidity, volume_24hr, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (market_id, question, reason, spread, liquidity, volume_24hr, _now()),
        )
        conn.commit()


def get_rejected(limit: int = 200) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM rejected_signals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Signals ───────────────────────────────────────────────────────────────────

def save_signal(signal: Dict[str, Any]) -> int:
    """
    Insert signal and return the new row ID.
    Writes v2 columns (yes_price_at_emit, signal_age_at_emit_seconds, fee_adjusted_ev,
    edgefinder_signal_id) in the same INSERT when migration 001 is present.
    Falls back gracefully to base columns only if v2 columns don't exist yet.
    """
    base_cols = (
        signal["market_id"], signal["token_id"], signal["question"],
        signal["signal"], signal["price"], signal["confidence"],
        signal["reason"], signal.get("created_at", _now()),
    )
    with _conn() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO signals
                  (market_id, token_id, question, signal, price, confidence, reason, created_at,
                   yes_price_at_emit, signal_age_at_emit_seconds, fee_adjusted_ev,
                   edgefinder_signal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                base_cols + (
                    signal.get("yes_price_at_emit"),
                    signal.get("signal_age_at_emit_seconds"),
                    signal.get("fee_adjusted_ev"),
                    signal.get("edgefinder_signal_id"),
                ),
            )
        except Exception:
            # v2 columns not present yet (migration 001 pending) — fall back to base schema
            cur = conn.execute(
                """
                INSERT INTO signals
                  (market_id, token_id, question, signal, price, confidence, reason, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                base_cols,
            )
        conn.commit()
        return cur.lastrowid


def get_signals(limit: int = 100) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def mark_signal_acted(signal_id: int) -> None:
    with _conn() as conn:
        conn.execute("UPDATE signals SET acted_on=1 WHERE id=?", (signal_id,))
        conn.commit()


# ── Account / positions ───────────────────────────────────────────────────────

def get_cash_balance() -> float:
    with _conn() as conn:
        row = conn.execute("SELECT cash FROM account WHERE id=1").fetchone()
        return float(row["cash"]) if row else config.STARTING_BALANCE


def _set_cash(amount: float) -> None:
    with _conn() as conn:
        conn.execute("UPDATE account SET cash=? WHERE id=1", (round(amount, 4),))
        conn.commit()


def get_open_market_ids() -> set:
    """
    Return set of market_ids that are either:
      - currently open, OR
      - closed within the last 24 hours (cooldown prevents re-entry on same loser)
    """
    with _conn() as conn:
        open_rows = conn.execute(
            "SELECT DISTINCT market_id FROM positions WHERE status='open'"
        ).fetchall()
        cooldown_rows = conn.execute(
            """
            SELECT DISTINCT market_id FROM trades
            WHERE closed_at >= datetime('now', '-24 hours')
            """
        ).fetchall()
        return {r[0] for r in open_rows} | {r[0] for r in cooldown_rows}


def get_open_positions_count() -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM positions WHERE status='open'"
        ).fetchone()
        return int(row["cnt"]) if row else 0


def get_open_position_cost() -> float:
    """Sum of (entry_price × size) across all open positions. Single source of truth."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT entry_price, size FROM positions WHERE status='open'"
        ).fetchall()
    total = 0.0
    for r in rows:
        try:
            total += float(r[0]) * float(r[1])
        except (TypeError, ValueError):
            continue
    return total


def get_cluster_exposure(end_date_iso: str, window_days: int = 7) -> float:
    """
    Return total cost (entry_price × size) of open positions whose underlying
    market resolves within `window_days` days of `end_date_iso`.

    Used by Gate 7 to enforce CORRELATION_CLUSTER_CAP_PCT: positions that resolve
    in the same ~7-day window are correlated — if one event surprises the market,
    related events often move together.

    `end_date_iso` is the end_date of the candidate signal being evaluated.
    Returns 0.0 if end_date_iso is empty or unparseable (safe-pass).
    """
    if not end_date_iso:
        return 0.0

    from datetime import datetime, timezone, timedelta
    try:
        candidate_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        if candidate_dt.tzinfo is None:
            candidate_dt = candidate_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0.0

    window_start = (candidate_dt - timedelta(days=window_days)).isoformat(timespec="seconds").replace("+00:00", "")
    window_end   = (candidate_dt + timedelta(days=window_days)).isoformat(timespec="seconds").replace("+00:00", "")

    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT p.entry_price, p.size
              FROM positions p
              JOIN edgefinder_signals e ON p.edgefinder_signal_id = e.id
             WHERE p.status = 'open'
               AND e.detected_at BETWEEN ? AND ?
            """,
            (window_start, window_end),
        ).fetchall()

    total = 0.0
    for r in rows:
        try:
            total += float(r[0]) * float(r[1])
        except (TypeError, ValueError):
            continue
    return total


def get_open_positions() -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_closed_trades() -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY closed_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def open_position(
    market_id: str,
    token_id: str,
    question: str,
    side: str,
    entry_price: float,
    size: float,
) -> Optional[Dict]:
    cost = round(entry_price * size, 4)
    cash = get_cash_balance()
    if cost > cash:
        return None

    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO positions
              (market_id, token_id, question, side, entry_price, size,
               current_price, unrealized_pnl, opened_at, status)
            VALUES (?,?,?,?,?,?,?,0,?,?)
            """,
            (market_id, token_id, question, side,
             entry_price, size, entry_price, _now(), "open"),
        )
        pos_id = cur.lastrowid
        conn.commit()

    _set_cash(cash - cost)
    return {
        "id": pos_id, "market_id": market_id, "token_id": token_id,
        "question": question, "side": side,
        "entry_price": entry_price, "size": size,
        "current_price": entry_price, "unrealized_pnl": 0,
    }


def close_position(position_id: int, exit_price: float) -> Optional[Dict]:
    with _conn() as conn:
        pos = conn.execute(
            "SELECT * FROM positions WHERE id=? AND status='open'", (position_id,)
        ).fetchone()
        if not pos:
            return None
        pos = dict(pos)

        realized_pnl = round((exit_price - pos["entry_price"]) * pos["size"], 4)
        proceeds     = round(exit_price * pos["size"], 4)

        conn.execute(
            """
            INSERT INTO trades
              (market_id, token_id, question, side, entry_price, exit_price,
               size, realized_pnl, opened_at, closed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                pos["market_id"], pos["token_id"], pos["question"], pos["side"],
                pos["entry_price"], exit_price, pos["size"], realized_pnl,
                pos["opened_at"], _now(),
            ),
        )
        conn.execute("UPDATE positions SET status='closed' WHERE id=?", (position_id,))
        conn.commit()

    _set_cash(get_cash_balance() + proceeds)
    return {**pos, "exit_price": exit_price, "realized_pnl": realized_pnl}


def update_position_price(position_id: int, current_price: float) -> None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT entry_price, size FROM positions WHERE id=?", (position_id,)
        ).fetchone()
        if row:
            unrealized = round((current_price - row["entry_price"]) * row["size"], 4)
            conn.execute(
                "UPDATE positions SET current_price=?, unrealized_pnl=? WHERE id=?",
                (current_price, unrealized, position_id),
            )
            conn.commit()


# ── Maintenance ───────────────────────────────────────────────────────────────

def reset_paper_account() -> None:
    """Wipe all paper trading state and restore starting balance."""
    with _conn() as conn:
        conn.executescript("""
        DELETE FROM positions;
        DELETE FROM trades;
        DELETE FROM signals;
        DELETE FROM rejected_signals;
        DELETE FROM bot_logs;
        """)
        conn.execute("UPDATE account SET cash=?", (config.STARTING_BALANCE,))
        conn.commit()


def reset_market_cache() -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM markets")
        conn.execute("DELETE FROM rejected_signals")
        conn.commit()


# ── EdgeFinder signals (added by migration 001) ──────────────────────────────

def upsert_edgefinder_signal(row: Dict[str, Any]) -> None:
    """Insert or replace one EdgeFinder signal keyed by its id."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO edgefinder_signals
              (id, question, direction, yes_price, signal_type, pattern,
               market_url, status, detected_at, resolved_at,
               free_signal_of_day, fetched_at, our_paper_action)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              status            = excluded.status,
              resolved_at       = excluded.resolved_at,
              fetched_at        = excluded.fetched_at
            """,
            (
                row.get("id", ""),
                row.get("question", ""),
                row.get("direction", "NO"),
                row.get("yes_price"),
                row.get("signal_type", ""),
                row.get("pattern", ""),
                row.get("market_url", ""),
                row.get("status", "pending"),
                row.get("detected_at", ""),
                row.get("resolved_at"),
                1 if row.get("free_signal_of_day") else 0,
                row.get("fetched_at", _now()),
                row.get("our_paper_action"),
            ),
        )
        conn.commit()


def get_edgefinder_signals(status: Optional[str] = None, limit: int = 200) -> List[Dict]:
    """Return mirrored EdgeFinder signals, optionally filtered by status."""
    with _conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM edgefinder_signals WHERE status=? "
                "ORDER BY detected_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM edgefinder_signals ORDER BY detected_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def mark_edgefinder_action(signal_id: str, action: str) -> None:
    """Record what our paper bot did with a signal — 'taken' or 'skipped:<reason>'."""
    with _conn() as conn:
        conn.execute(
            "UPDATE edgefinder_signals SET our_paper_action=? WHERE id=?",
            (action, signal_id),
        )
        conn.commit()


def insert_edgefinder_stats_snapshot(stats: Dict[str, Any]) -> None:
    """Trailing record of EdgeFinder's published aggregate win rate."""
    overall = stats.get("overall", {})
    by_type = stats.get("by_type", {})
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO edgefinder_stats_snapshots
              (snapshot_at, day, overall_win_rate, overall_wins,
               overall_losses, overall_pending, ml_win_rate,
               crash_win_rate, combined_win_rate, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _now(),
                stats.get("day"),
                overall.get("win_rate"),
                overall.get("wins"),
                overall.get("losses"),
                overall.get("pending"),
                by_type.get("ml", {}).get("win_rate"),
                by_type.get("crash", {}).get("win_rate"),
                by_type.get("combined", {}).get("win_rate"),
                json.dumps(stats),
            ),
        )
        conn.commit()


def latest_edgefinder_stats() -> Optional[Dict]:
    """Most recent stats snapshot (for the UI)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM edgefinder_stats_snapshots "
            "ORDER BY snapshot_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
