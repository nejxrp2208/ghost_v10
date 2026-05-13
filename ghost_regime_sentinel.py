"""
ghost_regime_sentinel.py — vol-regime data collector (OBSERVE MODE).

Runs as a background agent. Every 5 minutes, it captures the current market
"regime" (volatility, momentum, brain state) and logs one row to
agent_reports.db -> regime_log.

After 14 days of data, we backtest: for each candidate vol threshold, compute
the hypothetical net P&L if we'd skipped trades fired during low-vol periods.
The threshold that maximizes net P&L (vs always-trade) is a candidate halt
threshold — but only activated if it beats always-trade by ≥$50/day.

This agent NEVER halts the bot. It only records. Activation happens by
upgrading scanner code AFTER the backtest justifies it.

Run:
    python ghost_regime_sentinel.py
"""
import os, sys, time, json, sqlite3
import urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# .env
try:
    from dotenv import load_dotenv
    SD = os.path.dirname(os.path.abspath(__file__))
    ep = os.path.join(SD, ".env")
    if os.path.exists(ep):
        load_dotenv(ep, override=True)
except ImportError:
    SD = os.path.dirname(os.path.abspath(__file__))

# ── Config ────────────────────────────────────────────────────────────────────
BINANCE_REST = os.getenv("BINANCE_REST", "https://api.binance.com/api/v3")
REPORTS_DB   = os.path.join(SD, "agent_reports.db")
BRAIN_FILE   = os.path.join(SD, "ghost_brain.json")
POLL_S       = int(os.getenv("REGIME_POLL_SEC", "300"))   # 5 min
N_CANDLES    = int(os.getenv("REGIME_N_CANDLES", "12"))   # 12 × 5m = 1 hour

# Candidate halt thresholds (decimal, e.g. 0.0005 = 0.05%)
CANDIDATE_THRESHOLDS = [0.0002, 0.0005, 0.0008, 0.0012, 0.0018]

COINS = ("BTC", "ETH")
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


# ── Schema ────────────────────────────────────────────────────────────────────
def init_schema():
    con = sqlite3.connect(REPORTS_DB, timeout=60)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS regime_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            hour_et         INTEGER,
            btc_vol_5m      REAL,
            eth_vol_5m      REAL,
            btc_1h_move     REAL,
            eth_1h_move     REAL,
            btc_brain_state TEXT,
            eth_brain_state TEXT,
            btc_brain_score INTEGER,
            eth_brain_score INTEGER,
            would_halt_002  INTEGER,
            would_halt_005  INTEGER,
            would_halt_008  INTEGER,
            would_halt_012  INTEGER,
            would_halt_018  INTEGER,
            n_candles_used  INTEGER,
            error           TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_regime_log_ts ON regime_log(ts)")
    con.commit()
    con.close()


# ── Data fetchers ─────────────────────────────────────────────────────────────
def fetch_5m_klines(symbol: str, n: int = 12):
    """Fetch the n most recent 5m klines for symbol. Returns list of (open, close)."""
    url = f"{BINANCE_REST}/klines?symbol={symbol}&interval=5m&limit={n}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        # Each kline: [open_time, open, high, low, close, volume, close_time, ...]
        return [(float(k[1]), float(k[4])) for k in data]
    except Exception as e:
        print(f"[regime] fetch {symbol} failed: {e}", flush=True)
        return None


