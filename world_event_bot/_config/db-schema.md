# SQLite Schema Reference — v2

> Layer 3 reference. **Updated 2026-05-27** for the EdgeFinder overhaul. Run [`_migrations/001_edgefinder_overhaul.py`](../_migrations/001_edgefinder_overhaul.py) to apply.

DB file: `polymarket_event_bot.db` (overrideable via `DB_PATH` env). Journal mode: WAL.

---

## Tables

### `edgefinder_signals` (NEW)
Mirror of EdgeFinder's `/api/signals` feed.

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | EdgeFinder's signal id (e.g. `"aa77f404"`) |
| `question`, `market_url` | TEXT | From feed |
| `direction` | TEXT default `'NO'` | Always NO in practice |
| `yes_price` | REAL | YES price at detection |
| `signal_type` | TEXT | `'ml'` / `'crash'` / `'combined'` |
| `pattern` | TEXT | `'Momentum Crash'` / `'Extreme Crash'` / empty |
| `status` | TEXT | `'pending'` / `'win'` / `'loss'` |
| `detected_at`, `resolved_at`, `fetched_at` | TEXT | ISO-8601 |
| `free_signal_of_day` | INTEGER | 0/1 marketing flag |
| `our_paper_action` | TEXT | `'taken'` / `'skipped:<reason>'` / NULL |

Indexes: `status`, `detected_at`, `signal_type`.

Upsert via `db.upsert_edgefinder_signal`. Never `INSERT` directly. The upsert preserves `our_paper_action`.

### `edgefinder_stats_snapshots` (NEW)
Trailing record of EdgeFinder's published aggregate win rate. One row per `fetch_stats()` call.

| Column | Notes |
|--------|-------|
| `id` PK autoincrement |
| `snapshot_at` | ISO-8601 |
| `day` | EdgeFinder's `day` field |
| `overall_win_rate`, `overall_wins`, `overall_losses`, `overall_pending` | REAL/INT |
| `ml_win_rate`, `crash_win_rate`, `combined_win_rate` | REAL |
| `raw_json` | Full payload, in case we need a field we forgot to materialize |

Useful for: charting how EdgeFinder's published rate evolves over time.

### `positions` (UPDATED — new columns)
Open paper positions.

| Column | Type | Notes |
|--------|------|-------|
| existing columns | … | (unchanged) |
| `edgefinder_signal_id` | TEXT | FK to `edgefinder_signals.id` (logical, not enforced) |
| `stop_loss_price` | REAL | YES-price threshold above which we close |
| `signal_type` | TEXT | Denormalized from EdgeFinder |
| `pattern` | TEXT | Denormalized from EdgeFinder |

### `signals` (UPDATED — new columns)
Now stores the *trader's* signal record (the moment our bot decided to take an EdgeFinder signal), not raw EdgeFinder data.

| Column | Type | Notes |
|--------|------|-------|
| existing columns | … | (unchanged) |
| `yes_price_at_emit` | REAL | YES on Polymarket at moment we acted |
| `signal_age_at_emit_seconds` | INTEGER | `now - edgefinder_signal.detected_at` |
| `fee_adjusted_ev` | REAL | From `sizing.KellyOutcome.ev_per_dollar` |
| `edgefinder_signal_id` | TEXT | Pointer back to the EdgeFinder row |

### `trades`, `rejected_signals`, `bot_logs`, `account`
Unchanged from v1.

### `markets`
Preserved. Still useful as a Polymarket CLOB enrichment cache when we look up the current ask for an EdgeFinder signal's underlying market. Not load-bearing for signal selection anymore.

---

## Invariants

1. **Cash invariant** — `cash + sum(open position cost) + sum(realized P&L)` ≈ STARTING_BALANCE (within $0.01).
2. **One open position per `edgefinder_signal_id`** — enforced in `paper_trader.execute_signals` (not by DB constraint).
3. **24h cooldown** after `close_position` on same `market_id`.
4. **`our_paper_action` is append-once-then-stable** — set when we take or skip a signal; the upsert from `edgefinder_client.fetch_signals` preserves it.
5. **All DB access through [database.py](../database.py).** No raw `sqlite3.connect` elsewhere except in `_migrations/`.

---

## Migration trail

| Migration | What | When |
|-----------|------|------|
| `001_edgefinder_overhaul.py` | New tables + columns + wipe | **Pending — run after Phase 4 UI exists** |

---

## When to revise this file

- Any `ALTER TABLE`, new table, or new column
- Any new public function in `database.py` that exposes a query
- Any change to an invariant above
