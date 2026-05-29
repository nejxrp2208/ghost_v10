# -*- coding: utf-8 -*-
"""
World Event Bot — Streamlit dashboard (v2, EdgeFinder consumer).
Run: streamlit run app.py
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utcnow() -> datetime:
    """Naive UTC datetime — replaces deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

import pandas as pd
import streamlit as st

import config
import database as db
import edgefinder_client
import our_engine
from clob_client import estimate_sell_proceeds
from market_scanner import scan_markets
from signal_engine import generate_signals
from paper_trader import (
    execute_signals,
    update_positions,
    auto_resolve_positions,
    close_position_by_id,
    close_all_positions_at_mid,
)
import sizing


# ── Formatting helpers ───────────────────────────────────────────────────────

def _f4(x: Any) -> str:
    try: return f"{float(x):.4f}"
    except (TypeError, ValueError): return "—"

def _pct(x: Any, digits: int = 1) -> str:
    try: return f"{float(x):.{digits}f}%"
    except (TypeError, ValueError): return "—"

def _usd(x: Any, digits: int = 2) -> str:
    try: return f"${float(x):,.{digits}f}"
    except (TypeError, ValueError): return "—"

def _ago(ts: str) -> str:
    if not ts: return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", ""))
        secs = (_utcnow() - dt).total_seconds()
        if secs < 60:    return f"{int(secs)}s ago"
        if secs < 3600:  return f"{int(secs/60)}m ago"
        if secs < 86400: return f"{int(secs/3600)}h ago"
        return f"{int(secs/86400)}d ago"
    except Exception:
        return ts[:16]


# ── Bootstrap ────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
db.init_db()

