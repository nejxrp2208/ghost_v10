"""
GhostWatcher — Edge intelligence observer
Reads scanner.db + marketghost.db every 30 minutes.
Writes findings to ugotovitve.db (SQLite, no size limit).

PM2: pm2 start ghost_watcher.py --name ghost-watcher
"""

import sqlite3
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple

# ── Paths ──────────────────────────────────────────────────────────────────────
SD              = os.path.dirname(os.path.abspath(__file__))
SCANNER_DB      = os.path.join(SD, "scanner.db")
MARKETGHOST_DB  = os.path.join(SD, "marketghost.db")
UGOTOVITVE_DB   = os.path.join(SD, "ugotovitve.db")

# ── Config ─────────────────────────────────────────────────────────────────────
WATCH_INTERVAL    = 1800  # seconds between cycles (30 min)
FEATURE_WINDOW_S  = 90    # last N seconds for feature calculation
OBI_WINDOW_S      = 60    # last N seconds for OBI (book writes every 30s → 2 snapshots)
ACTIVE_CUTOFF_S   = 120   # market considered active if scanner wrote in last 2 min
OBI_THRESHOLD     = 0.15  # stat bucket: OBI > this = strong buyer pressure
GAP_THRESHOLD     = -0.3  # stat bucket: price_gap_pct < this = Binance below strike


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── DB Init ────────────────────────────────────────────────────────────────────
def init_ugotovitve(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS findings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc           TEXT    NOT NULL,
            asset            TEXT    NOT NULL,
            tf               TEXT    NOT NULL,
            status           TEXT    NOT NULL,
            cheap_side       TEXT,
            cheap_ask_cents  INTEGER,
            obi              REAL,
            bin_momentum_90s REAL,
            spread_first     REAL,
            spread_last      REAL,
            price_gap_pct    REAL,
            secs_left        INTEGER,
            winner           TEXT,
            cheap_won        INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc            TEXT    NOT NULL,
            total_closed      INTEGER,
            cheap_won_total   INTEGER,
            cheap_lost_total  INTEGER,
            obi_gt015_won     INTEGER,
            obi_gt015_lost    INTEGER,
            gap_neg_won       INTEGER,
            gap_neg_lost      INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_ts     ON findings(ts_utc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_asset  ON findings(asset, tf)")
    conn.commit()


# ── Korak 3: Active markets ────────────────────────────────────────────────────
def get_active_markets(scanner: sqlite3.Connection) -> List[Tuple[str, str]]:
    """Returns list of (asset, tf) with data written in last 2 minutes."""
    cutoff_ms = _now_ms() - ACTIVE_CUTOFF_S * 1000
    try:
        rows = scanner.execute(
            "SELECT DISTINCT asset, tf FROM summary WHERE ts > ?", (cutoff_ms,)
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception as e:
        print(f"  [WARN] get_active_markets: {e}")
        return []


# ── Korak 4: Features from last 90 seconds ────────────────────────────────────
def get_features(scanner: sqlite3.Connection, asset: str, tf: str) -> Optional[Dict]:
    """Calculates features from last FEATURE_WINDOW_S rows for a given market."""
    now_ms   = _now_ms()
    start_ms = now_ms - FEATURE_WINDOW_S * 1000
    try:
        rows = scanner.execute("""
            SELECT ts, up_ask, down_ask, up_spread, down_spread,
                   bin_price, ptb_usd, secs_left
            FROM summary
            WHERE asset=? AND tf=? AND ts BETWEEN ? AND ?
            ORDER BY ts ASC
        """, (asset, tf, start_ms, now_ms)).fetchall()
    except Exception as e:
        print(f"  [WARN] get_features {asset} {tf}: {e}")
        return None

    valid = [r for r in rows if r[1] is not None and r[2] is not None]
    if len(valid) < 5:
        return None

    last = valid[-1]
    up_ask_last   = last[1]
    down_ask_last = last[2]

    cheap_side      = "UP" if up_ask_last < down_ask_last else "DOWN"
    cheap_ask_cents = min(up_ask_last, down_ask_last)

    # Spread trend: compare first third vs last third of window
    third        = max(1, len(valid) // 3)
    spread_col   = 3 if cheap_side == "UP" else 4  # up_spread=idx3, down_spread=idx4
    early_vals   = [r[spread_col] for r in valid[:third]  if r[spread_col] is not None]
    late_vals    = [r[spread_col] for r in valid[-third:] if r[spread_col] is not None]
    spread_first = round(sum(early_vals) / len(early_vals), 2) if early_vals else None
    spread_last  = round(sum(late_vals)  / len(late_vals),  2) if late_vals  else None

    # Binance momentum over full window
    bin_prices  = [r[5] for r in valid if r[5] is not None]
    bin_momentum = None
    if len(bin_prices) >= 2 and bin_prices[0] > 0:
        bin_momentum = round((bin_prices[-1] - bin_prices[0]) / bin_prices[0] * 100, 4)

    # Price gap: how far Binance is above/below strike
    ptb_usd   = last[6]
    bin_price = last[5]
    price_gap_pct = None
    if ptb_usd and bin_price and ptb_usd > 0:
        price_gap_pct = round((bin_price - ptb_usd) / ptb_usd * 100, 4)

    return {
        "cheap_side":       cheap_side,
        "cheap_ask_cents":  cheap_ask_cents,
        "bin_momentum_90s": bin_momentum,
        "spread_first":     spread_first,
        "spread_last":      spread_last,
        "price_gap_pct":    price_gap_pct,
        "secs_left":        last[7],
    }


# ── Korak 5: OBI from book table ──────────────────────────────────────────────
def get_obi(scanner: sqlite3.Connection, asset: str, tf: str, cheap_side: str,
            ref_ms: Optional[int] = None) -> Optional[float]:
    """OBI = (bid_usd - ask_usd) / (bid_usd + ask_usd) for cheap token.
    Range [-1, +1]. Positive = more buyers = upward price pressure on cheap token.
    ref_ms: reference timestamp in ms (defaults to now, pass close time for closed markets)."""
    ref_ms   = ref_ms if ref_ms is not None else _now_ms()
    start_ms = ref_ms - OBI_WINDOW_S * 1000
    try:
        rows = scanner.execute("""
            SELECT order_type, SUM(total_usd)
            FROM book
            WHERE asset=? AND tf=? AND side=? AND ts BETWEEN ? AND ?
            GROUP BY order_type
        """, (asset, tf, cheap_side, start_ms, ref_ms)).fetchall()
    except Exception as e:
        print(f"  [WARN] get_obi {asset} {tf}: {e}")
        return None

    bid_usd = 0.0
    ask_usd = 0.0
    for order_type, depth in rows:
        if order_type == "bid":
            bid_usd = depth or 0.0
        elif order_type == "ask":
            ask_usd = depth or 0.0

    total = bid_usd + ask_usd
    if total < 1.0:
        return None
    return round((bid_usd - ask_usd) / total, 4)


# ── Korak 6: Closed markets ───────────────────────────────────────────────────
def get_closed_markets(mg: sqlite3.Connection) -> List[Dict]:
    """Markets resolved in the last 30 minutes from marketghost.db."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=WATCH_INTERVAL)).isoformat()
    try:
        rows = mg.execute("""
            SELECT market_id, coin, end_utc, winner
            FROM resolutions
            WHERE resolved_at > ?
              AND winner IN ('UP', 'DOWN')
        """, (cutoff,)).fetchall()
        return [{"market_id": r[0], "coin": r[1], "end_utc": r[2], "winner": r[3]}
                for r in rows]
    except Exception as e:
        print(f"  [WARN] get_closed_markets: {e}")
        return []


def get_closed_features(scanner: sqlite3.Connection, asset: str,
                         end_utc_str: str) -> Optional[Dict]:
    """Features from scanner.db in the 90s window before market close.
    end_utc_str is ISO format from marketghost.db resolutions."""
    try:
        dt     = datetime.fromisoformat(end_utc_str.replace("Z", "+00:00"))
        end_ms = int(dt.timestamp() * 1000)
        start_ms = end_ms - FEATURE_WINDOW_S * 1000

        rows = scanner.execute("""
            SELECT ts, up_ask, down_ask, up_spread, down_spread,
                   bin_price, ptb_usd, secs_left, tf
            FROM summary
            WHERE asset=? AND ts BETWEEN ? AND ?
            ORDER BY ts ASC
        """, (asset, start_ms, end_ms)).fetchall()
    except Exception as e:
        print(f"  [WARN] get_closed_features {asset}: {e}")
        return None

    valid = [r for r in rows if r[1] is not None and r[2] is not None]
    if len(valid) < 3:
        return None

    last          = valid[-1]
    tf            = last[8]
    up_ask_last   = last[1]
    down_ask_last = last[2]

    cheap_side      = "UP" if up_ask_last < down_ask_last else "DOWN"
    cheap_ask_cents = min(up_ask_last, down_ask_last)

    bin_prices   = [r[5] for r in valid if r[5] is not None]
    bin_momentum = None
    if len(bin_prices) >= 2 and bin_prices[0] > 0:
        bin_momentum = round((bin_prices[-1] - bin_prices[0]) / bin_prices[0] * 100, 4)

    ptb_usd   = last[6]
    bin_price = last[5]
    price_gap_pct = None
    if ptb_usd and bin_price and ptb_usd > 0:
        price_gap_pct = round((bin_price - ptb_usd) / ptb_usd * 100, 4)

    return {
        "tf":               tf,
        "cheap_side":       cheap_side,
        "cheap_ask_cents":  cheap_ask_cents,
        "bin_momentum_90s": bin_momentum,
        "spread_first":     None,
        "spread_last":      None,
        "price_gap_pct":    price_gap_pct,
        "secs_left":        last[7],
    }


# ── Korak 7a: Write single finding ────────────────────────────────────────────
def write_finding(ug: sqlite3.Connection, ts_utc: str, asset: str, tf: str,
                  status: str, features: Dict, winner: Optional[str]) -> None:
    cheap_won = None
    if winner and features.get("cheap_side"):
        cheap_won = 1 if winner == features["cheap_side"] else 0

    ug.execute("""
        INSERT INTO findings
            (ts_utc, asset, tf, status, cheap_side, cheap_ask_cents, obi,
             bin_momentum_90s, spread_first, spread_last, price_gap_pct, secs_left,
             winner, cheap_won)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ts_utc, asset, tf, status,
        features.get("cheap_side"),
        features.get("cheap_ask_cents"),
        features.get("obi"),
        features.get("bin_momentum_90s"),
        features.get("spread_first"),
        features.get("spread_last"),
        features.get("price_gap_pct"),
        features.get("secs_left"),
        winner,
        cheap_won,
    ))
    ug.commit()


# ── Korak 7b: Rolling stats ────────────────────────────────────────────────────
def update_stats(ug: sqlite3.Connection) -> Dict:
    """Recompute rolling stats from all closed findings and append one stats row."""
    try:
        rows = ug.execute("""
            SELECT obi, price_gap_pct, cheap_won
            FROM findings
            WHERE status='closed' AND cheap_won IS NOT NULL
        """).fetchall()
    except Exception:
        return {}

    total = len(rows)
    if total == 0:
        return {}

    won  = sum(1 for r in rows if r[2] == 1)
    lost = total - won

    obi_gt015_won  = sum(1 for r in rows if r[0] is not None and r[0] > OBI_THRESHOLD and r[2] == 1)
    obi_gt015_lost = sum(1 for r in rows if r[0] is not None and r[0] > OBI_THRESHOLD and r[2] == 0)
    gap_neg_won    = sum(1 for r in rows if r[1] is not None and r[1] < GAP_THRESHOLD  and r[2] == 1)
    gap_neg_lost   = sum(1 for r in rows if r[1] is not None and r[1] < GAP_THRESHOLD  and r[2] == 0)

    ug.execute("""
        INSERT INTO stats
            (ts_utc, total_closed, cheap_won_total, cheap_lost_total,
             obi_gt015_won, obi_gt015_lost, gap_neg_won, gap_neg_lost)
        VALUES (?,?,?,?,?,?,?,?)
    """, (_now_utc(), total, won, lost,
          obi_gt015_won, obi_gt015_lost, gap_neg_won, gap_neg_lost))
    ug.commit()

    return {"total": total, "won": won, "lost": lost,
            "obi_gt015_won": obi_gt015_won, "obi_gt015_lost": obi_gt015_lost,
            "gap_neg_won": gap_neg_won, "gap_neg_lost": gap_neg_lost}


# ── Korak 8: Main cycle ────────────────────────────────────────────────────────
def run_cycle() -> None:
    ts_utc = _now_utc()
    print(f"\n[{ts_utc}] GhostWatcher cycle start")

    scanner = None
    mg      = None
    ug      = None

    try:
        ug = sqlite3.connect(UGOTOVITVE_DB)
        init_ugotovitve(ug)

        # ── Active markets (scanner.db) ────────────────────────────────────────
        if os.path.exists(SCANNER_DB):
            scanner = sqlite3.connect(f"file:{SCANNER_DB}?mode=ro", uri=True)

            active = get_active_markets(scanner)
            print(f"  Active markets: {len(active)}")

            for asset, tf in active:
                feat = get_features(scanner, asset, tf)
                if not feat:
                    continue
                feat["obi"] = get_obi(scanner, asset, tf, feat["cheap_side"])
                write_finding(ug, ts_utc, asset, tf, "active", feat, None)
                print(f"  ACTIVE  {asset:3} {tf:5} | cheap={feat['cheap_side']}({feat['cheap_ask_cents']}¢)"
                      f" OBI={feat['obi']} mom={feat['bin_momentum_90s']}%"
                      f" gap={feat['price_gap_pct']}% secs={feat['secs_left']}")
        else:
            print(f"  [WARN] scanner.db not found — skipping active markets")

        # ── Closed markets (marketghost.db) ───────────────────────────────────
        if os.path.exists(MARKETGHOST_DB):
            mg = sqlite3.connect(f"file:{MARKETGHOST_DB}?mode=ro", uri=True)

            closed = get_closed_markets(mg)
            print(f"  Closed markets (last 30min): {len(closed)}")

            for m in closed:
                asset  = m["coin"]
                winner = m["winner"]

                feat = get_closed_features(scanner, asset, m["end_utc"]) if scanner else None
                if not feat:
                    continue

                tf = feat.pop("tf", "unknown")
                # Pass close time as ref_ms so OBI uses historical book data, not current
                try:
                    close_ms = int(datetime.fromisoformat(
                        m["end_utc"].replace("Z", "+00:00")).timestamp() * 1000)
                except Exception:
                    close_ms = None
                feat["obi"] = get_obi(scanner, asset, tf, feat["cheap_side"], close_ms) if scanner else None
                write_finding(ug, ts_utc, asset, tf, "closed", feat, winner)

                cheap_won = winner == feat.get("cheap_side")
                icon = "✓" if cheap_won else "✗"
                print(f"  CLOSED {icon} {asset:3} {m['end_utc'][:16]} | winner={winner}"
                      f" cheap={'WON' if cheap_won else 'LOST'}"
                      f" OBI={feat.get('obi')} gap={feat.get('price_gap_pct')}%")
        else:
            print(f"  [WARN] marketghost.db not found — skipping closed markets")

        # ── Rolling stats ──────────────────────────────────────────────────────
        stats = update_stats(ug)
        if stats.get("total", 0) > 0:
            wr        = stats["won"] / stats["total"] * 100
            obi_total = stats["obi_gt015_won"] + stats["obi_gt015_lost"]
            obi_wr    = stats["obi_gt015_won"] / obi_total * 100 if obi_total > 0 else 0
            gap_total = stats["gap_neg_won"] + stats["gap_neg_lost"]
            gap_wr    = stats["gap_neg_won"] / gap_total * 100 if gap_total > 0 else 0
            print(f"  STATS: {stats['total']} closed | win {wr:.1f}%"
                  f" | OBI>0.15: {obi_wr:.1f}% (n={obi_total})"
                  f" | gap<-0.3%: {gap_wr:.1f}% (n={gap_total})")

    except Exception as e:
        print(f"  [ERROR] Cycle failed: {e}")
    finally:
        if scanner: scanner.close()
        if mg:      mg.close()
        if ug:      ug.close()

    print(f"[{_now_utc()}] Done. Next cycle in 30 min.")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("GhostWatcher starting")
    print(f"  Scanner DB:      {SCANNER_DB}")
    print(f"  MarketGhost DB:  {MARKETGHOST_DB}")
    print(f"  Output DB:       {UGOTOVITVE_DB}")
    print(f"  Cycle interval:  {WATCH_INTERVAL // 60} min")
    print("=" * 60)

    while True:
        run_cycle()
        time.sleep(WATCH_INTERVAL)