def read_brain(coin: str):
    """Returns (state, score) tuple from ghost_brain.json. (None, None) if unavailable."""
    if not os.path.exists(BRAIN_FILE):
        return None, None
    try:
        with open(BRAIN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        c = data.get("coins", {}).get(coin, {})
        return c.get("state"), c.get("score")
    except Exception:
        return None, None


# ── Feature computation ───────────────────────────────────────────────────────
def compute_features(klines):
    """
    Given list of (open, close) tuples in chronological order, compute:
      - realized vol = mean(|close - open| / open)
      - 1h cumulative move = |last_close - first_open| / first_open
    Returns (vol, move) or (None, None) if insufficient data.
    """
    if not klines or len(klines) < 2:
        return None, None
    moves = [abs(c - o) / o for (o, c) in klines if o > 0]
    if not moves:
        return None, None
    realized_vol = sum(moves) / len(moves)
    first_open = klines[0][0]
    last_close = klines[-1][1]
    cum_move = abs(last_close - first_open) / first_open if first_open > 0 else 0.0
    return realized_vol, cum_move


# ── One observation cycle ─────────────────────────────────────────────────────
def observe_once():
    now = datetime.now(timezone.utc)
    et_hour = (now - timedelta(hours=4)).hour

    btc_klines = fetch_5m_klines(SYMBOLS["BTC"], N_CANDLES)
    eth_klines = fetch_5m_klines(SYMBOLS["ETH"], N_CANDLES)

    btc_vol, btc_move = compute_features(btc_klines) if btc_klines else (None, None)
    eth_vol, eth_move = compute_features(eth_klines) if eth_klines else (None, None)

    btc_state, btc_score = read_brain("BTC")
    eth_state, eth_score = read_brain("ETH")

    # Halt-flag pre-computation for each candidate threshold.
    # We say "would halt" if BOTH BTC and ETH vol are below the threshold —
    # because trend-snipe trades on either coin so we only halt when neither
    # coin has tradeable vol.
    halt_flags = {}
    for thr in CANDIDATE_THRESHOLDS:
        if btc_vol is None or eth_vol is None:
            halt_flags[thr] = None
        else:
            halt_flags[thr] = 1 if (btc_vol < thr and eth_vol < thr) else 0

    # n_candles actually used (for diagnostic transparency)
    n_used = max(
        len(btc_klines) if btc_klines else 0,
        len(eth_klines) if eth_klines else 0,
    )

    # Write to DB
    con = sqlite3.connect(REPORTS_DB, timeout=60)
    con.execute("""
        INSERT INTO regime_log (
            ts, hour_et,
            btc_vol_5m, eth_vol_5m,
            btc_1h_move, eth_1h_move,
            btc_brain_state, eth_brain_state,
            btc_brain_score, eth_brain_score,
            would_halt_002, would_halt_005, would_halt_008,
            would_halt_012, would_halt_018,
            n_candles_used, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        now.isoformat(), et_hour,
        btc_vol, eth_vol,
        btc_move, eth_move,
        btc_state, eth_state,
        btc_score, eth_score,
        halt_flags.get(0.0002), halt_flags.get(0.0005),
        halt_flags.get(0.0008), halt_flags.get(0.0012),
        halt_flags.get(0.0018),
        n_used,
        None if (btc_klines and eth_klines) else "partial fetch",
    ))
    con.commit()
    con.close()

    # Console log
    btc_vol_pct = f"{btc_vol*100:.3f}%" if btc_vol is not None else "  ?  "
    eth_vol_pct = f"{eth_vol*100:.3f}%" if eth_vol is not None else "  ?  "
    flags_str = " ".join(
        f"<{int(t*1e4)}={'Y' if halt_flags.get(t) else 'n' if halt_flags.get(t) == 0 else '?'}"
        for t in CANDIDATE_THRESHOLDS
    )
    print(f"[regime] {now.strftime('%H:%M:%S')} ET={et_hour:02d}  "
          f"BTC vol={btc_vol_pct} brain={btc_state}/{btc_score}  "
          f"ETH vol={eth_vol_pct} brain={eth_state}/{eth_score}  "
          f"halts:{flags_str}", flush=True)


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    init_schema()
    print(f"[regime] starting — poll={POLL_S}s ({POLL_S//60}m)  "
          f"candles={N_CANDLES}  thresholds={CANDIDATE_THRESHOLDS}", flush=True)
    print(f"[regime] DB: {REPORTS_DB}", flush=True)
    print(f"[regime] OBSERVE MODE — collects data, never halts the bot", flush=True)

    while True:
        try:
            observe_once()
        except Exception as e:
            print(f"[regime] cycle error: {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[regime] stopped by user", flush=True)
        sys.exit(0)