st.set_page_config(
    page_title="World Event Bot — EdgeFinder Paper Trader",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

_DEFAULTS = {
    "auto_cycle":   False,
    "cycle_count":  0,
    "last_cycle":   None,
    "next_cycle":   None,
}
for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ── Pull live data once per render ───────────────────────────────────────────

@st.cache_data(ttl=int(config.EDGEFINDER_POLL_INTERVAL_S))
def _live_signals() -> List[Dict[str, Any]]:
    return edgefinder_client.fetch_signals()

@st.cache_data(ttl=int(config.EDGEFINDER_POLL_INTERVAL_S))
def _live_stats() -> Dict[str, Any]:
    return edgefinder_client.fetch_stats()


def _run_cycle() -> Dict[str, Any]:
    """Single end-to-end cycle invoked from the UI."""
    edgefinder_client.fetch_signals(force=True)
    edgefinder_client.fetch_stats(force=True)
    new_signals = generate_signals()
    trades      = execute_signals(new_signals)
    closed      = auto_resolve_positions()
    update_positions()

    # Phase A — scan Polymarket, snapshot, fire our own rule-based signals
    our_fired = 0
    snapshots = 0
    try:
        scanned   = scan_markets()
        snapshots = len(scanned)
        our_fired = len(our_engine.run_cycle(scanned))
    except Exception as e:
        db.log_message("WARNING", f"Our-engine step failed in UI cycle: {e}")

    # Calibrator auto-retrain check (cheap — usually a no-op)
    retrained = None
    try:
        from ml.calibrator import maybe_retrain
        retrained = maybe_retrain()
    except Exception as e:
        db.log_message("WARNING", f"Calibrator retrain check failed: {e}")

    st.session_state.cycle_count += 1
    st.session_state.last_cycle = _utcnow()
    return {
        "signals":   len(new_signals),
        "opened":    len(trades),
        "closed":    len(closed),
        "snapshots": snapshots,
        "our_fired": our_fired,
        "retrained": bool(retrained),
        "retrain_promoted": (retrained or {}).get("promoted") if retrained else None,
    }


def _portfolio() -> Dict[str, float]:
    """Compute cash + invested + unrealized + realized in one query pass."""
    cash = db.get_cash_balance()
    open_positions = db.get_open_positions()
    invested   = sum(float(p["entry_price"]) * float(p["size"]) for p in open_positions)
    unrealized = sum(float(p.get("unrealized_pnl") or 0.0) for p in open_positions)
    closed     = db.get_closed_trades()
    realized   = sum(float(t.get("realized_pnl") or 0.0) for t in closed)
    wins  = sum(1 for t in closed if float(t.get("realized_pnl") or 0.0) > 0)
    losses = sum(1 for t in closed if float(t.get("realized_pnl") or 0.0) < 0)
    total  = wins + losses
    equity = cash + invested + unrealized
    return {
        "cash": cash, "invested": invested, "unrealized": unrealized,
        "realized": realized, "equity": equity,
        "wins": wins, "losses": losses, "win_rate": (wins/total*100 if total else 0.0),
        "closed": len(closed), "open": len(open_positions),
        "return_pct": (equity - config.STARTING_BALANCE) / config.STARTING_BALANCE * 100,
    }


# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("World Event Bot")
    st.caption("EdgeFinder consumer · Paper only · No wallet")

    # Manual cycle
    if st.button("Run cycle now", width='stretch', type="primary"):
        with st.spinner("Polling EdgeFinder + Polymarket + evaluating signals..."):
            res = _run_cycle()
        toast = (
            f"EF: {res['signals']} new / {res['opened']} opened / {res['closed']} closed · "
            f"Ours: {res['snapshots']} snapshotted / {res['our_fired']} fired"
        )
        if res.get("retrained"):
            toast += f" · Calibrator retrained (promoted={res.get('retrain_promoted')})"
        st.success(toast)
        st.cache_data.clear()
        st.rerun()

    # Auto-cycle toggle
    st.session_state.auto_cycle = st.toggle(
        "Auto-cycle (every 5 min)",
        value=st.session_state.auto_cycle,
        help="Poll EdgeFinder + run gates + trade automatically.",
    )

    if st.session_state.last_cycle:
        st.caption(f"Last cycle: {st.session_state.last_cycle.strftime('%H:%M:%S')} UTC")
        st.caption(f"Cycles this session: {st.session_state.cycle_count}")

    st.divider()
    st.subheader("Doctrine")
    st.markdown(f"""
| Knob | Value |
|------|-------|
| Kelly fraction | `{config.KELLY_FRACTION:.2f}` |
| Max per trade | `{config.MAX_BANKROLL_PCT_PER_TRADE:.0%}` |
| Cash reserve | `{config.RESERVE_PCT:.0%}` |
| Min volume | `${config.MIN_VOLUME_24HR:,.0f}` |
| Max age | `{config.MAX_SIGNAL_AGE_HOURS}h` |
| Max drift | `{config.MAX_PRICE_DRIFT:.2f}` |
| Fee model | `{config.POLYMARKET_FEE_RATE:.1%}` |
| Stop-loss YES | `{config.STOP_LOSS_YES_THRESHOLD:.2f}` |
| Starting bank | `${config.STARTING_BALANCE:,.0f}` |
""")

    st.divider()
    st.subheader("Danger Zone")
    if st.button("Reset paper account", width='content'):
        db.reset_paper_account()
        st.cache_data.clear()
        st.success("Paper account reset.")
        st.rerun()


# ── Header metrics ───────────────────────────────────────────────────────────

p = _portfolio()
stats = _live_stats()
ef_overall = (stats.get("overall") or {})
ef_ml      = (stats.get("by_type", {}).get("ml") or {})
ef_crash   = (stats.get("by_type", {}).get("crash") or {})

st.title("World Event Bot — EdgeFinder Paper Trader")
st.caption(
    f"EdgeFinder Day {stats.get('day', '?')} · "
    f"overall {ef_overall.get('win_rate', 0):.1f}% · "
    f"ML {ef_ml.get('win_rate', 0):.1f}% · "
    f"Crash {ef_crash.get('win_rate', 0):.1f}% · "
    f"published total {ef_overall.get('total_signals', 0)}"
)

m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Cash",         _usd(p["cash"]))
m2.metric("Invested",     _usd(p["invested"]))
m3.metric("Equity",       _usd(p["equity"]),       delta=_usd(p["equity"] - config.STARTING_BALANCE))
m4.metric("Unrealized",   _usd(p["unrealized"]),   delta=_usd(p["unrealized"]))
m5.metric("Realized",     _usd(p["realized"]),     delta=_usd(p["realized"]))
m6.metric("Return",       _pct(p["return_pct"]),   delta=_pct(p["return_pct"]))

st.divider()


# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_live, tab_pos, tab_perf, tab_ours, tab_calc, tab_logs = st.tabs([
    "EdgeFinder Live",
    "Open Positions",
    "Performance",
    "Our Engine",
    "Sizing Calculator",
    "Bot Logs",
])


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — EdgeFinder Live
# ─────────────────────────────────────────────────────────────────────────────

