"""
re_resolve_chainlink.py — re-resolve every closed trade using Chainlink
prices on Polygon (= what Polymarket's UMA oracle uses), and write the
authoritative result to the DB.

This is the SAME data that determines whether your real wallet gets paid
in live mode. After running this, your DB matches Polymarket exactly.

Usage:
    python re_resolve_chainlink.py             # dry run
    python re_resolve_chainlink.py --apply     # write to DB (with backup)

Setup:
    pip install web3
"""

import sqlite3
import shutil
import os
import sys
import re
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:
    _ET_TZ = None

try:
    from web3 import Web3
except ImportError:
    print("ERROR: web3 not installed. Run:  pip install web3")
    sys.exit(1)

# Reuse all the helpers from the audit script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_chainlink import (
    CHAINLINK_FEEDS, ChainlinkFeed, connect_polygon,
    parse_market_times, pm_rule_status, AGG_V3_ABI,
)

PAPER = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true","1","yes")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "crypto_ghost_PAPER.db" if PAPER else "crypto_ghost.db")
APPLY = "--apply" in sys.argv


def main():
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        return

    if APPLY:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = DB_PATH.replace(".db", f"_BACKUP_PRE_CHAINLINK_{ts}.db")
        shutil.copy(DB_PATH, backup)
        print(f"[BACKUP] {backup}")

    print("Connecting to Polygon...")
    w3 = connect_polygon()
    if w3 is None:
        print("ERROR: Could not connect to any Polygon RPC.")
        return

    print("Loading Chainlink feeds...")
    feeds = {}
    for coin, addr in CHAINLINK_FEEDS.items():
        try:
            feeds[coin] = ChainlinkFeed(w3, addr)
        except Exception as e:
            print(f"  {coin}: failed ({e})")

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, coin, outcome, status,
               size_usdc, entry_price, pnl, question
        FROM trades WHERE status IN ('won','lost')
        ORDER BY id ASC
    """).fetchall()

    print(f"\nRe-resolving {len(rows)} trades against Chainlink truth")
    print(f"Mode: {'APPLY' if APPLY else 'DRY RUN'}\n")
    print(f"{'ID':>3} {'Coin':4} {'Side':4} {'Bot':6} {'CL':6} "
          f"{'CL Δ%':>9} {'BotPnL':>9} {'CL PnL':>9} {'Δ':>9}  action")
    print("-" * 105)

    flipped = unchanged = errored = 0
    bot_total = cl_total = 0.0

    for tid, coin, side, status, size, entry, pnl, q in rows:
        bot_total += (pnl or 0)
        feed = feeds.get((coin or "").upper())
        start_utc, end_utc = parse_market_times(q or "")
        if feed is None or not start_utc or not end_utc:
            errored += 1
            cl_total += (pnl or 0)
            continue

        try:
            ps = feed.price_at(int(start_utc.timestamp()))
            pe = feed.price_at(int(end_utc.timestamp()))
        except Exception:
            ps = pe = None

        if ps is None or pe is None:
            errored += 1
            cl_total += (pnl or 0)
            print(f"{tid:>3} {coin:4} {side:4} {status:6} {'ERR':6}   Chainlink lookup failed")
            continue

        cl_status = pm_rule_status(side, ps, pe)
        cl_pct = ((pe - ps) / ps * 100) if ps else 0
        if cl_status == "won":
            cl_pnl = round((size / entry) - size, 2) if entry > 0 else 0
        else:
            cl_pnl = -float(size)
        delta = cl_pnl - (pnl or 0)
        cl_total += cl_pnl

        if cl_status == status:
            unchanged += 1
            print(f"{tid:>3} {coin:4} {side:4} {status:6} {cl_status:6} "
                  f"{cl_pct:>+9.4f} {pnl or 0:>+9.2f} {cl_pnl:>+9.2f} {delta:>+9.2f}  ok")
            continue

        flipped += 1
        action = "FIXED" if APPLY else "WOULD FIX"
        print(f"{tid:>3} {coin:4} {side:4} {status:6} {cl_status:6} "
              f"{cl_pct:>+9.4f} {pnl or 0:>+9.2f} {cl_pnl:>+9.2f} {delta:>+9.2f}  *** {action}")

        if APPLY:
            conn.execute("""
                UPDATE trades
                SET status = ?, pnl = ?,
                    price_start = ?, price_end = ?,
                    resolution_verified = 9
                WHERE id = ?
            """, (cl_status, cl_pnl, ps, pe, tid))

    if APPLY:
        conn.commit()
    conn.close()

    print("\n" + "=" * 60)
    print(f"Flipped:    {flipped}")
    print(f"Unchanged:  {unchanged}")
    print(f"Errored:    {errored}")
    print()
    print(f"Bot's recorded total:    ${bot_total:+,.2f}")
    print(f"Chainlink-truth total:   ${cl_total:+,.2f}")
    print(f"Difference:              ${cl_total - bot_total:+,.2f}")
    if not APPLY and flipped:
        print("\nDry run only. Re-run with --apply to write fixes to DB.")


if __name__ == "__main__":
    main()
