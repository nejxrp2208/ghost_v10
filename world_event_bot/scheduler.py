"""
Background scheduler — v2 (EdgeFinder consumer).

Polls the EdgeFinder API, evaluates pending signals through the seven gates,
paper-trades the ones that pass, and manages open positions.

Run in its own terminal:
    .venv\\Scripts\\python.exe scheduler.py

The Streamlit dashboard can be open or closed independently. All state
is written to SQLite.
"""

import logging
import sys
import time
from datetime import datetime, timezone

import config
import database as db
import edgefinder_client
import our_engine
from market_scanner import scan_markets
from signal_engine import generate_signals
from paper_trader import execute_signals, update_positions, auto_resolve_positions

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scheduler")

# ── Config ───────────────────────────────────────────────────────────────────
CYCLE_INTERVAL_S          = int(config.EDGEFINDER_POLL_INTERVAL_S)
POSITION_CHECK_INTERVAL_S = int(config.POSITION_CHECK_INTERVAL_S)


def manage_positions() -> None:
    """
    Stop-loss + EdgeFinder-resolution close-outs, then refresh open-position
    prices. Runs on its own fast cadence (POSITION_CHECK_INTERVAL_S) between
    full signal cycles so a fast YES move trips the stop-loss before it gaps
    far past STOP_LOSS_YES_THRESHOLD.
    """
    try:
        resolved = auto_resolve_positions()
        if resolved:
            logger.info("  -> closed %d positions (stop-loss / EdgeFinder resolution)", len(resolved))
        update_positions()
    except Exception as e:
        logger.error("  Position management failed: %s", e)
        db.log_message("ERROR", f"Position-check error: {e}")


def run_cycle(cycle_num: int) -> None:
    logger.info("=" * 60)
    logger.info("CYCLE %d — %s UTC", cycle_num, datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    # 1. Refresh EdgeFinder feed + stats (mirrors into DB)
    logger.info("Step 1/5 — Refresh EdgeFinder signals + stats...")
    try:
        signals = edgefinder_client.fetch_signals()
        stats   = edgefinder_client.fetch_stats()
        pending = [s for s in signals if s.get("status") == "pending"]
        logger.info(
            "  -> %d total signals / %d pending; published WR %.1f%% (Day %s)",
            len(signals), len(pending),
            (stats.get("overall", {}) or {}).get("win_rate", 0.0),
            stats.get("day", "?"),
        )
    except Exception as e:
        logger.error("  EdgeFinder fetch failed: %s", e)
        db.log_message("ERROR", f"Cycle {cycle_num} EdgeFinder fetch error: {e}")
        return

    # 2. Evaluate signals through the seven gates → emit signal rows
    logger.info("Step 2/5 — Evaluating signals through entry gates...")
    try:
        new_signals = generate_signals()
        logger.info("  -> %d signals passed all gates", len(new_signals))
    except Exception as e:
        logger.error("  Signal evaluation failed: %s", e)
        db.log_message("ERROR", f"Cycle {cycle_num} signal_engine error: {e}")
        new_signals = []

    # 3. Paper-execute the taken signals (Kelly sizing)
    logger.info("Step 3/5 — Paper-trading new signals...")
    try:
        trades = execute_signals(new_signals)
        logger.info("  -> %d paper positions opened", len(trades))
    except Exception as e:
        logger.error("  Paper execution failed: %s", e)
        db.log_message("ERROR", f"Cycle {cycle_num} execute error: {e}")
        trades = []

    # 4. Scan Polymarket + snapshot + fire our own rule-based signals (Phase A)
    logger.info("Step 4/5 — Scanning Polymarket for snapshots + our-engine signals...")
    try:
        scanned = scan_markets()
        our_fired = our_engine.run_cycle(scanned)
        logger.info("  -> %d markets snapshotted; our engine fired %d signals", len(scanned), len(our_fired))
    except Exception as e:
        logger.error("  Polymarket scan / our_engine failed: %s", e)
        db.log_message("ERROR", f"Cycle {cycle_num} our_engine error: {e}")

    # 5. Manage open positions: stop-loss + EdgeFinder resolution close-outs
    logger.info("Step 5/5 — Updating open positions...")
    manage_positions()
    logger.info("  -> open=%d  cash=$%,.2f", db.get_open_positions_count(), db.get_cash_balance())

    # 6. Calibrator auto-retrain check (cheap — usually a no-op)
    try:
        from ml.calibrator import maybe_retrain as _calib_retrain
        result = _calib_retrain()
        if result is not None:
            logger.info("  -> calibrator retrained (promoted=%s)", result.get("promoted"))
            db.log_message(
                "INFO",
                f"Calibrator retrained — promoted={result.get('promoted')} "
                f"AUC={result.get('holdout_auc')} ECE={result.get('holdout_ece')}"
            )
    except Exception as e:
        logger.warning("  Calibrator retrain check failed: %s", e)

    db.log_message(
        "INFO",
        f"Cycle {cycle_num} done — {len(new_signals)} signals / "
        f"{len(trades)} new positions / "
        f"{db.get_open_positions_count()} open"
    )
    logger.info("Cycle %d complete.\n", cycle_num)


def main():
    logger.info("Politicas EdgeFinder consumer — Background Scheduler")
    logger.info("Poll interval        : every %ds (%.1f min)", CYCLE_INTERVAL_S, CYCLE_INTERVAL_S/60)
    logger.info("Position-check int.  : every %ds", POSITION_CHECK_INTERVAL_S)
    logger.info("Kelly fraction       : %.2f (default half-K)", config.KELLY_FRACTION)
    logger.info("Max per trade        : %.0f%% of bankroll", config.MAX_BANKROLL_PCT_PER_TRADE * 100)
    logger.info("Cash reserve         : %.0f%%", config.RESERVE_PCT * 100)
    logger.info("Min market volume    : $%,.0f / 24h", config.MIN_VOLUME_24HR)
    logger.info("Polymarket fee model : %.1f%%", config.POLYMARKET_FEE_RATE * 100)
    logger.info("Stop-loss YES        : %.2f", config.STOP_LOSS_YES_THRESHOLD)
    logger.info("Starting bankroll    : $%,.2f", config.STARTING_BALANCE)
    logger.info("Press Ctrl+C to stop.\n")

    db.init_db()

    cycle = 1
    while True:
        try:
            run_cycle(cycle)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user.")
            break
        except Exception as e:
            logger.error("Unexpected error in cycle %d: %s", cycle, e)
            db.log_message("ERROR", f"Unexpected scheduler error: {e}")

        # Sleep until the next full cycle, but wake every POSITION_CHECK_INTERVAL_S
        # to run a stop-loss / resolution sweep so fast moves don't gap past the SL.
        logger.info("Next full cycle in %ds (position checks every %ds)...\n",
                    CYCLE_INTERVAL_S, POSITION_CHECK_INTERVAL_S)
        try:
            elapsed = 0
            while elapsed < CYCLE_INTERVAL_S:
                step = min(POSITION_CHECK_INTERVAL_S, CYCLE_INTERVAL_S - elapsed)
                time.sleep(step)
                elapsed += step
                if elapsed < CYCLE_INTERVAL_S:   # the final sweep is the next full cycle's job
                    manage_positions()
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user.")
            break
        cycle += 1


if __name__ == "__main__":
    main()