with tab_live:
    st.subheader("Pending EdgeFinder signals")
    live = _live_signals()
    pending = [s for s in live if s.get("status") == "pending"]
    resolved = [s for s in live if s.get("status") in ("win", "loss")]

    info_l, info_r = st.columns([3, 1])
    info_l.caption(
        f"Cached every {int(config.EDGEFINDER_POLL_INTERVAL_S)}s · "
        f"{len(pending)} pending / {len(resolved)} resolved in last batch · "
        f"Source: api.edgefinderai.org/api/signals"
    )
    if info_r.button("Force refresh feed"):
        st.cache_data.clear()
        st.rerun()

    if pending:
        # Pull our recorded action per signal from DB
        mirrored = {row["id"]: row for row in db.get_edgefinder_signals(status="pending", limit=200)}

        rows = []
        for s in sorted(pending, key=lambda x: x.get("yes_price") or 1.0):
            ef_id = s.get("id", "")
            our_action = (mirrored.get(ef_id, {}) or {}).get("our_paper_action") or "—"
            rows.append({
                "EF id":        ef_id[:8],
                "Signal type":  (s.get("signal_type") or "?").upper(),
                "Pattern":      s.get("pattern") or "—",
                "YES @":        _f4(s.get("yes_price")),
                "NO costs":     _f4(1.0 - (float(s.get("yes_price") or 0.0))),
                "Question":     (s.get("question") or "")[:70],
                "Age":          _ago(s.get("detected_at") or s.get("date_added", "")),
                "Our action":   our_action,
                "Market":       s.get("market_url") or s.get("url", ""),
            })
        df = pd.DataFrame(rows)
        st.dataframe(df, height=500, width='stretch')
    else:
        st.info("No pending EdgeFinder signals in the latest fetch.")

    if resolved:
        st.divider()
        st.subheader("Recently resolved (from EdgeFinder)")
        rrows = []
        for s in resolved[:20]:
            rrows.append({
                "Status":      s.get("status", "—").upper(),
                "Signal type": (s.get("signal_type") or "?").upper(),
                "YES @":       _f4(s.get("yes_price")),
                "Question":    (s.get("question") or "")[:80],
                "Detected":    _ago(s.get("detected_at") or s.get("date_added", "")),
                "Resolved":    _ago(s.get("resolved_at") or ""),
            })
        st.dataframe(pd.DataFrame(rrows), height=300, width='stretch')


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — Open Positions
# ─────────────────────────────────────────────────────────────────────────────

