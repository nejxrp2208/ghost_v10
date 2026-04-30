"""
restore_pm_rule.py — undo the bad flip from re_resolve_strict.py and
re-resolve every closed trade against Polymarket's actual rule.

Polymarket settlement rule (from PM docs):
    p_end >= p_start  →  UP wins   (flat counts as UP)
    p_end <  p_start  →  DOWN wins (DOWN requires strict drop)

The earlier re_resolve_strict.py I had you run used `p_end > p_start`
(strict). That was WRONG. It flipped flat-UP wins to losses (they were
actually wins) and flat-DOWN losses stayed the same (they were correct,
but for the wrong reason).

This script walks every closed trade, re-applies the correct PM rule
using the price_start / price_end already in the DB, and reports / fixes
any rows where the bot's recorded status disagrees with the PM rule.

Trades where bot was already correct (or where re_resolve_strict left
them correct) won't change. Trades where re_resolve_strict.py incorrectly
flipped them from won→lost will flip BACK to won.

Usage:
    python restore_pm_rule.py             # dry run, prints diff only
    python restore_pm_rule.py --apply     # writes corrections to DB

This does NOT need network access — uses prices already recorded.
"""

import sqlite3
import sys
import os
import shutil
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

PAPER = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true","1","yes")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "crypto_ghost_PAPER.db" if PAPER else "crypto_ghost.db")
APPLY = "--apply" in sys.argv


def pm_rule_status(outcome: str, p_start, p_end):
    """Apply Polymarket's documented rule. Returns 'won', 'lost', or None."""
    if p_start is None or p_end is None:
        return None
    bought_up = (outcome or "").upper() in ("UP","YES","HIGHER","ABOVE")
    market_resolves_up = p_end >= p_start
    if bought_up:
        return "won" if market_resolves_up else "lost"
    return "lost" if market_resolves_up else "won"


def main():
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        return

    if APPLY:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = DB_PATH.replace(".db", f"_BACKUP_PRE_PMRULE_{ts}.db")
        shutil.copy(DB_PATH, backup)
        print(f"[BACKUP] {backup}")

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, coin, outcome, status,
               size_usdc, entry_price, pnl,
               price_start, price_end
        FROM trades
        WHERE status IN ('won','lost')
        ORDER BY id ASC
    """).fetchall()

    print(f"Re-checking {len(rows)} closed trades against Polymarket rule")
    print(f"Mode: {'APPLY (will write)' if APPLY else 'DRY RUN — use --apply to write'}\n")
    print(f"{'ID':>3} {'Coin':4} {'Side':4} {'Bot':6} {'PM':6} "
          f"{'Δ%':>9} {'BotPnL':>9} {'PMPnL':>9} {'Δ':>9}  note")
    print("-" * 105)

    flipped = unchanged = unknown = 0
    bot_total = pm_total = 0.0

    for tid, coin, side, status, size, entry, pnl, ps, pe in rows:
        bot_total += (pnl or 0)
        new_status = pm_rule_status(side, ps, pe)
        if new_status is None:
            unknown += 1
            pm_total += (pnl or 0)
            print(f"{tid:>3} {coin:4} {side:4} {status:6} {'?':6} "
                  f"{'-':>9} {pnl or 0:>+9.2f} {pnl or 0:>+9.2f} {0:>+9.2f}  no prices")
            continue

        pct = ((pe - ps) / ps * 100) if ps else 0
        if new_status == "won":
            new_pnl = round((size / entry) - size, 2) if entry > 0 else 0
        else:
            new_pnl = -float(size)
        delta = new_pnl - (pnl or 0)
        pm_total += new_pnl

        flat = (ps == pe)
        flat_note = "  (flat → UP wins)" if flat else ""

        if new_status == status:
            unchanged += 1
            print(f"{tid:>3} {coin:4} {side:4} {status:6} {new_status:6} "
                  f"{pct:>+9.4f} {pnl or 0:>+9.2f} {new_pnl:>+9.2f} {delta:>+9.2f}  ok{flat_note}")
            continue

        flipped += 1
        action = "FIXED" if APPLY else "WOULD FIX"
        print(f"{tid:>3} {coin:4} {side:4} {status:6} {new_status:6} "
              f"{pct:>+9.4f} {pnl or 0:>+9.2f} {new_pnl:>+9.2f} {delta:>+9.2f}  *** {action}{flat_note}")

        if APPLY:
            conn.execute("""
                UPDATE trades
                SET status = ?, pnl = ?, resolution_verified = 7
                WHERE id = ?
            """, (new_status, new_pnl, tid))

    if APPLY:
        conn.commit()
    conn.close()

    print("\n" + "=" * 60)
    print(f"Flipped (would-fix or fixed):    {flipped}")
    print(f"Unchanged:                       {unchanged}")
    print(f"Unknown (no prices):             {unknown}")
    print()
    print(f"Bot's recorded total P&L:  ${bot_total:+,.2f}")
    print(f"PM-rule total P&L:         ${pm_total:+,.2f}")
    print(f"Difference:                ${pm_total - bot_total:+,.2f}")
    if not APPLY and flipped:
        print("\nDry run only. Re-run with --apply to write the fixes.")


if __name__ == "__main__":
    main()