with tab_pos:
    st.subheader("Open paper positions")

    positions = db.get_open_positions()
    if not positions:
        st.info("No open positions. Click **Run cycle now** in the sidebar to evaluate the latest EdgeFinder signals.")
    else:
        for pos in positions:
            head = f"#{pos['id']} · {pos.get('side', 'NO')} · {(pos.get('question') or '')[:80]}"
            with st.expander(head, expanded=False):
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Entry",     _f4(pos["entry_price"]))
                c2.metric("Current",   _f4(pos.get("current_price") or pos["entry_price"]))
                c3.metric("Shares",    _f4(pos["size"]))
                c4.metric("Unrealized P&L",
                          _usd(pos.get("unrealized_pnl") or 0, digits=4),
                          delta=_usd(pos.get("unrealized_pnl") or 0, digits=4))
                c5.metric("Stop @ YES", _f4(pos.get("stop_loss_price")))

                meta = []
                if pos.get("edgefinder_signal_id"):
                    meta.append(f"EF id: `{pos['edgefinder_signal_id'][:8]}`")
                if pos.get("signal_type"):
                    meta.append(f"Type: `{pos['signal_type']}`")
                if pos.get("pattern"):
                    meta.append(f"Pattern: `{pos['pattern']}`")
                meta.append(f"Opened: `{(pos.get('opened_at') or '')[:19]}`")
                st.caption(" · ".join(meta))

                # ── Exit-now estimate (walks the live bid book) ──────────────
                cost_basis = float(pos["entry_price"]) * float(pos["size"])
                est = estimate_sell_proceeds(pos.get("token_id", ""), float(pos["size"]))
                mid = float(pos.get("current_price") or pos["entry_price"])
                if est is None:
                    st.caption("Exit estimate: order book unavailable.")
                    suggested_exit = mid
                else:
                    realized_if_exit = est["proceeds"] - cost_basis
                    settle_proceeds  = float(pos["size"]) * 1.0
                    left_on_table    = settle_proceeds - est["proceeds"]
                    # Default to the BEST achievable close — the highest of the live
                    # mid and the top-of-book bid — not the walked-book average (which
                    # is pessimistic on thin markets and undersells winners).
                    suggested_exit   = max(
                        c for c in (mid, est.get("best_bid"), est.get("avg_price"))
                        if c and c > 0
                    )

                    eb1, eb2, eb3, eb4 = st.columns(4)
                    eb1.metric("Exit now (proceeds)", _usd(est["proceeds"]))
                    eb2.metric("Avg fill",            _f4(est["avg_price"]))
                    eb3.metric("Realized if exit",
                               _usd(realized_if_exit),
                               delta=_usd(realized_if_exit))
                    eb4.metric("Left on table vs $1 settle",
                               _usd(left_on_table),
                               delta=f"-{_usd(left_on_table)}",
                               delta_color="inverse")

                    fill_note = "fully fills" if est["fully_filled"] else (
                        f"PARTIAL — only {est['shares_filled']:.2f} of {float(pos['size']):.2f} shares "
                        f"({est['depth_available']:.2f} resting on bids)"
                    )
                    st.caption(
                        f"Best bid `{est['best_bid']:.4f}` · book {fill_note} · "
                        f"walks {est['levels_consumed']} level(s) · "
                        f"cost basis `${cost_basis:.2f}`"
                    )

                ec1, ec2 = st.columns([1, 3])
                exit_px = ec1.number_input(
                    "Exit @ NO",
                    min_value=0.001, max_value=0.999,
                    value=min(0.999, max(0.001, float(suggested_exit))),
                    step=0.001, format="%.4f",
                    key=f"exit_{pos['id']}",
                    help="Defaults to the best achievable close — the higher of the live mid and top-of-book bid.",
                )
                if ec2.button(f"Close position #{pos['id']}", key=f"close_{pos['id']}"):
                    r = close_position_by_id(pos["id"], exit_px)
                    if r:
                        st.success(f"Closed #{pos['id']} · P&L {_usd(r['realized_pnl'], digits=4)}")
                        st.rerun()

        st.divider()
        if st.button("Close all positions (at mid)"):
            closed = close_all_positions_at_mid()
            st.success(f"Closed {len(closed)} positions")
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# TAB 3 — Performance
# ─────────────────────────────────────────────────────────────────────────────

with tab_perf:
    st.subheader("World Event Bot vs EdgeFinder published")

    pc1, pc2 = st.columns(2)
    with pc1:
        st.markdown("**Our paper performance**")
        st.metric("Win rate",      _pct(p["win_rate"]))
        st.metric("Closed trades", f"{p['closed']}")
        st.metric("W / L",          f"{p['wins']} / {p['losses']}")
        st.metric("Realized P&L",   _usd(p["realized"]))
        st.metric("Total return",   _pct(p["return_pct"]))

    with pc2:
        st.markdown("**EdgeFinder published track record**")
        st.metric("Overall WR",  _pct(ef_overall.get("win_rate", 0)))
        st.metric("Total signals", f"{ef_overall.get('total_signals', 0)}")
        st.metric("W / L",        f"{ef_overall.get('wins', 0)} / {ef_overall.get('losses', 0)}")
        st.metric("ML WR",       _pct(ef_ml.get("win_rate", 0)))
        st.metric("Crash WR",    _pct(ef_crash.get("win_rate", 0)))

    st.divider()
    st.subheader("Cumulative realized P&L")
    closed = db.get_closed_trades()
    if closed:
        df = pd.DataFrame(closed)
        if "closed_at" in df.columns and "realized_pnl" in df.columns:
            df = df.sort_values("closed_at")
            df["cumulative_pnl"] = df["realized_pnl"].cumsum()
            st.line_chart(df.set_index("closed_at")["cumulative_pnl"])
    else:
        st.info("No closed trades yet.")

    st.divider()
    st.subheader("EdgeFinder published WR over time")
    snaps = []
    import sqlite3
    try:
        with sqlite3.connect(config.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT snapshot_at, overall_win_rate, ml_win_rate, crash_win_rate "
                "FROM edgefinder_stats_snapshots ORDER BY snapshot_at"
            ).fetchall()
            snaps = [{"t": r[0], "overall": r[1], "ml": r[2], "crash": r[3]} for r in rows]
    except sqlite3.OperationalError:
        st.warning("Run migration 001 to enable the stats-snapshot trend chart.")

    if snaps:
        sdf = pd.DataFrame(snaps).set_index("t")
        st.line_chart(sdf)
    else:
        st.caption("No snapshots yet — they accumulate as `fetch_stats()` is called.")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 4 — Our Engine (Phase A: rule-based replica + snapshot accumulation)
# ─────────────────────────────────────────────────────────────────────────────

with tab_ours:
    st.subheader("Our own engine — Phase A (rule-based replica)")
    st.caption(
        "Snapshots every scanned Polymarket market into `market_snapshots`. "
        "Fires `our_signals` when our deterministic rules match (see `_config/our-engine-rules.md`). "
        "ML model (Phase C) is gated on ~200 resolved EdgeFinder signals as training labels."
    )

    # Quick health: are the tables present?
    import sqlite3 as _sql
    _migration_ok = True
    try:
        with _sql.connect(config.DB_PATH) as _c:
            _c.execute("SELECT 1 FROM market_snapshots LIMIT 1")
            _c.execute("SELECT 1 FROM our_signals LIMIT 1")
    except _sql.OperationalError:
        _migration_ok = False

    if not _migration_ok:
        st.error(
            "Migration 002 has not been run. From a PowerShell with the venv active:\n\n"
            "    python -m _migrations.002_own_engine_foundation\n\n"
            "Then refresh this tab."
        )
    else:
        # ── Counters ─────────────────────────────────────────────────────────
        with _sql.connect(config.DB_PATH) as _c:
            _c.row_factory = _sql.Row
            n_snaps   = _c.execute("SELECT COUNT(*) AS n FROM market_snapshots").fetchone()["n"]
            n_markets = _c.execute("SELECT COUNT(DISTINCT token_id) AS n FROM market_snapshots").fetchone()["n"]
            oldest    = _c.execute("SELECT MIN(captured_at) AS t FROM market_snapshots").fetchone()["t"]
            newest    = _c.execute("SELECT MAX(captured_at) AS t FROM market_snapshots").fetchone()["t"]
            n_our     = _c.execute("SELECT COUNT(*) AS n FROM our_signals").fetchone()["n"]
            n_ef_resolved = _c.execute(
                "SELECT COUNT(*) AS n FROM edgefinder_signals WHERE status IN ('win','loss')"
            ).fetchone()["n"]

        with _sql.connect(config.DB_PATH) as _c:
            _c.row_factory = _sql.Row
            n_shadow = _c.execute(
                "SELECT COUNT(*) AS n FROM our_signals WHERE signal_kind='rule_v1_shadow'"
            ).fetchone()["n"]
            n_crypto_binary = _c.execute(
                "SELECT COUNT(*) AS n FROM our_signals WHERE pattern='crypto_binary'"
            ).fetchone()["n"]

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Snapshots",        f"{n_snaps:,}")
        c2.metric("Unique markets",   f"{n_markets:,}")
        c3.metric("Our signals (live rules)", f"{n_our - n_shadow:,}")
        c4.metric("Shadow signals",   f"{n_shadow:,}",
                  delta=f"crypto_binary: {n_crypto_binary}",
                  delta_color="off",
                  help="Shadow patterns are detected but NEVER paper-traded. They accumulate evidence before promotion.")
        c5.metric("EF resolved (labels)", f"{n_ef_resolved:,}")
        c6.metric("History span",
                  _ago(oldest) if oldest else "—",
                  delta=("last: " + _ago(newest)) if newest else None)

        # Phase progress
        days_history = 0.0
        if oldest and newest:
            try:
                d0 = datetime.fromisoformat(oldest.replace("Z", ""))
                d1 = datetime.fromisoformat(newest.replace("Z", ""))
                days_history = (d1 - d0).total_seconds() / 86400.0
            except Exception:
                pass

        pp1, pp2 = st.columns(2)
        with pp1:
            st.markdown("**Crash detection readiness**")
            st.progress(min(1.0, days_history / 7.0),
                        text=f"{days_history:.1f} / 7 days of history (need 7 for 7-day crash check)")
        with pp2:
            st.markdown("**ML training readiness (Phase C)**")
            st.progress(min(1.0, n_ef_resolved / 200.0),
                        text=f"{n_ef_resolved} / 200 resolved EF signals (training threshold)")

        st.divider()

        # ── Recent our_signals ───────────────────────────────────────────────
        st.markdown("**Recent rule-based signals**")
        with _sql.connect(config.DB_PATH) as _c:
            _c.row_factory = _sql.Row
            our_rows = _c.execute(
                "SELECT * FROM our_signals ORDER BY detected_at DESC LIMIT 50"
            ).fetchall()

        if our_rows:
            data = []
            for r in our_rows:
                r = dict(r)
                kind = r.get("signal_kind", "")
                pattern = r.get("pattern", "")
                pattern_display = (
                    f"🔍 {pattern} (SHADOW)" if kind == "rule_v1_shadow" else pattern
                )
                data.append({
                    "Detected":  _ago(r.get("detected_at", "")),
                    "Pattern":   pattern_display,
                    "YES @":     _f4(r.get("yes_price")),
                    "Conf":      _f4(r.get("confidence")),
                    "Status":    r.get("status", ""),
                    "Agreement": r.get("agreement", "—"),
                    "Question":  (r.get("question") or "")[:80],
                })
            st.dataframe(pd.DataFrame(data), height=400, width='stretch')
            st.caption("🔍 SHADOW = pattern detected but **never** paper-traded. Used to validate the rule before promotion.")

            # Agreement stats
            agree_n   = sum(1 for r in our_rows if dict(r).get("agreement") == "agree")
            silent_n  = sum(1 for r in our_rows if dict(r).get("agreement") == "edgefinder_silent")
            ag1, ag2, ag3 = st.columns(3)
            ag1.metric("Agree with EF",     f"{agree_n}")
            ag2.metric("EF silent (only us)", f"{silent_n}")
            ag3.metric("Total ours",        f"{len(our_rows)}")
        else:
            st.info(
                "No rule-based signals fired yet. Possible reasons:\n\n"
                "• We've only just started — markets haven't been scanned yet (try Run cycle now).\n"
                "• Crash detection needs 7 days of history; cold-start can only fire `structural` signals at YES ≥ 0.45.\n"
                "• None of the top-100 Polymarket events match our band + volume + spread + TTR gates.\n\n"
                "Once snapshots accumulate, more patterns will become detectable."
            )

        st.divider()

        # ── Path A calibrator status ─────────────────────────────────────────
        st.markdown("**Path A — EdgeFinder calibrator (sharper Kelly p)**")
        try:
            from ml.calibrator import model_info as _calib_info
            info = _calib_info()
        except Exception as _e:
            info = {"loaded": False, "error": str(_e)}

        if not info.get("loaded"):
            st.info(
                "No calibrator trained yet. With ~298 resolved EF signals already available via the API, "
                "you can train now:\n\n"
                "```powershell\n"
                "pip install scikit-learn   # one-time\n"
                "python -m ml.calibrator train\n"
                "```\n\n"
                "Until trained, Kelly sizing uses refined static rates (Momentum/Extreme split)."
            )
        else:
            ci1, ci2, ci3, ci4 = st.columns(4)
            ci1.metric("Promoted?", "YES" if info.get("promoted") else "no")
            ci2.metric("AUC", _f4(info.get("holdout_auc")))
            ci3.metric("ECE", _f4(info.get("holdout_ece")))
            ci4.metric("Log-loss vs base",
                       _f4(info.get("holdout_logloss")),
                       delta=_f4((info.get("baseline_logloss") or 0) - (info.get("holdout_logloss") or 0)))
            st.caption(
                f"Trained at {info.get('trained_at', '?')} · "
                f"train={info.get('n_train')}  val={info.get('n_val')}  test={info.get('n_test')}"
            )

            # Retrain schedule + manual button
            try:
                from ml.calibrator import (
                    next_retrain_eta,
                    CALIBRATOR_RETRAIN_DAYS,
                    CALIBRATOR_MIN_NEW_RESOLVED,
                    maybe_retrain as _retrain_fn,
                )
                eta = next_retrain_eta()
                rt1, rt2 = st.columns([3, 1])
                with rt1:
                    if eta is not None and eta > 0:
                        st.caption(
                            f"Auto-retrain in **{eta:.1f} days** (cadence: every {CALIBRATOR_RETRAIN_DAYS:.0f}d, "
                            f"needs ≥ {CALIBRATOR_MIN_NEW_RESOLVED} new resolved). "
                            f"Runs at end of every cycle."
                        )
                    else:
                        st.caption(
                            f"**Eligible to retrain on the next cycle** (cadence elapsed). "
                            f"Will only fire if ≥ {CALIBRATOR_MIN_NEW_RESOLVED} new resolved signals exist."
                        )
                with rt2:
                    if st.button("Retrain now", width='content'):
                        with st.spinner("Retraining calibrator..."):
                            result = _retrain_fn(force=True)
                        if result is None:
                            st.warning("Retrain failed — check Bot Logs.")
                        else:
                            st.success(f"Retrained. Promoted: {result.get('promoted')}")
                        st.cache_data.clear()
                        st.rerun()
            except Exception as _e:
                st.caption(f"Schedule info unavailable: {_e}")

            if not info.get("promoted"):
                st.warning(f"Not promoted: {info.get('promotion_reason')}")
            if info.get("coefs"):
                st.markdown("**Feature coefficients** (log-odds, positive = more likely to win):")
                coef_df = pd.DataFrame([
                    {"Feature": k, "Coef": v} for k, v in (info.get("coefs") or {}).items()
                ])
                st.dataframe(coef_df, width='content')

        st.divider()

        # ── Path B status (independent model from snapshots) ─────────────────
        st.markdown("**Path B — independent model from market_snapshots (long-term)**")
        st.caption(
            "Future: train a model that ranks Polymarket markets independently of EdgeFinder. "
            "Requires accumulated `market_snapshots` (started 2026-05-27) + ML training pipeline (skeleton at `ml/train.py`)."
        )
        st.progress(
            min(1.0, n_snaps / 50000.0),
            text=f"{n_snaps:,} snapshots / 50,000 target (~1 month of cycles)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TAB 5 — Sizing Calculator
# ─────────────────────────────────────────────────────────────────────────────

with tab_calc:
    st.subheader("Sizing calculator — fractional Kelly")
    st.caption("Validate Kelly math against the lessons (M69 WTI, M72 BTC) or sanity-check a pending signal.")

    cc1, cc2, cc3 = st.columns(3)
    yes_in   = cc1.slider("YES price",         min_value=0.05, max_value=0.95, value=0.395, step=0.005)
    p_in     = cc2.slider("Win probability p", min_value=0.30, max_value=0.95, value=0.707, step=0.001)
    bank_in  = cc3.number_input("Bankroll ($)", min_value=10.0,  value=float(p["cash"]) or config.STARTING_BALANCE, step=10.0)

    cc4, cc5, cc6 = st.columns(3)
    frac_in  = cc4.slider("Kelly fraction",       min_value=0.10, max_value=1.00, value=float(config.KELLY_FRACTION), step=0.05)
    cap_in   = cc5.slider("Cap (% of bankroll)",  min_value=0.01, max_value=0.50, value=float(config.MAX_BANKROLL_PCT_PER_TRADE), step=0.01)
    fee_in   = cc6.slider("Fee rate",             min_value=0.0,  max_value=0.10, value=float(config.POLYMARKET_FEE_RATE), step=0.001, format="%.3f")

    out = sizing.size_no_bet(
        yes_price=yes_in,
        win_probability=p_in,
        bankroll=bank_in,
        fee_rate=fee_in,
        kelly_fraction=frac_in,
        cap_pct=cap_in,
    )

    o1, o2, o3, o4 = st.columns(4)
    o1.metric("Full Kelly f*",     _pct(out.full_kelly_pct * 100, digits=2))
    o2.metric("Fractional",        _pct(out.fractional_kelly_pct * 100, digits=2))
    o3.metric("Capped",            _pct(out.capped_pct * 100, digits=2))
    o4.metric("Dollar size",       _usd(sizing.dollar_size(out, bank_in)))

    o5, o6, o7, o8 = st.columns(4)
    o5.metric("NO cost",           _f4(out.no_cost))
    o6.metric("Net odds b",        _f4(out.b))
    o7.metric("EV per $1",         _f4(out.ev_per_dollar))
    o8.metric("Refuse?",           "YES" if out.refuse else "no")

    if out.refuse:
        st.error(f"Refused: {out.refuse_reason}")
    else:
        st.success("All gates pass — this size would be allowed.")

    st.caption(
        "Verify: at YES=0.395, p=0.65, fee=0, frac=1.0, cap=1.0 → full Kelly should be ~11.5% (M69 WTI). "
        "At YES=0.325, p=0.72, fee=0, frac=1.0, cap=1.0 → ~13.8% (M72 BTC)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# TAB 6 — Bot Logs
# ─────────────────────────────────────────────────────────────────────────────

with tab_logs:
    st.subheader("Bot logs")

    fc, lc, _ = st.columns([2, 1, 3])
    level = fc.selectbox("Level", ["ALL", "INFO", "WARNING", "ERROR"])
    limit = lc.number_input("Last N", min_value=20, max_value=500, value=150)

    logs = db.get_logs(limit=int(limit))
    if level != "ALL":
        logs = [l for l in logs if l.get("level") == level]

    if logs:
        ldf = pd.DataFrame(logs)[["created_at", "level", "message"]]
        ldf.columns = ["Time (UTC)", "Level", "Message"]

        def _row_color(row):
            if row["Level"] == "ERROR":   return ["background-color: #3d0000"] * 3
            if row["Level"] == "WARNING": return ["background-color: #2d2000"] * 3
            return [""] * 3
        st.dataframe(ldf.style.apply(_row_color, axis=1), height=550, width='stretch')
    else:
        st.info("No logs yet.")


# ─────────────────────────────────────────────────────────────────────────────
# Auto-cycle engine
# ─────────────────────────────────────────────────────────────────────────────

if st.session_state.auto_cycle:
    # Poll every EDGEFINDER_POLL_INTERVAL_S
    now = _utcnow()
    if (st.session_state.next_cycle is None) or (now >= st.session_state.next_cycle):
        with st.sidebar.status("Cycling..."):
            res = _run_cycle()
            st.sidebar.caption(
                f"Auto-cycle: {res['signals']} new, {res['opened']} opened, {res['closed']} closed"
            )
        from datetime import timedelta
        st.session_state.next_cycle = now + timedelta(seconds=int(config.EDGEFINDER_POLL_INTERVAL_S))
    secs_left = max(1, int((st.session_state.next_cycle - _utcnow()).total_seconds()))
    time.sleep(min(secs_left, 30))
    st.rerun()
