"""
GHOST LATTICE v10 — Polymarket Crypto Scanner (SPECTER + WRAITH)
================================================================
Live on Polygon/Polymarket. PAPER_TRADE=false = real money.
Place in: C:\\Users\\ooage\\Desktop\\GHOST LATTICE\\

2-Tier Strategy (CoinDirEngine + oracle-lag edge):
────────────────────────────────────────────────────────────────
T1-WRAITH   Contrarian tier — fires AGAINST Binance deviation
            Target: 15-min & hourly Up/Down markets, final window
            Entry: $0.03-0.18 | Size: $6.66 flat | Avg win: ~$20

T2-SPECTER  Oracle-lag tier — fires WITH Binance deviation
            Target: 5-min Up/Down markets, Chainlink stale window
            Entry: $0.04-0.09 | Size: $6.66 flat | Lotto wins: $30-$120+

Edge: Chainlink updates every ~5 min. Binance is real-time.
      Buy the mispriced side before the oracle catches up.

Config: .env (root) | DB: GHOST_LATTICE.db | Bot: GHOST_LATTICE_dashboard.py
"""

import asyncio
import aiohttp
import json
import time
import sqlite3
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from collections import deque

# ── SSL fix (stdlib level — covers requests, httpx, py_clob_client_v2, all) ──
import ssl, urllib3
ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── GHOST FILL GUARD ─────────────────────────────────────────────────────────
try:
    from GHOST_LATTICE_fill_guard import GhostFillGuard
    _GHOST_GUARD_AVAILABLE = True
except ImportError:
    _GHOST_GUARD_AVAILABLE = False

# ─── WINDOWS FIX ──────────────────────────────────────────────────────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ─── LOAD .ENV ────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    # Try multiple locations (your existing UnifiedBot .env or local)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_candidates = [
        os.path.join(script_dir, ".env"),
        os.path.join(script_dir, "..", ".env"),
        r"C:\Users\ramos\OneDrive\Desktop\CryptoGhostScanner\.env",
        r"C:\Users\ramos\Desktop\CryptoGhostScanner\.env",
        os.path.join(os.path.expanduser("~"), "Desktop", ".env"),
        r"C:\Users\ramos\OneDrive\Desktop\UnifiedBot\.env",
    ]
    for ep in env_candidates:
        if os.path.exists(ep):
            load_dotenv(ep, override=True)
            print(f"[ENV] Loaded: {ep}")
            break
except ImportError:
    print("[WARN] python-dotenv not installed — reading env vars directly")

# ─── CLOB CLIENT ──────────────────────────────────────────────────────────────
try:
    from py_clob_client_v2 import (
        ClobClient, MarketOrderArgsV2 as MarketOrderArgs,
        OrderType, PartialCreateOrderOptions,
    )
    from py_clob_client_v2.order_builder.constants import BUY
except ImportError:
    print("ERROR: py-clob-client-v2 not installed.")
    print("Run: pip install py-clob-client-v2")
    sys.exit(1)

# ─── TELEGRAM ALERTS (optional — silent if creds missing) ─────────────────────
try:
    from telegram_alerts import send_alert as _tg_send
    TELEGRAM_ENABLED = True
except Exception as _tg_err:
    TELEGRAM_ENABLED = False
    async def _tg_send(msg: str): pass

# ─── CONFIG (all values come from .env — edit that file, not this one) ────────
PRIVATE_KEY     = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS  = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
POLYMARKET_HOST   = "https://clob.polymarket.com"
GAMMA_API         = "https://gamma-api.polymarket.com"
DATA_API          = "https://data-api.polymarket.com"

# ── Whale / smart-wallet discovery ───────────────────────────────────────────
# Comma-separated list of Polymarket proxy wallet addresses to watch for
# market discovery. Their recent trades are used to find active markets and
# apply a mild confidence/priority boost when direction matches our signal.
# @ohanism: proven active discovery wallet (used in marketghost.py)
# Add more wallets to WHALE_WALLETS in .env (comma-separated).
_WHALE_WALLETS_DEFAULT = "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30"
WHALE_WALLETS: list = [
    w.strip().lower()
    for w in os.getenv("WHALE_WALLETS", _WHALE_WALLETS_DEFAULT).split(",")
    if w.strip()
]
# How many seconds a whale trade stays "fresh" for signal boosting
WHALE_SIGNAL_TTL      = float(os.getenv("WHALE_SIGNAL_TTL", "120"))
# Priority boost multiplier when a whale recently traded this market (same direction)
WHALE_PRIORITY_BOOST  = float(os.getenv("WHALE_PRIORITY_BOOST", "1.20"))
# Confidence boost when whale direction matches ours
WHALE_CONF_BOOST      = float(os.getenv("WHALE_CONF_BOOST", "0.03"))
# In-memory cache: condition_id -> {"direction": "up"/"down"/"both", "ts": float, "wallet": str}
_whale_signals: dict = {}
CLOB_API          = "https://clob.polymarket.com"
# Skip first 1000 old markets in CLOB API (they are old resolved sports markets)
# Recent April 2026 crypto markets start appearing after offset 1000
CLOB_START_CURSOR = "MTAwMA=="
BINANCE_REST    = "https://api.binance.com/api/v3"   # .com = 10-20ms vs .us 150-400ms
BINANCE_WS_URL  = (
    "wss://stream.binance.com:9443/stream?streams="  # .com: lower latency, stable WS
    "btcusdt@ticker/ethusdt@ticker/bnbusdt@ticker"
    "/xrpusdt@ticker/solusdt@ticker"  # XRP + SOL live feed added
)

# ── Trade sizing per tier ──
T1_SIZE  = float(os.getenv("T1_SIZE_USDC",  "10.0"))  # 4hr contrarian
T2_SIZE  = float(os.getenv("T2_SIZE_USDC",  "10.0"))  # Hourly Up/Down

# ── Per-coin T2 sizing overrides (data-backed) ──
# All 5 coins active: BTC / ETH / BNB / SOL / XRP (re-enabled 2026-05-10)
T2_SIZE_PER_COIN = {
    "BNB": float(os.getenv("T2_SIZE_BNB", "3.0")),  # flat $3 — uniform sizing for dual-fire data collection
    "BTC": float(os.getenv("T2_SIZE_BTC", "3.0")),  # flat $3 — uniform sizing for dual-fire data collection
    "SOL": float(os.getenv("T2_SIZE_SOL", "3.0")),  # flat $3 — uniform sizing for dual-fire data collection
    "XRP": float(os.getenv("T2_SIZE_XRP", "3.0")),  # flat $3 — uniform sizing for dual-fire data collection
    "ETH": float(os.getenv("T2_SIZE_ETH", "3.0")),  # flat $3 — uniform sizing for dual-fire data collection
}

# ── Hour-of-day T2 size multiplier (Ghost Bot forensics, 2026-05-10) ─────────
# Scale actual dollar bet size during statistically strong ET hours.
# Uses the same ET scale as HOUR_BOOST_HOURS_ET. OFF by default until
# we have 30+ live T2 fires to validate our own hour distribution.
# Example .env entries: T2_SIZE_HOUR_MULT_23=1.5  T2_SIZE_HOUR_MULT_21=1.3
# Ghost Bot finding: UTC hour 8 = 85% of their PnL. Our best = 23/21/17 ET.
T2_SIZE_HOUR_MULTS: dict = {}
for _h in range(24):
    _hm = os.getenv(f"T2_SIZE_HOUR_MULT_{_h}")
    if _hm:
        try:
            T2_SIZE_HOUR_MULTS[_h] = float(_hm)
        except ValueError:
            pass
if T2_SIZE_HOUR_MULTS:
    print(f"[ENV] T2_SIZE_HOUR_MULTS(ET) = {T2_SIZE_HOUR_MULTS}")

# ── Entry price ceilings per tier ──
T1_MAX_ENTRY = float(os.getenv("T1_MAX_ENTRY", "0.20"))  # 4hr contrarian ceiling
T2_MAX_ENTRY = float(os.getenv("T2_MAX_ENTRY", "0.08"))
T2_MIN_ENTRY = float(os.getenv("T2_MIN_ENTRY", "0.01"))  # 1¢ floor: best payout ratio
T1_MIN_ENTRY = float(os.getenv("T1_MIN_ENTRY", "0.12"))  # 4hr contrarian floor
T4_MAX_ENTRY = float(os.getenv("T4_MAX_ENTRY", "0.20"))  # daily market ceiling
T4_MIN_ENTRY = float(os.getenv("T4_MIN_ENTRY", "0.10"))  # daily market floor

# ── 5-minute market overrides (ported from CryptoGhostScanner) ───────────────
# 5-min markets (title like "1:20PM-1:25PM") are ultra-short — stricter gates.
# Ceiling tighter (10¢ vs 15¢), certainty higher (90%), time window last 45s.
T2_5M_MAX_ENTRY  = float(os.getenv("T2_5M_MAX_ENTRY",  "0.10"))  # 5m: max 10¢ entry
T2_5M_MIN_NO     = float(os.getenv("T2_5M_MIN_NO",     "0.90"))  # 5m: 90% certainty floor
T2_5M_WINDOW_SEC = float(os.getenv("T2_5M_WINDOW_SEC", "45"))    # 5m: fire only last 45s

# ── Dual-fire: fire BOTH sides of a market when both are cheap ──────────────
# When we hold one leg (e.g. UP at $0.01) and the opposite leg (DOWN) also
# collapses to ≤ DUAL_FIRE_MAX_ENTRY, fire the second leg immediately.
# One side MUST settle at $1.00 → guaranteed net profit regardless of outcome.
# Math at $0.01 entry, $3 size: 300×$1 payout – $3 losing leg = +$294 net.
# Math at $0.02 entry, $3 size: 150×$1 payout – $3 losing leg = +$144 net.
# Bypasses ALL trend/direction gates — direction is irrelevant when both sides win.
DUAL_FIRE_ENABLED   = os.getenv("DUAL_FIRE_ENABLED",   "true").lower() == "true"
DUAL_FIRE_MAX_ENTRY = float(os.getenv("DUAL_FIRE_MAX_ENTRY", "0.02"))

# Balance-scaled sizing: when BET_PCT > 0, sizes auto-scale with live wallet balance.
# base = balance * BET_PCT * per_coin_ratio   (per_coin_ratio = T2_SIZE_PER_COIN[coin] / T2_SIZE)
# 0.0 = disabled, use fixed T2_SIZE_* dollar amounts. MAX_BET_SIZE hard-caps each trade.
BET_PCT      = float(os.getenv("BET_PCT",      "0.0"))
MAX_BET_SIZE = float(os.getenv("MAX_BET_SIZE", "20.0"))

# ── Signal filters ──
MIN_NO_PRICE  = float(os.getenv("MIN_NO_PRICE",   "0.80"))  # certainty floor
# Liquidity coverage multiplier: required book depth as multiple of bet size.
# 1.2 = need 120% of bet size in the book (20% slippage buffer).
# Used in depth pre-check, kill filter 3, and liq_score.
LIQ_MULT      = float(os.getenv("LIQ_MULT",      "1.2"))   # e.g. 1.2 = 120% of bet size

# ── Entry time windows (minutes before market close) ──
T2_WINDOW_MIN = int(os.getenv("T2_WINDOW_MIN", "30"))    # Hourly: final 20 min

# v10.2 STALE-ASK SNIPE WINDOW
# Data from 71-trade backup: WR 53% at 0-30s left, 22% at 30-60s,
# 0-20% beyond. Firing only in the last 60 seconds doubles your edge.
T2_MAX_SECS_LEFT = int(os.getenv("T2_MAX_SECS_LEFT", "90"))   # widened 60→90s: more entries = more dual-fire setups
T2_MIN_SECS_LEFT = int(os.getenv("T2_MIN_SECS_LEFT", "5"))    # don't fire if less than 5s (won't fill)
T1_MIN_SECS_LEFT = int(os.getenv("T1_MIN_SECS_LEFT", "180"))  # 4hr contrarian: fire ≥3 min before close
T2_CANDLE_SECS       = int(os.getenv("T2_CANDLE_SECS",       "900"))   # 15-min market total duration (seconds)
CANDLE_PROGRESS_MAX  = float(os.getenv("CANDLE_PROGRESS_MAX",  "0.60")) # hard reject above 60% through candle
CANDLE_SWEET_LO      = float(os.getenv("CANDLE_SWEET_LO",      "0.20")) # bell-curve sweet spot start (data: 20%)
CANDLE_SWEET_HI      = float(os.getenv("CANDLE_SWEET_HI",      "0.50")) # bell-curve sweet spot end
T1_WINDOW_MIN = int(os.getenv("T1_WINDOW_MIN", "20"))    # 4hr contrarian: last 20 min

# ── Ghost Brain integration ───────────────────────────────────────────────────
# When enabled, reads ghost_brain.json (written by ghost_brain.py every 10s).
# Skips coins with ghost_score below GHOST_BRAIN_MIN_SCORE (25 = DORMANT cutoff).
# ghost_brain.json also carries learned_thresholds (per-coin) that override the
# global min after enough trade history accumulates (see ghost_brain.py learning loop).
GHOST_BRAIN_ENABLED   = os.getenv("GHOST_BRAIN_ENABLED",   "false").lower() == "true"
GHOST_BRAIN_MIN_SCORE = int(os.getenv("GHOST_BRAIN_MIN_SCORE", "25"))

# ── Price deviation required for Up/Down markets ──
T2_MIN_DEV = float(os.getenv("T2_MIN_DEV", "0.001"))   # hourly oracle-lag
T1_MIN_DEV = float(os.getenv("T1_MIN_DEV", "0.001"))   # 4hr contrarian

# ── Oracle discrepancy gate (T2) ──────────────────────────────────────────────
# Score = abs(binance_dev) / ask_price
# Captures both DIRECTION (dev sign vs buying direction, gated earlier) and
# MAGNITUDE (how large Binance's move is relative to what Polymarket implies).
# Example: dev=+0.15%, ask=$0.02 → score=0.075 → passes threshold 0.05
# Example: dev=+0.10%, ask=$0.03 → score=0.033 → FAILS — Binance barely moved
#          relative to the more expensive ask; weaker edge
# Set to 0 to disable (pure T2_MIN_DEV + direction gate applies instead).
ORACLE_DISC_ENABLED   = os.getenv("ORACLE_DISC_ENABLED", "false").lower() == "true"  # OFF for data collection
# Threshold raised 0.05→0.10: production data shows BTC disc≈0.109 (borderline),
# XRP disc≈0.433 (strong). 0.10 cuts weak BTC entries while keeping XRP/BNB.
ORACLE_DISC_MIN_RATIO = float(os.getenv("ORACLE_DISC_MIN_RATIO", "0.10"))
# ─────────────────────────────────────────────────────────────────────────────

# ── Signal priority ranking ───────────────────────────────────────────────────
# Each scan cycle collects ALL valid signals, ranks them by composite score,
# then fires only the top N. Weaker signals that pass all gates are skipped.
# Priority score = disc_score * no_implied
#   disc_score  = abs(dev) / ask   (Binance move size relative to market price)
#   no_implied  = 1 - ask          (certainty that the other side wins)
# High score = big Binance move + very cheap ask + high certainty = best edge.
TOP_SIGNALS_PER_CYCLE = int(os.getenv("TOP_SIGNALS_PER_CYCLE", "4"))  # raised 2→4: fire more signals per scan cycle
# ─────────────────────────────────────────────────────────────────────────────

# ── Enhanced trend detection ──────────────────────────────────────────────────
# Uses real-time tick history (stored in PriceTracker.tick_history deque).
# Three independent checks, all must pass when TREND_ENHANCED=true:
#
#  Velocity  — rate of price change per second (fractional).
#              Filters markets where price is technically above/below open but
#              has stalled — no active buying/selling pressure.
#              TREND_VELOCITY_MIN = 0.000003 → BTC must move ~$0.30/s at $100k
#
#  Momentum  — net directional score over last N ticks: (up−down)/(up+down).
#              Range −1..+1. Positive = more up-ticks than down-ticks.
#              TREND_MOMENTUM_MIN = 0.4 → at least 70% of ticks agree with direction.
#              Filters choppy oscillating prices that pass the candle-open dev check.
#
#  Volatility — mean absolute tick-to-tick fractional move over last N ticks.
#              MIN filters flat/dead markets (Binance WS stale, no real movement).
#              MAX filters spike anomalies (fat-finger, flash crash noise).
#
TREND_ENHANCED       = os.getenv("TREND_ENHANCED", "true").lower() == "true"
TREND_N_TICKS        = int(os.getenv("TREND_N_TICKS",        "10"))
TREND_VELOCITY_MIN   = float(os.getenv("TREND_VELOCITY_MIN",  "0.000003"))  # frac/sec
TREND_MOMENTUM_MIN   = float(os.getenv("TREND_MOMENTUM_MIN",  "0.4"))       # -1..+1
TREND_VOLATILITY_MIN = float(os.getenv("TREND_VOLATILITY_MIN","0.000005"))  # frac/tick
TREND_VOLATILITY_MAX = float(os.getenv("TREND_VOLATILITY_MAX","0.003"))     # frac/tick
# 15-minute Punisher filter — T2 only.
# If 15m dev opposes 5m dev AND |15m dev| >= this threshold, skip the signal.
# Lower = more signals killed (stricter). Raise to 0.003 to relax on choppy days.
DEV_15M_THRESH       = float(os.getenv("DEV_15M_THRESH", "0.0010"))  # 0.1% hardcoded → now configurable
# ─────────────────────────────────────────────────────────────────────────────

# ── Trade confidence scoring ──────────────────────────────────────────────────
# Composite 0–1 score built from four independent dimensions before each trade.
# Trades below CONFIDENCE_MIN are rejected even if all filter gates passed.
#
#  dev_score  — deviation vs per-tier minimum (5× min = 1.0)
#  liq_score  — cumulative depth vs bet size  (3× bet = 1.0)
#  disc_score — ask distance from entry ceiling (at ceiling = 0.0, near 0 = 1.0)
#  time_score — proximity to resolution        (T2: peaks at T2_MIN_SECS_LEFT)
#
# Weights sum to 1.0 by design; adjust proportions without changing CONFIDENCE_MIN.
# 6-component confidence formula. Weights sum to 1.0 by design.
# MICRO-LAG ARB weights: mismatch(0.35) + dev(0.20) + liq(0.15) + time(0.15)
#                         + price(0.10) + freshness(0.05)
# Strategy: oracle-lag sniping. mismatch = edge. time = precision. freshness = low vel.
CONFIDENCE_MIN       = float(os.getenv("CONFIDENCE_MIN",       "0.65"))

# Adaptive filter relaxation — prevents zero-fire droughts.
# If no trade fires for ADAP_DROUGHT_SECS, lower conf + mismatch thresholds.
# Restores to originals immediately when a trade fires.
ADAP_DROUGHT_SECS   = int(os.getenv("ADAP_DROUGHT_SECS",   "1800"))  # 30 min drought trigger
ADAP_CONF_RELAX     = float(os.getenv("ADAP_CONF_RELAX",    "0.05")) # lower conf_min by this
ADAP_CONF_FLOOR     = float(os.getenv("ADAP_CONF_FLOOR",    "0.55")) # never go below 0.55
ADAP_MISMATCH_RELAX = float(os.getenv("ADAP_MISMATCH_RELAX","0.05")) # lower kill_mismatch_min

# Timing + price compensator: if candle progress is in the sweet window AND
# the entry is cheap (lower portion of max_entry range), bypass low-dev and
# low-mismatch hard kills. Slight signal + right timing/price = still worth it.
TIMING_PRICE_OVERRIDE     = os.getenv("TIMING_PRICE_OVERRIDE", "true").strip().lower() in ("true","1","yes")
TIMING_PRICE_CHEAP_THRESH = float(os.getenv("TIMING_PRICE_CHEAP_THRESH", "0.75"))  # fraction of max_entry

# Module-level drought state (mutated by _adap_update / read by _adap_eff_*)
_adap_last_fire_ts: float = 0.0   # epoch time of last fired trade
_adap_in_drought:   bool  = False  # True when thresholds are relaxed
CONF_WEIGHT_DEV      = float(os.getenv("CONF_WEIGHT_DEV",      "0.20"))
CONF_WEIGHT_MISMATCH = float(os.getenv("CONF_WEIGHT_MISMATCH", "0.35"))
CONF_WEIGHT_LIQ      = float(os.getenv("CONF_WEIGHT_LIQ",      "0.15"))
CONF_WEIGHT_PRICE    = float(os.getenv("CONF_WEIGHT_PRICE",    "0.10"))
CONF_WEIGHT_TIME     = float(os.getenv("CONF_WEIGHT_TIME",     "0.15"))
CONF_WEIGHT_MOM      = float(os.getenv("CONF_WEIGHT_MOM",      "0.05"))
# Inverted mismatch: penalise large oracle gaps (already repriced), reward subtle ones.
# Backtest (133 live trades): winning disc_score mean=0.00270 vs losing=0.00912 (3.4×).
# Above cap → score = cap − (raw−cap)×PENALTY, floored at MISMATCH_FLOOR.
# 0.0 cap = disabled (legacy linear behaviour).
MISMATCH_CAP           = float(os.getenv("MISMATCH_CAP",           "0.70"))
MISMATCH_PENALTY_SCALE = float(os.getenv("MISMATCH_PENALTY_SCALE", "0.50"))
MISMATCH_FLOOR         = float(os.getenv("MISMATCH_FLOOR",         "0.15"))
# Probability-weighted, confidence-adjusted ROI rate (normalized per dollar).
# Formula: (fair_p/ask - 1) * confidence
# fair_p = model win probability (ARB engine Gaussian), NOT confidence score.
# confidence = signal quality discount [0,1].
# At fair_p=0.70, ask=0.06, conf=0.68: rate = 10.67 * 0.68 = 7.25 (passes 2.0).
# At fair_p=0.51, ask=0.06, conf=0.65: rate = 7.50 * 0.65 = 4.88 (passes 2.0).
# Threshold 2.0 = minimum 200% adj ROI rate after probability + quality discount.
MIN_EXPECTED_ROI     = float(os.getenv("MIN_EXPECTED_ROI",     "2.0"))
# ── Pre-trade signal quality hard limits ─────────────────────────────────────
KILL_MISMATCH_MIN    = float(os.getenv("KILL_MISMATCH_MIN",    "0.40"))  # weak edge: oracle barely disagrees with market
KILL_DEV_MIN         = float(os.getenv("KILL_DEV_MIN",         "0.50"))  # fake trend: deviation < 2.5x tier minimum
KILL_LIQ_MIN         = float(os.getenv("KILL_LIQ_MIN",         "1.00"))  # partial fill: depth < 3x bet size
KILL_SECS_MIN        = float(os.getenv("KILL_SECS_MIN",        "3.0"))   # late noise: too close to resolution
KILL_COOLDOWN_SECS   = float(os.getenv("KILL_COOLDOWN_SECS",   "10.0"))  # lowered 30→10s: faster re-entry for volume
# filter 7: L2 depth imbalance — skip when book is overwhelmingly against our direction
# imbalance = (bid_total - ask_total) / (bid_total + ask_total);  -1.0 = disabled
# Tick-level data (trentmkelly/polymarket_crypto_derivatives, 3 BTC 15m markets):
#   UP-resolving markets: mean imbalance = +0.191
#   DOWN-resolving markets: mean imbalance = -0.042 to -0.227
#   Cheap windows in DOWN markets: imbalance = -0.402 to -0.816
# Set -0.75 to skip strongly-against-direction books; -1.0 to disable.
KILL_DEPTH_IMBALANCE = float(os.getenv("KILL_DEPTH_IMBALANCE", "-1.0"))  # default disabled; tune with live data
# filter 8: dev ceiling — backtest shows underlying move >= 0.07% → near-zero WR

# filter 9: velocity ceiling — if BTC screaming for multiple seconds, market
# makers already repriced. Micro-lag arb edge lives at LOW-to-MEDIUM velocity.
# 0.0 = disabled (tune after observing live velocity distribution).
KILL_VELOCITY_MAX = float(os.getenv("KILL_VELOCITY_MAX", "0.0"))  # frac/sec; 0 = off
# (EV collapses from +$4.67 to -$3.19 above 0.05% move threshold).
# 0.0 = disabled; 0.00070 = 0.07% cutoff (keeps 74% of signals, lifts EV to +$3.97)
KILL_DEV_MAX = float(os.getenv("KILL_DEV_MAX", "0.0"))  # 0.0 = disabled
# Akey et al: top-1% bots fire ~89×/day (40× more than non-bots) by bypassing cooldown
# on truly elite signals. When arb_score >= ARB_OVERRIDE_SCORE, cooldown is waived.
ELITE_COOLDOWN_BYPASS = os.getenv("ELITE_COOLDOWN_BYPASS", "true").lower() == "true"
# ─────────────────────────────────────────────────────────────────────────────

# ── Risk controls ──
MAX_OPEN_POSITIONS     = int(os.getenv("MAX_OPEN_POSITIONS",      "20"))
MAX_DAILY_LOSS         = float(os.getenv("DAILY_LOSS_LIMIT",      "50.0"))
SCAN_INTERVAL          = float(os.getenv("SCAN_INTERVAL",         "0.2"))  # 0.2s = 5× faster than default 1.0s
MAX_TRADES_PER_COIN_DAY= int(os.getenv("MAX_TRADES_PER_COIN_DAY", "0"))   # 0 = disabled (no per-coin daily cap)

# ── Kelly criterion sizing ────────────────────────────────────────────────────
# Binary market Kelly: f* = (p*b - q) / b  where b=(1-ask)/ask, q=1-p
# Fractional Kelly at 25% limits drawdown volatility vs full Kelly.
KELLY_SIZING_ENABLED   = os.getenv("KELLY_SIZING_ENABLED", "true").lower() == "true"
KELLY_FRACTION         = float(os.getenv("KELLY_FRACTION",  "0.25"))  # 25% of full Kelly
KELLY_MAX_SIZE         = float(os.getenv("KELLY_MAX_SIZE",  "20.0"))  # hard USDC cap per bet

# ── Regime / market-health filters ───────────────────────────────────────────
# DEAD regime: Binance price flat for REGIME_DEAD_SECS → skip (no oracle-lag edge).
# Elevated spread: bid-ask ratio > SPREAD_ELEV_MULT × baseline → skip (liquidity degraded).
REGIME_DEAD_ENABLED    = os.getenv("REGIME_DEAD_ENABLED",  "true").lower() == "true"
REGIME_DEAD_SECS       = float(os.getenv("REGIME_DEAD_SECS",  "60.0"))  # secs of price flatness = dead
REGIME_DEAD_MIN_VEL    = float(os.getenv("REGIME_DEAD_MIN_VEL", "0.00002"))  # min velocity to be "alive"
SPREAD_ELEV_ENABLED    = os.getenv("SPREAD_ELEV_ENABLED",  "true").lower() == "true"
SPREAD_ELEV_MULT       = float(os.getenv("SPREAD_ELEV_MULT",   "3.0"))   # spread > 3× baseline → skip

# ── Tick-triggered scanning (F1 speed) ───────────────────────────────────────
# When Binance moves ≥ TICK_TRIGGER_MAG in a single tick, the scan loop wakes
# IMMEDIATELY instead of waiting up to SCAN_INTERVAL seconds.
# This cuts reaction time from avg 500ms → <10ms after a sharp move.
# TICK_TRIGGER_MAG = 0.02% per tick is enough to signal an oracle-lag setup.
# Set TICK_TRIGGER_ENABLED=false to disable (fall back to fixed interval).
TICK_TRIGGER_ENABLED = os.getenv("TICK_TRIGGER_ENABLED", "true").strip().lower() in ("true","1","yes")
TICK_TRIGGER_MAG     = float(os.getenv("TICK_TRIGGER_MAG", "0.0001"))  # 0.01% per tick (was 0.02%; research: signal on every tick)

# Zombie WS detection (research: oracle-lag-sniper, Mar 2026).
# The WS connection stays alive (heartbeats flow) but REAL price data stops.
# Track last actual price timestamp; if stale > WS_STALL_TIMEOUT seconds → force reconnect.
# Research author: "spent an embarrassing amount of time debugging why the bot went
# silent for 20 minutes" before adding this. 30s is safe; lower = faster recovery.
WS_STALL_TIMEOUT = float(os.getenv("WS_STALL_TIMEOUT", "30.0"))  # seconds without real data → reconnect

# Module-level asyncio.Event — set in WS handler, awaited in scan loop.
# Initialized to None; replaced with a live Event in CryptoGhostScanner.run().
_TICK_SCAN_EVENT: "Optional[asyncio.Event]" = None

# ── CLOB maintenance detection ────────────────────────────────────────────────
_CLOB_DOWN: bool = False            # True while Polymarket CLOB is unreachable
_CLOB_DOWN_SINCE: Optional[float] = None  # time.time() when downtime started

# ── Adaptive risk management ──────────────────────────────────────────────────
# Tracks consecutive losses at runtime and adjusts sizing + trading access.
#
#  LOSS_STREAK_REDUCE  — after this many consecutive losses, multiply every
#                        position size by LOSS_SIZE_FACTOR (e.g. 0.5 = half size).
#  LOSS_STREAK_PAUSE   — after this many consecutive losses, halt all new trades
#                        for LOSS_PAUSE_MINUTES minutes. Resets on first win.
#  LOSS_SIZE_FACTOR    — size multiplier applied during a loss streak (0.5 = 50%).
#
ADAPTIVE_RISK        = os.getenv("ADAPTIVE_RISK",        "true").lower() == "true"
LOSS_STREAK_REDUCE   = int(os.getenv("LOSS_STREAK_REDUCE",   "3"))
LOSS_STREAK_PAUSE    = int(os.getenv("LOSS_STREAK_PAUSE",    "5"))
LOSS_PAUSE_MINUTES   = float(os.getenv("LOSS_PAUSE_MINUTES", "15"))
LOSS_SIZE_FACTOR     = float(os.getenv("LOSS_SIZE_FACTOR",   "0.5"))
# ─────────────────────────────────────────────────────────────────────────────

# ── Oracle arbitrage engine (PATH B) ──────────────────────────────────────────
# Extends the oracle-discrepancy gate with a statistical probability model.
# Converts Binance price deviation into an expected market probability using
# the normal CDF (Gaussian diffusion), then scores the mismatch vs what
# Polymarket is actually pricing. Signals with larger mismatch + higher
# velocity + shorter time left fire with boosted priority and can override
# the TOP_SIGNALS_PER_CYCLE cap entirely.
#
#  fair_p     = Φ( |dev| / (σ·√(secs/window)) )  — expected P(our side wins)
#  mismatch   = fair_p − ask                       — market underpricing
#  arb_score  = mismatch × recency_boost × vel_boost   [0..1]
#
# ARB_MIN_MISMATCH:   gate off by default (0.0); raise to filter weak arb.
# ARB_OVERRIDE_SCORE: arb_score ≥ this bypasses TOP_SIGNALS_PER_CYCLE cap.
# ARB_RECENCY_WINDOW: seconds; signals inside this window get recency boost.
# ARB_RECENCY_BOOST:  multiplier applied when secs ≤ ARB_RECENCY_WINDOW.
# ARB_VEL_BOOST_SCALE:velocity (frac/sec) at which vel_boost reaches 2×.
# COIN_VOL_5M_*:      per-coin empirical 5-min fractional price σ (2025–2026).
ARB_ENABLED          = os.getenv("ARB_ENABLED",          "true").lower() == "true"
ARB_MIN_MISMATCH     = float(os.getenv("ARB_MIN_MISMATCH",     "0.0"))    # gate (off by default)
ARB_OVERRIDE_SCORE   = float(os.getenv("ARB_OVERRIDE_SCORE",   "0.80"))   # bypass TOP_N cap
ARB_RECENCY_WINDOW   = float(os.getenv("ARB_RECENCY_WINDOW",   "15.0"))   # seconds
ARB_RECENCY_BOOST    = float(os.getenv("ARB_RECENCY_BOOST",    "1.5"))    # ×1.5 within window
ARB_VEL_BOOST_SCALE  = float(os.getenv("ARB_VEL_BOOST_SCALE",  "0.0001")) # velocity normalizer
COIN_VOL_5M: dict = {
    "BTC": float(os.getenv("COIN_VOL_5M_BTC", "0.0015")),
    "ETH": float(os.getenv("COIN_VOL_5M_ETH", "0.0020")),
    "SOL": float(os.getenv("COIN_VOL_5M_SOL", "0.0030")),
    "XRP": float(os.getenv("COIN_VOL_5M_XRP", "0.0025")),
    "BNB": float(os.getenv("COIN_VOL_5M_BNB", "0.0020")),
}
# ─────────────────────────────────────────────────────────────────────────────
# ── Anti-longshot calibration (Reichenbach-Walther / Fensory 2025-2026) ─────────
# Polymarket's <10% tokens win ~14% of the time (vs 10% priced).
# Apply a small upward correction to fair_p for cheap ask tokens to compensate.
# Set ANTI_LONGSHOT_ENABLED=false to revert to pure Gaussian model.
ANTI_LONGSHOT_ENABLED   = os.getenv("ANTI_LONGSHOT_ENABLED",   "true").lower() == "true"
ANTI_LONGSHOT_THRESHOLD = float(os.getenv("ANTI_LONGSHOT_THRESHOLD", "0.10"))  # apply when ask < this
ANTI_LONGSHOT_MULT      = float(os.getenv("ANTI_LONGSHOT_MULT",      "1.15"))  # fair_p × mult (1.40 = full Fensory data)
ANTI_LONGSHOT_MULT_DOWN = float(os.getenv("ANTI_LONGSHOT_MULT_DOWN", "1.05"))  # reduced mult for DOWN tokens (4% base-rate headwind from bull market bias)
# ─────────────────────────────────────────────────────────────────────────────

# ── Dynamic position sizing ───────────────────────────────────────────────────
# Scales USDC per trade based on a composite signal quality score that blends
# confidence (4-dimension score) and arb_score (statistical mismatch + velocity).
#
# Quality tiers (piecewise-linear, fully interpolated):
#   quality < DYN_THRESH_LOW  → DYN_MULT_LOW  (e.g. 0.5× — low conviction)
#   LOW ≤ quality < DYN_THRESH_HIGH → interpolates smoothly from LOW to 1.0×
#   quality ≥ DYN_THRESH_HIGH → scales from 1.0× up to DYN_MULT_ELITE (e.g. 2.0×)
#
# ARB blend: quality = confidence*(1−DYN_ARB_BLEND) + arb_score*DYN_ARB_BLEND
#   Set DYN_ARB_BLEND=0 to use raw confidence only.
#
# Exposure caps enforce maximum total USDC at risk across all open positions
# (MAX_TOTAL_EXPOSURE) and per-coin (MAX_COIN_EXPOSURE). Both check pending
# positions in addition to confirmed open ones. Set to 0 to disable.
DYNAMIC_SIZING      = os.getenv("DYNAMIC_SIZING",      "true").lower() == "true"
DYN_THRESH_LOW      = float(os.getenv("DYN_THRESH_LOW",      "0.60"))  # below → DYN_MULT_LOW
DYN_THRESH_HIGH     = float(os.getenv("DYN_THRESH_HIGH",     "0.90"))  # above → elite scaling
DYN_MULT_LOW        = float(os.getenv("DYN_MULT_LOW",        "0.5"))   # e.g. $3 → $1.50
DYN_MULT_ELITE      = float(os.getenv("DYN_MULT_ELITE",      "2.0"))   # e.g. $3 → $6.00
DYN_ARB_BLEND       = float(os.getenv("DYN_ARB_BLEND",       "0.3"))   # 30% arb weight
MAX_TOTAL_EXPOSURE  = float(os.getenv("MAX_TOTAL_EXPOSURE",  "30.0"))  # 0 = disabled
MAX_COIN_EXPOSURE   = float(os.getenv("MAX_COIN_EXPOSURE",   "12.0"))  # 0 = disabled
# ─────────────────────────────────────────────────────────────────────────────

# ── Segment performance tracking ──────────────────────────────────────────────
# Reads resolved trades from the DB every SEG_REFRESH_MIN minutes, computes
# rolling win-rate for four dimensions (tier, coin, ET hour, tier×coin), and
# returns a composite multiplier applied to both priority_score and dyn_size.
#
# Multiplier curve (relative to SEG_NEUTRAL_WR = the theoretical break-even):
#   wr < SEG_DISABLE_BELOW  → 0.0  (hard skip — segment is statistically dead)
#   wr = SEG_NEUTRAL_WR     → 1.0  (neutral — base size and priority)
#   wr = SEG_BOOST_ABOVE    → SEG_MAX_MULT  (best segments get more capital)
#
# Uses a geometric mean across dimensions so one weak slice doesn't fully
# suppress an otherwise strong signal. Requires SEG_MIN_SAMPLE trades in a
# dimension before that dimension's multiplier activates; otherwise neutral.
SEG_TRACKING_ENABLED = os.getenv("SEG_TRACKING_ENABLED", "true").lower() == "true"
SEG_WINDOW_TRADES    = int(os.getenv("SEG_WINDOW_TRADES",    "100"))   # rolling history per dim
SEG_MIN_SAMPLE       = int(os.getenv("SEG_MIN_SAMPLE",       "10"))    # trades needed to activate
SEG_NEUTRAL_WR       = float(os.getenv("SEG_NEUTRAL_WR",     "0.03"))  # 3% WR = mult 1.0
SEG_DISABLE_BELOW    = float(os.getenv("SEG_DISABLE_BELOW",  "0.01"))  # <1% WR = hard skip
SEG_BOOST_ABOVE      = float(os.getenv("SEG_BOOST_ABOVE",    "0.06"))  # 6% WR = max boost
SEG_MAX_MULT         = float(os.getenv("SEG_MAX_MULT",        "1.5"))   # cap on boost multiplier
SEG_REFRESH_MIN      = float(os.getenv("SEG_REFRESH_MIN",     "5.0"))   # DB refresh interval (min)
SEG_SUMMARY_MIN      = float(os.getenv("SEG_SUMMARY_MIN",     "30.0"))  # summary print interval (min)
# ─────────────────────────────────────────────────────────────────────────────

# ── PERFORMANCE ANALYTICS ──────────────────────────────────────────────
PERF_ENABLED       = os.getenv("PERF_ENABLED",       "true").lower() == "true"
PERF_WINDOW_TRADES = int(os.getenv("PERF_WINDOW_TRADES", "50"))    # rolling trade window
PERF_REFRESH_MIN   = float(os.getenv("PERF_REFRESH_MIN", "5.0"))   # DB re-read interval (min)
PERF_ROLLING_MIN   = float(os.getenv("PERF_ROLLING_MIN", "30.0"))  # rolling-report print interval (min)
PERF_ADAPT_ENABLED = os.getenv("PERF_ADAPT_ENABLED", "true").lower() == "true"
PERF_DISABLE_WR    = float(os.getenv("PERF_DISABLE_WR", "0.005"))  # <0.5% WR after PERF_MIN_SAMPLE -> disable
PERF_BOOST_WR      = float(os.getenv("PERF_BOOST_WR",   "0.08"))   # >8% WR -> ease confidence gate
PERF_MIN_SAMPLE    = int(os.getenv("PERF_MIN_SAMPLE",    "20"))     # min trades before adapting


# ── LATENCY TRACKING ────────────────────────────────────────────────────────────
LATENCY_TRACK_ENABLED  = os.getenv("LATENCY_TRACK_ENABLED",  "true").lower() == "true"
LATENCY_FRESH_MS_ELITE = float(os.getenv("LATENCY_FRESH_MS_ELITE", "3000"))  # <3s  -> 2.0x
LATENCY_FRESH_MS_GOOD  = float(os.getenv("LATENCY_FRESH_MS_GOOD",  "8000"))  # <8s  -> 1.5x
LATENCY_FRESH_MS_BASE  = float(os.getenv("LATENCY_FRESH_MS_BASE",  "15000")) # <15s -> 1.2x
LATENCY_STATS_MIN      = float(os.getenv("LATENCY_STATS_MIN",      "60.0"))  # print interval (min)
# Hard freshness gate: reject any T2/T3 signal where the triggering Binance tick
# is older than this many seconds.  Research: oracle lag window ≈55s; median
# move_age in production was 88s → bot was firing AFTER the window closed.
# 25s gives a comfortable margin to still catch the lag. Set 0 to disable.
# Dual-fire simultaneous signals bypass this (arbitrage — age irrelevant).
KILL_FRESHNESS_SECS    = float(os.getenv("KILL_FRESHNESS_SECS",    "45.0"))  # 45s = full oracle lag window (55s confirmed, 10s buffer)
# v9.5: Skip T1 strike-crossing scan entirely if no strikes are firing.
# Saves ~6s at startup + ~5min idle gamma fetches.
# T1 strike + T4 resolution-arb tiers removed 2026-05-15

# ── DST-safe ET timezone (for hour-of-day filters) ─────────────────
try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:
    _ET_TZ = None  # Fallback to UTC-4 below if zoneinfo unavailable


def _et_hour() -> int:
    """Current ET hour (0-23). Falls back to UTC-4 if tzdata missing."""
    from datetime import datetime, timezone, timedelta
    if _ET_TZ is not None:
        return datetime.now(_ET_TZ).hour
    return (datetime.now(timezone.utc) - timedelta(hours=4)).hour


# ── News calendar alerts (ForexFactory, 2026-05-10) ─────────────────────────
# Sends Telegram heads-up before HIGH impact macro events so you know to be live.
# No filters, no gates — pure situational awareness pushed to your phone.
NEWS_CALENDAR_ENABLED    = os.getenv("NEWS_CALENDAR_ENABLED", "true").strip().lower() in ("true","1","yes")
NEWS_ALERT_CURRENCIES    = set(c.strip().upper() for c in os.getenv("NEWS_ALERT_CURRENCIES", "USD").split(",") if c.strip())
NEWS_ALERT_MINS_EARLY    = int(os.getenv("NEWS_ALERT_MINS_EARLY",    "60"))
NEWS_ALERT_MINS_IMMINENT = int(os.getenv("NEWS_ALERT_MINS_IMMINENT",  "5"))
FF_CALENDAR_URL          = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# ── Bias-aware filters (data-backed from 5,852 pooled markets) ──────
# Coins listed here have such a strong UP-bias in pooled data that
# cheap-DOWN longshots have negative EV. Skip cheap-DOWN entries on
# these coins. Comma-separated list in .env. Set blank to disable.
SKIP_DOWN_BIASED_COINS = set(
    c.strip().upper() for c in
    os.getenv("SKIP_DOWN_BIASED_COINS", "").split(",")  # cleared for data collection (was BNB)
    if c.strip()
)

# Whole-coin skip: reject any fire on coins in this list regardless of
# UP/DOWN side. Use when a coin's edge is gone entirely. Comma-separated.
SKIP_COINS = set(
    c.strip().upper() for c in
    os.getenv("SKIP_COINS", "").split(",")
    if c.strip()
)

# Hour-of-day gate. Hours listed here are skipped for cheap-UP entries
# (in pooled data they have <50% UP-win rate). 11 AM ET only wins 40%
# UP — flip the strategy in those hours, or skip outright.
DEAD_UP_HOURS_ET = set(
    int(h.strip()) for h in
    os.getenv("DEAD_UP_HOURS_ET", "").split(",")  # cleared for data collection (was 11,1)
    if h.strip().isdigit()
)
DEAD_DOWN_HOURS_ET = set(
    int(h.strip()) for h in
    os.getenv("DEAD_DOWN_HOURS_ET", "").split(",")  # cleared for data collection (was 4,5,19)
    if h.strip().isdigit()
)
BIAS_FILTERS_ENABLED = os.getenv("BIAS_FILTERS", "true").strip().lower() in ("true","1","yes")

# ── MarketGhost hour-of-day directional bias (v8 parity, 2026-05-13) ─────────
# Mirrors v8's "Direction filter: Auto (MarketGhost hour bias)".
# Reads live DB trade history, computes per-UTC-hour WR for UP vs DOWN.
# If one direction has WR >= HOUR_BIAS_MIN_WR with >= HOUR_BIAS_MIN_SAMPLES,
# the opposite direction is blocked at that hour.  Refreshes every 30 min.
HOUR_BIAS_ENABLED       = os.getenv("HOUR_BIAS_ENABLED",       "true").strip().lower() in ("true","1","yes")
HOUR_BIAS_MIN_WR        = float(os.getenv("HOUR_BIAS_MIN_WR",        "0.58"))
HOUR_BIAS_MIN_SAMPLES   = int(os.getenv("HOUR_BIAS_MIN_SAMPLES",     "5"))
HOUR_BIAS_REFRESH_MIN   = int(os.getenv("HOUR_BIAS_REFRESH_MIN",     "10"))
HOUR_BIAS_LOOKBACK_DAYS = int(os.getenv("HOUR_BIAS_LOOKBACK_DAYS",   "30"))
HOUR_BIAS_RECENT_DAYS   = int(os.getenv("HOUR_BIAS_RECENT_DAYS",     "7"))
HOUR_BIAS_RECENT_WEIGHT = float(os.getenv("HOUR_BIAS_RECENT_WEIGHT", "2.0"))

# ── CoinDirEngine — dynamic coin+direction auto-blocker ──────────────────────
# Reads live DB every COIN_DIR_REFRESH_SECS seconds. Computes EV/trade per
# coin+direction pair over the last COIN_DIR_LOOKBACK settled trades.
# Auto-blocks pairs whose EV/trade falls below COIN_DIR_EV_THRESH (-0.30).
# Replaces the static SKIP_DOWN_BIASED_COINS env var. Pre-seeds from it on startup.
COIN_DIR_REFRESH_SECS   = int(os.getenv("COIN_DIR_REFRESH_SECS",   "60"))
COIN_DIR_LOOKBACK       = int(os.getenv("COIN_DIR_LOOKBACK",       "200"))  # wider window: more T2 samples
COIN_DIR_MIN_TRADES     = int(os.getenv("COIN_DIR_MIN_TRADES",     "10"))   # T1 min samples to block
COIN_DIR_MIN_TRADES_T2  = int(os.getenv("COIN_DIR_MIN_TRADES_T2",  "20"))   # T2: needs 20 samples before blocking
COIN_DIR_EV_THRESH      = float(os.getenv("COIN_DIR_EV_THRESH",    "-0.50"))
# Bull regime: when True, CoinDir never blocks UP signals (DOWN still filtered by EV).
# Enable during sustained bull markets — UP oracle-lag signals have proven edge.
BULL_REGIME             = os.getenv("BULL_REGIME", "false").lower() == "true"

# ── Coinbase oracle agreement gate (2026-05-09) ──────────────────────────────
# Pre-fire cross-exchange check: Coinbase's 5m candle direction must not oppose
# the bet. Ghost-bot data: ~43% of Binance/Coinbase disagreements on tight
# markets were losses. Gate only fires for T2 (oracle-lag tier, 5-min markets).
# Fail-open on API error — never blocks a valid signal due to network hiccup.
# CB_ORACLE_TIE_PCT: |5m pct change| below this = "TIE" → skip (too tight).
CB_ORACLE_GATE    = os.getenv("CB_ORACLE_GATE", "false").strip().lower() in ("true","1","yes")  # disabled: disagreement trades win 57% — gate was blocking winners
CB_ORACLE_TIE_PCT = float(os.getenv("CB_ORACLE_TIE_PCT", "0.02"))  # 0.02% floor

# Hour confidence weighting -- boost proven hours, dampen losing hours.
# Backtest (133 live trades): 23 ET = 33.3% WR +$21.57 EV/trade; 21 ET = strong;
# 17 ET = positive. 14 ET = -$34 total; 16 ET = negative; 20 ET = 0% WR 5 losses.
HOUR_BOOST_HOURS_ET = set(
    int(h.strip()) for h in
    os.getenv("HOUR_BOOST_HOURS_ET", "23,21,17").split(",")
    if h.strip().isdigit()
)
HOUR_BOOST_FACTOR   = float(os.getenv("HOUR_BOOST_FACTOR",  "1.10"))  # e.g. 1.10 = +10% conf

HOUR_REDUCE_HOURS_ET = set(
    int(h.strip()) for h in
    os.getenv("HOUR_REDUCE_HOURS_ET", "14,16,20").split(",")
    if h.strip().isdigit()
)
HOUR_REDUCE_FACTOR  = float(os.getenv("HOUR_REDUCE_FACTOR", "0.85"))  # e.g. 0.85 = -15% conf

# v9 Engine Change 1: Lazy orderbook fetch (filters → predict side → 1 fetch).
# Strategy-neutral in 95%+ of cases per analysis. Default ON.
# Set LAZY_ORDERBOOK=false to fall back to v7-style fetch-both-then-pick.
LAZY_ORDERBOOK = os.getenv("LAZY_ORDERBOOK", "true").strip().lower() in ("true","1","yes")

# ── Paper vs Live ──
PAPER_TRADE    = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true", "1", "yes")
PORTFOLIO_SIZE = float(os.getenv("PORTFOLIO_SIZE", "200.0"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(SCRIPT_DIR,
                "GHOST_LATTICE_PAPER.db" if PAPER_TRADE else "GHOST_LATTICE.db")


# ─── DATABASE ─────────────────────────────────────────────────────────────────
def _db(timeout: float = 5.0) -> sqlite3.Connection:
    """Single entry-point for all DB connections.
    Sets WAL journal mode + busy_timeout on every connection so concurrent
    reads from dashboard / resolver / redeemer never produce 'database is locked'.
    WAL persists in the DB file; busy_timeout must be set per-connection.
    """
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")   # wait up to 5s before raising OperationalError
    conn.execute("PRAGMA synchronous=NORMAL")   # safe + fast under WAL
    return conn

def init_db():
    conn = _db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            tier        INTEGER,
            coin        TEXT,
            market_id   TEXT,
            token_id    TEXT,
            question    TEXT,
            entry_price REAL,
            size_usdc   REAL,
            order_id    TEXT,
            outcome     TEXT DEFAULT 'UP',
            status      TEXT DEFAULT 'open',
            pnl         REAL DEFAULT 0,
            closed_at   TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT,
            icon    TEXT,
            msg     TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_stats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT,
            markets_found   INTEGER DEFAULT 0,
            signals_passed  INTEGER DEFAULT 0,
            skip_ask        INTEGER DEFAULT 0,
            skip_liq        INTEGER DEFAULT 0,
            skip_certainty  INTEGER DEFAULT 0,
            skip_window     INTEGER DEFAULT 0,
            scan_ms         INTEGER DEFAULT 0,
            skip_vel        INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fill_attempts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT,
            tier            INTEGER,
            coin            TEXT,
            token_id        TEXT,
            ask_price       REAL,
            attempted_size  REAL,
            filled          INTEGER DEFAULT 0,
            fill_price      REAL,
            fill_size       REAL,
            slippage_pct    REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS latency_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT,
            tier         INTEGER,
            coin         TEXT,
            token_id     TEXT,
            move_age_ms  REAL,
            order_ms     REAL,
            confirm_ms   REAL,
            total_ms     REAL,
            freshness    REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_scores (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT,
            tier          INTEGER,
            coin          TEXT,
            outcome       TEXT,
            ask           REAL,
            liq           REAL,
            dev           REAL,
            velocity      REAL,
            mins_left     REAL,
            utility_score REAL,
            s_certainty   REAL,
            s_velocity    REAL,
            s_liquidity   REAL,
            s_time        REAL,
            fired         INTEGER DEFAULT 0
        )
    """)
    # Schema migrations — safe to run every startup (errors = column exists already)
    for migration in [
        "ALTER TABLE trades ADD COLUMN outcome    TEXT DEFAULT 'UP'",
        "ALTER TABLE trades ADD COLUMN fill_price REAL DEFAULT NULL",
        "ALTER TABLE trades ADD COLUMN fill_size  REAL DEFAULT NULL",
        "ALTER TABLE trades ADD COLUMN seg_mult    REAL    DEFAULT 1.0",
        "ALTER TABLE trades ADD COLUMN ghost_score INTEGER DEFAULT NULL",
        "ALTER TABLE scan_stats ADD COLUMN skip_vel INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(migration)
        except Exception:
            pass
    conn.commit()
    conn.close()

def log_scan_stats(markets, signals, skip_ask, skip_liq, skip_cert, skip_win, ms, skip_vel=0):
    try:
        conn = _db()
        conn.execute("""
            INSERT INTO scan_stats
            (ts, markets_found, signals_passed, skip_ask, skip_liq, skip_certainty, skip_window, scan_ms, skip_vel)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (datetime.now(timezone.utc).isoformat(),
              markets, signals, skip_ask, skip_liq, skip_cert, skip_win, ms, skip_vel))
        conn.execute("DELETE FROM scan_stats WHERE id NOT IN (SELECT id FROM scan_stats ORDER BY id DESC LIMIT 500)")
        conn.commit()
        conn.close()
    except Exception:
        pass

# ─── GHOST SIGNAL SCORER (inline — no external import) ───────────────────────
# Adapted from signal_scorer.py. Weights: velocity=30, certainty=25, liq=20, time=10
# Used for DATA COLLECTION ONLY — not a gate. Logs every qualifying signal
# to the signal_scores table so we can tune thresholds post-session.
_GS_W_VEL  = 30
_GS_W_CERT = 25
_GS_W_LIQ  = 20
_GS_W_TIME = 10

def _gs_score_velocity(velocity, outcome: str) -> float:
    if velocity is None:
        return 0.0
    is_down = outcome.upper() in ("DOWN", "NO")
    aligned = (velocity < 0) if is_down else (velocity > 0)
    if not aligned:
        return 0.0
    s = abs(velocity)
    if s >= 0.003:   return float(_GS_W_VEL)
    if s >= 0.0015:  return round(_GS_W_VEL * 0.73, 2)
    if s >= 0.0008:  return round(_GS_W_VEL * 0.50, 2)
    if s >= 0.0004:  return round(_GS_W_VEL * 0.27, 2)
    return round(_GS_W_VEL * 0.07, 2)

def _gs_score_certainty(ask: float, max_entry: float) -> float:
    if ask <= 0 or max_entry <= 0 or ask >= max_entry:
        return 0.0
    return round((1.0 - ask / max_entry) * _GS_W_CERT, 2)

def _gs_score_liquidity(liq: float) -> float:
    if liq is None or liq <= 0:  return 0.0
    if liq >= 100:  return float(_GS_W_LIQ)
    if liq >= 50:   return round(_GS_W_LIQ * 0.80, 2)
    if liq >= 20:   return round(_GS_W_LIQ * 0.60, 2)
    if liq >= 10:   return round(_GS_W_LIQ * 0.40, 2)
    if liq >= 5:    return round(_GS_W_LIQ * 0.20, 2)
    return 0.0

def _gs_score_time(mins_left: float, tier: int) -> float:
    if mins_left is None or mins_left < 0:
        return 0.0
    sweet = {2: (3, 15), 1: (20, 60), 4: (60, 180)}.get(tier, (3, 20))
    lo, hi = sweet
    if lo <= mins_left <= hi:
        mid  = (lo + hi) / 2
        dist = abs(mins_left - mid) / (hi - lo)
        return round(_GS_W_TIME * (1.0 - dist * 0.5), 2)
    elif mins_left < lo:
        return round(_GS_W_TIME * (mins_left / lo) * 0.5, 2)
    else:
        overshoot = (mins_left - hi) / hi
        return round(_GS_W_TIME * max(0, 0.3 - overshoot * 0.3), 2)

def _gs_log_signal_score(sig: dict, fired: bool = False):
    """Log a signal's Ghost utility score to oracle_sniper.db — passive, never raises."""
    try:
        tier     = sig.get("tier", 2)
        coin     = sig.get("coin", "?")
        outcome  = sig.get("outcome", "NO")
        ask      = sig.get("ask", 0.0)
        liq      = sig.get("liq", 0.0)
        dev      = sig.get("dev", 0.0)
        velocity = sig.get("velocity")
        mins     = sig.get("mins", 0.0)
        max_entry_map = {2: T2_MAX_ENTRY, 1: T1_MAX_ENTRY}
        max_entry = max_entry_map.get(tier, T2_MAX_ENTRY)

        s_vel  = _gs_score_velocity(velocity, outcome)
        s_cert = _gs_score_certainty(ask, max_entry)
        s_liq  = _gs_score_liquidity(liq)
        s_time = _gs_score_time(mins, tier)
        total  = round(s_vel + s_cert + s_liq + s_time, 1)

        conn = _db()
        conn.execute("""
            INSERT INTO signal_scores
            (ts, tier, coin, outcome, ask, liq, dev, velocity, mins_left,
             utility_score, s_certainty, s_velocity, s_liquidity, s_time, fired)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (datetime.now(timezone.utc).isoformat(),
              tier, coin, outcome, ask, liq, dev, velocity, mins,
              total, s_cert, s_vel, s_liq, s_time, 1 if fired else 0))
        conn.execute("DELETE FROM signal_scores WHERE id NOT IN "
                     "(SELECT id FROM signal_scores ORDER BY id DESC LIMIT 10000)")
        conn.commit()
        conn.close()
    except Exception:
        pass  # never let scoring crash the scanner


def write_stats_json():
    """Write stats to JSON file for web dashboard to read."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        conn  = _db()
        wins    = conn.execute("SELECT COUNT(*) FROM trades WHERE status='won'").fetchone()[0]
        losses  = conn.execute("SELECT COUNT(*) FROM trades WHERE status='lost'").fetchone()[0]
        open_   = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
        pnl     = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status IN ('won','lost')").fetchone()[0]
        recent  = conn.execute("""
            SELECT ts, tier, coin, entry_price, size_usdc, pnl, status
            FROM trades ORDER BY id DESC LIMIT 10
        """).fetchall()

        total = wins + losses
        wr    = (wins / total * 100) if total > 0 else 0

        trades_list = []
        for row in recent:
            ts_, tier_, coin_, entry_, size_, p_, status_ = row
            trades_list.append({
                "time":   ts_[11:19] if ts_ else "",
                "tier":   tier_, "coin":   coin_,
                "entry":  f"@{entry_:.3f}" if entry_ else "",
                "size":   size_, "pnl": round(p_ or 0, 2), "status": status_,
            })

        # Full trades list for Live Trades tab (BEFORE closing connection)
        all_trades = conn.execute("""
            SELECT id, ts, tier, coin, question, entry_price, size_usdc, pnl, status
            FROM trades ORDER BY id DESC LIMIT 200
        """).fetchall()
        conn.close()

        all_trades_list = []
        for row in all_trades:
            all_trades_list.append({
                "id": row[0], "ts": row[1], "tier": row[2], "coin": row[3],
                "question": row[4], "entry_price": row[5], "size_usdc": row[6],
                "pnl": round(row[7] or 0, 2), "status": row[8]
            })

        data = {
            "wins": wins, "losses": losses, "open": open_,
            "total_pnl": round(pnl, 2), "win_rate": round(wr, 1),
            "recent_trades": trades_list, "all_trades": all_trades_list,
            "updated": datetime.now().strftime("%H:%M:%S")
        }
        script_dir = os.path.dirname(os.path.abspath(__file__))
        out_path = os.path.join(script_dir, "ghost_stats.json")
        with open(out_path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        # Print so we can see what's going wrong
        print(f"[write_stats_json ERROR] {type(e).__name__}: {e}")



# Dedup cache: (icon, msg) → last logged epoch. Prevents signal spam flooding the feed.
_log_event_cache: dict = {}
_LOG_EVENT_DEDUP_SECS = 60  # same icon+msg won't repeat within this window

def log_event(icon: str, msg: str):
    """Write an event to the DB so the dashboard can display it."""
    # Deduplicate: skip if same icon+msg was logged within the last 60 seconds
    _key = (icon, msg)
    _now = time.monotonic()
    if _now - _log_event_cache.get(_key, 0) < _LOG_EVENT_DEDUP_SECS:
        return
    _log_event_cache[_key] = _now
    try:
        conn = _db()
        conn.execute("INSERT INTO events (ts, icon, msg) VALUES (?,?,?)",
                     (datetime.now().strftime("%H:%M:%S"), icon, msg))
        # Keep only last 500 events (100 was too small — signal spam buried wins in ~25s)
        conn.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)")
        conn.commit()
        conn.close()
    except Exception:
        pass

_BRAIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ghost_brain.json")
_brain_cache: dict = {}
_brain_cache_ts: float = 0.0

def _read_ghost_brain(coin: str):
    """Return (score_or_None, state, learned_threshold_or_None) for coin.
    Caches for up to 12 seconds so rapid-fire scans don't hammer the FS.

    Returns None for score in two cases:
      - File missing / JSON error (ghost_brain.py not running)
      - File mtime > 5 min old (ghost_brain.py crashed mid-session)
    None scores are stored as NULL in the DB, excluded from the learning loop,
    and treated as fail-open (gate never blocks when score is None).
    """
    global _brain_cache, _brain_cache_ts
    now = time.time()
    if now - _brain_cache_ts > 12:
        try:
            # Staleness check BEFORE reading: if file hasn't been updated in
            # 5 min, ghost_brain.py is down — don't use stale data for gating.
            _mtime = os.path.getmtime(_BRAIN_FILE)
            if now - _mtime > 300:   # 5 min
                _brain_cache    = {}
                _brain_cache_ts = now
                return None, "STALE", None
            with open(_BRAIN_FILE, "r") as _f:
                _brain_cache    = json.load(_f)
                _brain_cache_ts = now
        except Exception:
            _brain_cache    = {}
            _brain_cache_ts = now
            return None, "OFFLINE", None
    if not _brain_cache:
        return None, "OFFLINE", None
    coins_data = _brain_cache.get("coins", {})
    cd = coins_data.get(coin, {})
    score  = cd.get("score")    # None if coin not in brain yet
    state  = cd.get("state", "UNKNOWN")
    learned = _brain_cache.get("learned_thresholds", {}).get(coin)
    return score, state, learned


def log_trade(tier, coin, market_id, token_id, question, entry, size, order_id,
              outcome="UP", fill_price=None, fill_size=None, ghost_score=None):
    """Record a new trade. fill_price/fill_size are the actual execution values
    parsed from the order response; they may differ from the signal ask/size."""
    conn = _db(timeout=10)
    conn.execute("""
        INSERT INTO trades
            (ts, tier, coin, market_id, token_id, question,
             entry_price, size_usdc, order_id, outcome, fill_price, fill_size,
             ghost_score)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (datetime.now(timezone.utc).isoformat(), tier, coin,
          market_id, token_id, question[:120], entry, size, order_id, outcome,
          fill_price, fill_size, ghost_score))
    conn.commit()
    conn.close()

def get_open_count():
    conn = _db()
    count = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
    conn.close()
    return count

def get_daily_loss():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = _db()
    pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts LIKE ? AND status IN ('won','lost')",
        (f"{today}%",)
    ).fetchone()[0]
    conn.close()
    return -min(pnl, 0)  # Return loss as positive number

def compute_kelly_size(fair_p: float, ask: float, bankroll: float,
                        fraction: float = 0.25, max_size: float = 20.0) -> float:
    """Binary market Kelly criterion sizing.

    f* = (p*b - q) / b   where b = net odds = (1-ask)/ask
    Fractional Kelly: bet = f* × fraction × bankroll

    Args:
        fair_p:   estimated win probability (from compute_arb_score)
        ask:      current ask price (0-1 scale)
        bankroll: total USDC balance
        fraction: Kelly fraction (0.25 = quarter Kelly)
        max_size: absolute USDC cap per bet

    Returns:
        USDC bet size, floored at $1.00, capped at max_size
    """
    if ask <= 0.01 or ask >= 0.99 or bankroll <= 1.0:
        return 1.0  # degenerate price or empty wallet → floor
    b = (1.0 - ask) / ask   # net odds (win $b per $1 risked)
    q = 1.0 - fair_p
    f_star = (fair_p * b - q) / b
    if f_star <= 0:
        return 1.0  # negative Kelly → no edge by this metric → floor
    size = min(f_star * fraction * bankroll, max_size)
    return max(round(size, 2), 1.0)

def get_daily_coin_trades(coin: str) -> int:
    """Count how many trades (any status except open) fired for this coin today."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        conn = _db()
        count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE ts LIKE ? AND coin=? AND status != 'open'",
            (f"{today}%", coin.upper())
        ).fetchone()[0]
        conn.close()
        return int(count)
    except Exception:
        return 0

# ─── CRYPTO KEYWORD FILTERS ───────────────────────────────────────────────────
COINS = {"BTC": ["btc", "bitcoin"],
         "ETH": ["eth", "ethereum"],
         "BNB": ["bnb", "binance coin"],
         "SOL": ["sol", "solana"],
         "XRP": ["xrp", "ripple"]}

def get_coin(question: str) -> Optional[str]:
    q = question.lower()
    for coin, keywords in COINS.items():
        if any(k in q for k in keywords):
            return coin
    return None

def is_crypto_market(question: str) -> bool:
    return get_coin(question) is not None

def classify_tier(question: str) -> Optional[int]:
    """
    T1 (contrarian/Punisher): 15-min, 30-min, 4hr, hourly markets — AGAINST deviation.
        '...Up or Down - 1:00PM-1:15PM ET'  (10 < gap ≤ 90 min)
        '...Up or Down [4hr]' / '[hour]' / hourly keywords
    T2 (oracle-lag snipe): 5-min markets — WITH deviation, last 90s window.
        '...Up or Down - 1:00PM-1:05PM ET'  (gap ≤ 10 min)

    BUG FIX: time-range regex MUST run before month-name check.
    'Bitcoin Up or Down - May 5, 1:20PM-1:25PM ET' contains 'may' → was wrongly
    classified instead of T2. Fix: detect short time windows first.

    RENAME FIX (2026-05-15): was returning 3 for T1 markets. Global rename changed
    all tier==3 comparisons to tier==1 but missed these return literals.
    Now correctly returns 1 for contrarian markets.
    """
    q = question.lower()

    if "up or down" in q or "up-or-down" in q:
        # T1: explicit 4-hour contrarian keywords
        if any(x in q for x in ["[4hr]", "[4h]", "[4-hour]", "4 hour", "4hr",
                                  "4-hour", "four hour"]):
            return 1

        # T2: SHORT TIME-RANGE (≤10 min gap = 5-min stale-ask)
        # T1: MEDIUM TIME-RANGE (10-90 min gap = 15-min/hourly contrarian)
        m2 = re.findall(r'(\d{1,2}):(\d{2})([ap]m)', q)
        if len(m2) >= 2:
            def _mins(h, mn, ap):
                h, mn = int(h), int(mn)
                if ap == "pm" and h != 12: h += 12
                if ap == "am" and h == 12: h = 0
                return h * 60 + mn
            diff = (_mins(*m2[-1]) - _mins(*m2[0])) % (24 * 60)
            if diff <= 10:
                return 2
            elif diff <= 90:
                return 1

        # T1: hourly keywords
        if any(x in q for x in ["[hour]", "[1hr]", "[1h]", ":00pm", ":00am",
                                  "- hour", "hourly"]):
            return 1

    return None  # Not a recognised crypto tier

def parse_mins_from_question(question: str, now: float) -> Optional[float]:
    """Parse estimated minutes to close from question text like
    'ETH above $2,050 on April 18?' or 'BTC Up or Down - April 16, 11:55PM-12:00AM ET'"""
    q = question.lower()
    months = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
              "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
    cur = datetime.fromtimestamp(now, tz=timezone.utc)
    for mname, mnum in months.items():
        if mname not in q:
            continue
        dm = re.search(rf'{mname}[^\d]*(\d{{1,2}})', q)
        if not dm:
            continue
        day = int(dm.group(1))
        for yr in [cur.year, cur.year + 1]:
            try:
                end  = datetime(yr, mnum, day, 23, 59, 59, tzinfo=timezone.utc)
                mins = (end.timestamp() - now) / 60
                if 1 < mins < 43200:  # 1 min to 30 days
                    return round(mins, 1)
            except Exception:
                pass
    return None


# ─── BINANCE PRICE TRACKER ────────────────────────────────────────────────────
class PriceTracker:
    """
    Maintains real-time prices and candle opens via Binance WebSocket.
    Also tracks which strikes have been crossed to avoid duplicate fires.
    """
    def __init__(self):
        self.prices: Dict[str, float] = {}          # e.g. {"BTC": 67500.0}
        self.opens_5m: Dict[str, float] = {}         # 5-min open prices (current candle)
        self.opens_5m_history: Dict[str, List[Tuple[float, float]]] = {}  # coin→[(open_epoch, price)]
        # OHLC per timeframe — high/low used for price-position (market state signal)
        # Calibrated from ghost scanner observations_full_20260508.db:
        #   pos>=0.85 → UP wins 60.6%  |  pos<=0.15 → DOWN wins 60.2%
        self.highs_5m: Dict[str, float] = {}; self.lows_5m: Dict[str, float] = {}
        self.highs_1h: Dict[str, float] = {}; self.lows_1h: Dict[str, float] = {}
        self.highs_4h: Dict[str, float] = {}; self.lows_4h: Dict[str, float] = {}
        self.highs_1d: Dict[str, float] = {}; self.lows_1d: Dict[str, float] = {}
        # Full OHLC history for 5m candles: (open_time_epoch, open, high, low, close)
        self.candles_5m_history: Dict[str, List[Tuple[float,float,float,float,float]]] = {}
        self.opens_1h: Dict[str, float] = {}         # Hourly open prices
        self.opens_4h: Dict[str, float] = {}         # 4hr open prices
        self.opens_1d: Dict[str, float] = {}         # Daily open prices
        self.running = True
        self._ws_connected = False
        # Zombie WS detection: timestamp of last REAL price message (not heartbeat).
        self._last_price_ts: float = time.monotonic()
        # Tick-level history for velocity / momentum / volatility calculations.
        # Stores (monotonic_time, price) tuples; maxlen=150 covers ~2.5 min of ticks.
        _tracked = ("BTC", "ETH", "BNB", "SOL", "XRP")
        self.tick_history: Dict[str, deque] = {c: deque(maxlen=150) for c in _tracked}
        # Punisher WS quality patches (L3/L4) — reset on each reconnect
        self._ws_skip_first: set = set()      # L4: coins whose first post-connect tick has been skipped

    async def fetch_opens(self, session: aiohttp.ClientSession):
        """Fetch current candle opens from Binance REST.
        For T2 oracle-lag direction: we fetch the last 4 five-minute candles so
        check_market can look up the candle that was open AT the market's start time.
        15-minute Polymarket markets span 3 five-minute candles — without history we'd
        compare current price to the WRONG 5m open once the market is >5 min old."""
        symbols = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT", "XRP": "XRPUSDT", "SOL": "SOLUSDT"}
        for coin, sym in symbols.items():
            try:
                url = f"{BINANCE_REST}/klines"

                # 5m candles — last 4 candles (20 min) to cover 15-min T2 market start
                # Binance returns oldest-first; data[-1] is the current forming candle.
                async with session.get(url, params={"symbol": sym, "interval": "5m", "limit": 4},
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data:
                            self.opens_5m[coin] = float(data[-1][1])  # current 5m open
                            self.highs_5m[coin] = float(data[-1][2])  # current 5m high
                            self.lows_5m[coin]  = float(data[-1][3])  # current 5m low
                            # History: [(open_time_epoch_secs, open_price), ...] oldest→newest
                            self.opens_5m_history[coin] = [
                                (float(d[0]) / 1000, float(d[1])) for d in data
                            ]
                            # Full OHLC history for market-range price-position computation
                            self.candles_5m_history[coin] = [
                                (float(d[0])/1000, float(d[1]), float(d[2]),
                                 float(d[3]), float(d[4])) for d in data
                            ]

                # 1h candle — T3/T4 reference
                async with session.get(url, params={"symbol": sym, "interval": "1h", "limit": 1},
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data:
                            self.opens_1h[coin] = float(data[0][1])
                            self.highs_1h[coin] = float(data[0][2])
                            self.lows_1h[coin]  = float(data[0][3])

                # 4h candle — legacy reference
                async with session.get(url, params={"symbol": sym, "interval": "4h", "limit": 1},
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data:
                            self.opens_4h[coin] = float(data[0][1])
                            self.highs_4h[coin] = float(data[0][2])
                            self.lows_4h[coin]  = float(data[0][3])

                # 1d candle — T1 daily reference
                async with session.get(url, params={"symbol": sym, "interval": "1d", "limit": 1},
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data:
                            self.opens_1d[coin] = float(data[0][1])
                            self.highs_1d[coin] = float(data[0][2])
                            self.lows_1d[coin]  = float(data[0][3])

            except Exception as e:
                print(f"[PriceTracker] Open fetch error for {coin}: {e}")

    def get_deviation(self, coin: str, timeframe: str) -> Optional[float]:
        """Return % deviation from candle open. Positive = above open."""
        price = self.prices.get(coin)
        opens = {"5m": self.opens_5m, "1h": self.opens_1h,
                 "4h": self.opens_4h, "1d": self.opens_1d}
        open_price = opens.get(timeframe, {}).get(coin)
        if not price or not open_price:
            return None
        return (price - open_price) / open_price

    def get_velocity(self, coin: str, n: int = 10) -> Optional[float]:
        """Fractional price change per second over the last n ticks.
        Positive = rising, negative = falling. Returns None if insufficient history.
        Example: +0.000004 = BTC moving up ~$0.40/s at $100k."""
        hist = list(self.tick_history.get(coin, []))
        if len(hist) < max(2, n):
            return None
        t0, p0 = hist[-n]
        t1, p1 = hist[-1]
        dt = t1 - t0
        if dt < 0.001 or p0 <= 0:
            return None
        return (p1 - p0) / p0 / dt

    def get_momentum(self, coin: str, n: int = 10) -> Optional[float]:
        """Net directional score over last n ticks: (up_ticks - down_ticks) / total.
        Range -1..+1. +1 = every tick was an up-move. 0 = random walk.
        Returns None if insufficient history."""
        hist = list(self.tick_history.get(coin, []))
        if len(hist) < max(2, n):
            return None
        recent = hist[-n:]
        ups   = sum(1 for i in range(1, len(recent)) if recent[i][1] > recent[i-1][1])
        downs = sum(1 for i in range(1, len(recent)) if recent[i][1] < recent[i-1][1])
        total = ups + downs
        if total == 0:
            return 0.0
        return (ups - downs) / total

    def get_volatility(self, coin: str, n: int = 20) -> Optional[float]:
        """Mean absolute tick-to-tick fractional move over last n ticks.
        Low value = flat/dead market. High value = spiking/noisy market.
        Returns None if insufficient history."""
        hist = list(self.tick_history.get(coin, []))
        if len(hist) < max(2, n):
            return None
        recent = hist[-n:]
        changes = [
            abs(recent[i][1] - recent[i-1][1]) / recent[i-1][1]
            for i in range(1, len(recent))
            if recent[i-1][1] > 0
        ]
        if not changes:
            return None
        return sum(changes) / len(changes)

    def get_deviation_t2(self, coin: str, market_start_ts: float) -> Optional[float]:
        """Deviation of current price vs Binance price AT the market's start time.

        Fixes the systematic direction bug for 15-minute T2 markets:
        - Current 5m candle open changes every 5 min, but the market reference
          price is fixed at market start (e.g., 1:20PM open).
        - At 1:28PM with a 1:20PM-1:35PM market: opens_5m = 1:25PM price.
          If BTC dipped at 1:25PM then recovered, dev(5m) < 0 → scanner buys DOWN,
          but vs the 1:20PM reference the price is UP → should buy UP → systematic loss.

        Uses 5m candle history to find the candle that was open AT market_start_ts
        (the most recent candle whose open_time <= market_start_ts).
        Falls back to opens_5m (current candle) when history is unavailable.
        """
        price = self.prices.get(coin)
        if not price:
            return None
        history = self.opens_5m_history.get(coin, [])
        if not history:
            return self.get_deviation(coin, "5m")   # warmup fallback
        # Walk backwards to find the last candle that opened at or before market start
        ref_price: Optional[float] = None
        for open_ts, open_price in reversed(history):
            if open_ts <= market_start_ts:
                ref_price = open_price
                break
        if ref_price is None:
            ref_price = history[0][1]   # oldest candle available
        if ref_price <= 0:
            return None
        return (price - ref_price) / ref_price

    def get_deviation_15m(self, coin: str) -> Optional[float]:
        """15-minute trend deviation: current price vs the candle open ~15 min ago.

        Punisher confirmation filter: T2 5-minute plays that run AGAINST the
        15-minute trend are the primary bleed-out source. Requiring 15m to agree
        with 5m direction cuts those counter-trend noise fires.

        Uses opens_5m_history (4 candles = 20 min). Takes the oldest available
        candle open (ideally ~15-20 min ago) as the 15m reference.
        Returns None during warmup (<2 candles stored).
        """
        price = self.prices.get(coin)
        if not price:
            return None
        history = self.opens_5m_history.get(coin, [])
        if len(history) < 2:
            return None   # not enough history yet
        # Use candle from ~15 min ago: index -3 if 4 candles, else oldest available
        ref_idx = max(0, len(history) - 3)
        ref_price = history[ref_idx][1]
        if ref_price <= 0:
            return None
        return (price - ref_price) / ref_price

    def get_price_position(self, coin: str, timeframe: str = "5m",
                           market_start_ts: Optional[float] = None) -> Optional[float]:
        """Price position within candle range: 0.0 = at low, 1.0 = at high.

        For T2/T3 time-range markets, market_start_ts aggregates across all
        5-min candles since market open to get the true market-duration range.

        Data source: ghost scanner observations_full_20260508.db (1.46M obs):
          pos >= 0.85  → UP wins 60.6%, DOWN  7.0%
          pos <= 0.15  → UP wins 13.6%, DOWN 60.2%
        """
        price = self.prices.get(coin)
        if not price:
            return None

        if timeframe == "5m" and market_start_ts:
            # Aggregate OHLC across all 5-min candles from market start → now
            cands = self.candles_5m_history.get(coin, [])
            # Include candle that was open at/before market start + all subsequent
            relevant = []
            for cd in cands:
                if cd[0] <= market_start_ts + 5:   # +5s tolerance
                    relevant.append(cd)
                else:
                    break
            # fallback: find at least the opening candle
            if not relevant:
                relevant = [c for c in cands if c[0] <= market_start_ts + 300]
            if relevant:
                h = max(c[2] for c in relevant)
                l = min(c[3] for c in relevant)
            else:
                h = self.highs_5m.get(coin)
                l = self.lows_5m.get(coin)
        else:
            _highs = {"5m": self.highs_5m, "1h": self.highs_1h,
                      "4h": self.highs_4h, "1d": self.highs_1d}
            _lows  = {"5m": self.lows_5m,  "1h": self.lows_1h,
                      "4h": self.lows_4h,  "1d": self.lows_1d}
            h = _highs.get(timeframe, {}).get(coin)
            l = _lows.get(timeframe,  {}).get(coin)

        if not h or not l or h <= l:
            return None
        return min(max((price - l) / (h - l), 0.0), 1.0)

    def get_market_state(self, coin: str, timeframe: str = "5m",
                         market_start_ts: Optional[float] = None) -> str:
        """Classify current market structure into one of four states.

        Derived from ghost scanner observations_full_20260508.db (1.46M observations,
        April–May 2026, 5 coins, 10 days). States and their win rates in the last
        60 seconds before resolution (TRADE_WINDOW, mins_to_close <= 1.0):

          TRENDING_UP   — pos >= 0.75, vel+, mom+:  UP 68.7%  DN  0.9%  (7,330 obs)
          TRENDING_DOWN — pos <= 0.25, vel-, mom-:  UP  2.0%  DN 62.0%  (7,748 obs)
          BOUNCING      — mid range, mixed signals:  UP 44.7%  DN 29.4% (693K obs)
          RANGING       — low pos, weak/flat vel:    UP 15.5%  DN 57.4% (230K obs)
          UNKNOWN       — insufficient data (warmup)

        Hard gate in check_market: TRENDING_UP blocks DOWN fires (0.9% WR),
        TRENDING_DOWN blocks UP fires (2.0% WR). Prevents worst wrong-direction trades.
        """
        pos = self.get_price_position(coin, timeframe, market_start_ts)
        vel = self.get_velocity(coin, n=10)
        mom = self.get_momentum(coin, n=10)

        if pos is None:
            return "UNKNOWN"

        vel_up = vel is not None and vel > 0
        vel_dn = vel is not None and vel < 0
        mom_up = mom is not None and mom >= 0.3
        mom_dn = mom is not None and mom <= -0.3

        # TRENDING: price near extreme + directional velocity/momentum confirming
        if pos >= 0.75 and (vel_up or mom_up) and not (vel_dn and mom_dn):
            return "TRENDING_UP"
        if pos <= 0.25 and (vel_dn or mom_dn) and not (vel_up and mom_up):
            return "TRENDING_DOWN"
        # RANGING: price hugging the bottom without confirmed downtrend signal
        if pos < 0.35:
            return "RANGING"
        # BOUNCING: mid-range, no clear trend
        return "BOUNCING"

    async def run_websocket(self):
        """Maintain Binance WebSocket connection with auto-reconnect"""
        import ssl as _ssl
        _ssl_ctx = _ssl.create_default_context()  # proper SSL — avoids handshake delay on Windows
        coin_map = {"btcusdt": "BTC", "ethusdt": "ETH", "bnbusdt": "BNB", "xrpusdt": "XRP", "solusdt": "SOL"}
        while self.running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        BINANCE_WS_URL,
                        heartbeat=20,                              # was 30 — detect drops faster
                        timeout=aiohttp.ClientWSTimeout(ws_close=10),
                        ssl=_ssl_ctx
                    ) as ws:
                        self._ws_connected = True
                        self._last_price_ts = time.monotonic()  # reset stall clock on connect
                        self._ws_skip_first = set()             # L4: first-tick skip — clear on reconnect
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"[{ts}] Binance WebSocket connected ✓")
                        # ── Zombie-safe recv loop (research: oracle-lag-sniper) ──────────
                        # async for msg in ws: never times out — zombie connections
                        # (alive WS, no real data) kill the bot silently for 20+ min.
                        # Use explicit recv with timeout; stall check on TimeoutError.
                        while self.running:
                            try:
                                msg = await asyncio.wait_for(ws.receive(), timeout=10.0)
                            except asyncio.TimeoutError:
                                since = time.monotonic() - self._last_price_ts
                                if since > WS_STALL_TIMEOUT:
                                    _ts2 = datetime.now().strftime("%H:%M:%S")
                                    print(f"[{_ts2}] [WS] 🧟 ZOMBIE detected "
                                          f"({since:.0f}s no data) — forcing reconnect")
                                    break
                                continue  # heartbeat-only stall, keep waiting
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                    stream = data.get("stream", "")
                                    ticker = data.get("data", {})
                                    sym = stream.split("@")[0]
                                    coin = coin_map.get(sym)
                                    if coin and ticker:
                                        # L4 — first-tick skip (Punisher): first message after
                                        # a new WS connection is almost always a stale cached
                                        # snapshot, not live data — discard it silently.
                                        if coin not in self._ws_skip_first:
                                            self._ws_skip_first.add(coin)
                                            continue
                                        _p = float(ticker.get("c", 0))
                                        if _p > 0:
                                            self._last_price_ts = time.monotonic()  # zombie reset
                                            _prev_p = self.prices.get(coin, 0.0)
                                            # L3 — stale-tick guard (Punisher): if the new tick
                                            # jumps >0.5% from the last valid price it's almost
                                            # certainly a delayed cache flush, not real movement.
                                            # Uses relative % threshold (not absolute $) so it works
                                            # correctly for BTC ($81k), ETH ($2k), SOL, XRP alike.
                                            # 0.5% = $408 on BTC, $11.55 on ETH — catches CDN
                                            # artifacts while passing normal tick-to-tick movement.
                                            if _prev_p > 0 and abs(_p - _prev_p) / _prev_p > 0.005:
                                                _ts3 = datetime.now().strftime("%H:%M:%S")
                                                _pct3 = abs(_p - _prev_p) / _prev_p * 100
                                                print(f"[{_ts3}] [WS] ⚠ STALE TICK REJECTED "
                                                      f"{coin} {_prev_p:.4f}→{_p:.4f} "
                                                      f"(Δ${abs(_p - _prev_p):.2f} / {_pct3:.3f}%)")
                                                continue
                                            self.prices[coin] = _p
                                            LATENCY_TRACKER.on_tick(coin, _p, _prev_p)
                                            # F1 tick-trigger: wake scan loop immediately on significant move
                                            if (TICK_TRIGGER_ENABLED and _TICK_SCAN_EVENT is not None
                                                    and _prev_p > 0):
                                                _tick_mag = abs(_p - _prev_p) / _prev_p
                                                if _tick_mag >= TICK_TRIGGER_MAG:
                                                    _TICK_SCAN_EVENT.set()
                                            # Append to tick history for velocity/momentum/volatility
                                            if coin in self.tick_history:
                                                self.tick_history[coin].append(
                                                    (time.monotonic(), _p))
                                except Exception:
                                    pass
                            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                             aiohttp.WSMsgType.ERROR):
                                break
            except Exception as e:
                self._ws_connected = False
                print(f"[PriceTracker] WS error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)

    def check_ws_quality(self, coin: str, window_secs: float = 15.0,
                         min_ticks: int = 3, max_jump: float = 0.05) -> bool:
        """Punisher L1 warmup gate: verify live tick quality before trusting direction.

        Inspects the last `window_secs` of tick_history for `coin` and requires:
          - At least `min_ticks` ticks received in that window  (data is arriving)
          - No single tick-to-tick jump > `max_jump` dollars     (no stale snapshots)

        Returns True  = quality OK, safe to trade this window.
        Returns False = skip — warmup data is thin or spiked.
        Fails OPEN during startup warmup (< min_ticks ever recorded) so the bot
        doesn't sit silent forever on first launch.
        """
        history = self.tick_history.get(coin)
        if not history:
            return True   # fail-open: no history at all = startup, let through
        now    = time.monotonic()
        cutoff = now - window_secs
        recent = [(t, p) for t, p in history if t >= cutoff]
        if len(recent) < min_ticks:
            # Fail-open if tick_history has plenty of entries but they're all old —
            # that means we're outside trading hours; don't block on that.
            if len(history) >= min_ticks:
                return True
            return False  # truly too few ticks ever — data not flowing yet
        prices = [p for _, p in recent]
        for i in range(1, len(prices)):
            if abs(prices[i] - prices[i - 1]) > max_jump:
                return False  # spike detected in window
        return True

# ─── STRIKE MARKET PRELOADER ──────────────────────────────────────────────────

async def _fetch_orderbook_raw(session: aiohttp.ClientSession,
                               token_id: str) -> Optional[dict]:
    """Live HTTP fetch — no cache. Used for T1 (latency-critical) path."""
    try:
        url = f"{POLYMARKET_HOST}/book"
        async with session.get(url, params={"token_id": token_id},
                               timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        pass
    return None


async def get_orderbook(session: aiohttp.ClientSession,
                        token_id: str,
                        max_age_ms: int = 500) -> Optional[dict]:
    """
    Cache-aware orderbook fetch. Returns a cached book if it's fresher
    than `max_age_ms`; otherwise hits the API and caches the result.
    Set max_age_ms=0 to bypass the cache (T1 strike scan does this).
    """
    if max_age_ms > 0 and PREWARM_ORDERBOOKS:
        cached = _orderbook_cache.get(token_id)
        if cached:
            ts, book = cached
            if (time.time() - ts) * 1000 < max_age_ms and book:
                return book
    book = await _fetch_orderbook_raw(session, token_id)
    if book is not None:
        _orderbook_cache[token_id] = (time.time(), book)
    return book


async def prewarm_orderbook(session: aiohttp.ClientSession, token_id: str):
    """Background task: fetch + cache an orderbook for a market about
    to enter its fire window. Silent on failure."""
    try:
        await get_orderbook(session, token_id, max_age_ms=0)
    except Exception:
        pass


async def batch_prewarm_orderbooks(session: aiohttp.ClientSession,
                                   token_ids: List[str]) -> int:
    """Fetch all orderbooks in ONE HTTP call via POST /books (AlterEgo tip).

    Replaces N individual GET /book calls with a single round-trip.
    Populates _orderbook_cache for every token_id in the list.
    Returns the number of books successfully cached.

    POST clob.polymarket.com/books
    Body: {"token_ids": [...]}
    Response: list of book objects, each with token_id + bids/asks.
    """
    if not token_ids:
        return 0
    try:
        url = f"{POLYMARKET_HOST}/books"
        async with session.post(
            url,
            json={"token_ids": token_ids},
            timeout=aiohttp.ClientTimeout(total=6),
        ) as r:
            if r.status != 200:
                return 0
            data = await r.json()
            if not isinstance(data, list):
                return 0
            now = time.time()
            cached = 0
            for book in data:
                if not isinstance(book, dict):
                    continue
                tid = (book.get("asset_id") or book.get("token_id")
                       or book.get("market") or "")
                if not tid:
                    continue
                _orderbook_cache[str(tid)] = (now, {
                    "bids": book.get("bids", []) or [],
                    "asks": book.get("asks", []) or [],
                })
                cached += 1
            return cached
    except Exception:
        return 0


# ── Coinbase oracle direction helper (2026-05-09) ─────────────────────────────
# Returns "UP", "DOWN", "TIE", or "UNKNOWN" (on any error → fail-open).
# Uses the public Coinbase Exchange REST API — no API key required.
# Coin map: BTC→BTC-USD, ETH→ETH-USD, SOL→SOL-USD, XRP→XRP-USD, BNB→BNB-USDT
_CB_COIN_MAP = {
    "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
    "XRP": "XRP-USD", "BNB": "BNB-USDT",
}
_cb_dir_cache: dict = {}  # coin -> (monotonic_ts, result_str)
_CB_DIR_TTL = 25.0  # reuse result for 25s (Coinbase candles refresh ~60s)

async def _get_coinbase_direction(session: aiohttp.ClientSession,
                                  coin: str) -> str:
    """
    Fetch Coinbase 5m candle and return directional read:
      "UP"   — close > open by >= CB_ORACLE_TIE_PCT
      "DOWN" — close < open by >= CB_ORACLE_TIE_PCT
      "TIE"  — move too small to call (< CB_ORACLE_TIE_PCT)
      "UNKNOWN" — API error / timeout (caller treats as pass-through)
    """
    coin_u = (coin or "").upper()
    cached = _cb_dir_cache.get(coin_u)
    if cached:
        age = time.monotonic() - cached[0]
        if age < _CB_DIR_TTL:
            return cached[1]
    pair = _CB_COIN_MAP.get(coin_u)
    if not pair:
        return "UNKNOWN"
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles"
    try:
        async with session.get(
            url,
            params={"granularity": "300", "limit": "2"},
            timeout=aiohttp.ClientTimeout(total=3.0),
        ) as resp:
            if resp.status != 200:
                return "UNKNOWN"
            data = await resp.json()
            # Response: [[ts, low, high, open, close, volume], ...]
            # Index 0 = most recent (may still be forming). Index 1 = last closed.
            # Use index 1 (last fully closed 5m candle) for a stable signal.
            if not data or len(data) < 2:
                return "UNKNOWN"
            candle = data[1]  # [ts, low, high, open, close, volume]
            open_p, close_p = float(candle[3]), float(candle[4])
            if open_p <= 0:
                return "UNKNOWN"
            pct = (close_p - open_p) / open_p * 100.0
            if abs(pct) < CB_ORACLE_TIE_PCT:
                result = "TIE"
            elif pct > 0:
                result = "UP"
            else:
                result = "DOWN"
            _cb_dir_cache[coin_u] = (time.monotonic(), result)
            return result
    except Exception:
        return "UNKNOWN"


# ─── v10: WebSocket orderbook subscription ────────────────────────────────────
# Subscribe to Polymarket CLOB live order-book stream. When a market the
# bot is tracking has its orderbook update, PM pushes the new state to us
# in ~50ms. We write it straight into _orderbook_cache; the next scan tick
# reads cache and fires immediately. Net effect: ~10x faster reaction time
# vs the 1-second polling loop. Falls back to HTTP polling if WS fails.

WS_ENABLED        = os.getenv("WS_ENABLED", "true").strip().lower() in ("true", "1", "yes")
WS_URL            = os.getenv("WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
PREWARM_ORDERBOOKS = os.getenv("PREWARM_ORDERBOOKS", "true").strip().lower() in ("true", "1", "yes")


class _OrderbookWS:
    """Persistent WebSocket client. Auto-reconnects. Subscribes to
    token_ids on demand. Pushes book snapshots into _orderbook_cache."""

    def __init__(self):
        self.subscribed: set = set()  # token_ids we've asked PM to push
        self.connected = False
        self.ws       = None
        self._sess    = None
        self.task     = None
        self.msgs_received = 0
        self.last_msg_ts   = 0.0
        # Layer 4: first-tick skip — first book msg per token is always a
        #          cached snapshot (not live). Drop it, accept all after.
        self._first_tick_seen: set = set()
        # Layer 3: stale tick guard — reject snapshots where best ask moved
        #          >$15 vs warmup price (corrupted / mis-sequenced snapshot).
        self._warmup_price: dict = {}   # token_id -> float (first accepted ask)

    async def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        import ssl as _ssl
        _ssl_ctx = _ssl.create_default_context()  # proper SSL — fixes silent fail on Windows
        backoff = 1
        while True:
            try:
                self._sess = aiohttp.ClientSession()
                async with self._sess.ws_connect(
                    WS_URL,
                    heartbeat=20,
                    timeout=aiohttp.ClientTimeout(total=15),
                    ssl=_ssl_ctx,
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    print(f"[WS] Connected to {WS_URL}")
                    backoff = 1
                    # Layer 4+3: reset per-connection tick state on every
                    # (re)connect — new connection = new first-tick window.
                    self._first_tick_seen.clear()
                    self._warmup_price.clear()
                    # Re-subscribe to everything we had
                    if self.subscribed:
                        await self._send_subscribe(list(self.subscribed))
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_msg(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.CLOSING,
                                          aiohttp.WSMsgType.ERROR):
                            break
            except Exception as e:
                print(f"[WS] connection error: {type(e).__name__}: {e}")
            finally:
                self.connected = False
                self.ws = None
                if self._sess and not self._sess.closed:
                    await self._sess.close()
                self._sess = None
            # Reconnect with exponential backoff (capped)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def subscribe(self, token_ids: List[str]):
        """Request push updates for these token_ids. Idempotent."""
        new = [t for t in token_ids if t and t not in self.subscribed]
        if not new:
            return
        self.subscribed.update(new)
        if self.connected and self.ws and not self.ws.closed:
            await self._send_subscribe(new)

    async def _send_subscribe(self, token_ids: List[str]):
        # Polymarket CLOB market channel subscribe payload.
        # Try the documented format; if PM rejects, future messages will
        # surface the error and we'll iterate.
        payload = {"type": "market", "assets_ids": token_ids}  # PM requires lowercase
        try:
            await self.ws.send_json(payload)
            print(f"[WS] subscribed to {len(token_ids)} token_ids "
                  f"(total: {len(self.subscribed)})")
        except Exception as e:
            print(f"[WS] subscribe send failed: {e}")

    def _handle_msg(self, raw: str):
        """Convert PM book / price_change events into _orderbook_cache rows."""
        self.msgs_received += 1
        self.last_msg_ts = time.time()
        try:
            obj = json.loads(raw)
        except Exception:
            return

        events = obj if isinstance(obj, list) else [obj]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            ev_type = (ev.get("event_type") or ev.get("type") or "").lower()
            asset_id = ev.get("asset_id") or ev.get("asset") or ev.get("token_id")
            if not asset_id:
                continue

            if ev_type == "book":
                # ── Layer 4: first-tick skip ─────────────────────────────
                # The very first book snapshot on a fresh WS connection is
                # always a cached/stale state from PM's edge servers. Drop
                # it so the bot never prices off stale data. (Punisher L4)
                _tid = str(asset_id)
                if _tid not in self._first_tick_seen:
                    self._first_tick_seen.add(_tid)
                    continue  # discard the snapshot; wait for live tick

                # Full orderbook snapshot
                book = {
                    "bids": ev.get("bids", []) or [],
                    "asks": ev.get("asks", []) or [],
                }

                # ── Layer 3: stale tick guard ────────────────────────────
                # Reject any snapshot whose best ask differs from warmup
                # price by >$15. That size jump means corrupted/mis-sequenced
                # data — writing it would corrupt _orderbook_cache. (Punisher L3)
                _new_ask = None
                if book["asks"]:
                    try:
                        _new_ask = float(min(book["asks"],
                                             key=lambda x: float(x.get("price", x) if isinstance(x, dict) else x)
                                             ).get("price") if isinstance(book["asks"][0], dict)
                                         else min(float(a) for a in book["asks"] if a))
                    except Exception:
                        _new_ask = None
                if _new_ask is not None:
                    _warm = self._warmup_price.get(_tid)
                    if _warm is None:
                        # First accepted tick — record as warmup baseline
                        self._warmup_price[_tid] = _new_ask
                    elif abs(_new_ask - _warm) > 0.70:
                        # Polymarket token prices are 0–1 scale. >0.70 (70%) between
                        # warmup and current ask is a CDN stale snapshot.
                        # Must be high: markets legitimately move 0.15–0.50 as they resolve.
                        # 0.15 was too tight (fired on every near-resolution market).
                        print(f"[WS] STALE TICK REJECTED {_tid[:12]}… "
                              f"delta=${abs(_new_ask-_warm):.4f} "
                              f"(warmup={_warm:.4f} new={_new_ask:.4f})")
                        continue  # discard corrupted snapshot

                _orderbook_cache[_tid] = (time.time(), book)
            elif ev_type in ("price_change", "tick_size_change"):
                # Incremental update — invalidate cache, next scan refetches
                # via HTTP (still faster than polling because HTTP only when
                # the book actually changed)
                cur = _orderbook_cache.get(str(asset_id))
                if cur:
                    _orderbook_cache[str(asset_id)] = (0.0, cur[1])


WS_CLIENT = _OrderbookWS()

# ── Position poll interval (fallback when user-WS is unavailable) ─────────────
POSITION_POLL_INTERVAL = float(os.getenv("POSITION_POLL_INTERVAL", "5"))

class _UserWS:
    """Polymarket user-channel WebSocket.

    Subscribes to the user's trade events at
    wss://ws-subscriptions-clob.polymarket.com/ws/user using the existing
    CLOB API credentials from .env.

    On every matched-trade event, sets `fill_event` so
    `refresh_positions_periodically` can call `check_positions` immediately
    instead of waiting for the REST poll timeout.

    If credentials are missing or the connection fails, the class degrades
    gracefully — `refresh_positions_periodically` falls back to polling every
    POSITION_POLL_INTERVAL seconds without any code-path changes.
    """

    _USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

    def __init__(self):
        self.connected  = False
        self.fill_event = asyncio.Event()   # set on any trade fill; cleared after consumed
        self._task: Optional[asyncio.Task] = None

    def start(self):
        """Create the background WebSocket task (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        _key  = os.getenv("API_KEY",        os.getenv("CLOB_API_KEY",   ""))
        _sec  = os.getenv("API_SECRET",     os.getenv("CLOB_SECRET",    ""))
        _pass = os.getenv("API_PASSPHRASE", os.getenv("CLOB_PASSPHRASE",""))

        if not all([FUNDER_ADDRESS, _key, _sec, _pass]):
            print("[UserWS] Credentials not set — user channel disabled; "
                  f"falling back to {POSITION_POLL_INTERVAL:.0f}s REST poll")
            return

        backoff = 2
        while True:
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(
                        self._USER_WS_URL,
                        heartbeat=20,
                        timeout=aiohttp.ClientTimeout(total=15),
                        ssl=False,
                    ) as ws:
                        self.connected = True
                        # Subscribe: authenticate on the user channel
                        await ws.send_json({
                            "type": "User",
                            "user": FUNDER_ADDRESS,
                            "auth": {
                                "apiKey":     _key,
                                "secret":     _sec,
                                "passphrase": _pass,
                            },
                        })
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"[{ts}] [UserWS] Connected — monitoring fills "
                              f"for {FUNDER_ADDRESS[:12]}...")
                        backoff = 2  # reset on successful connect

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    payload = json.loads(msg.data)
                                    events  = payload if isinstance(payload, list) else [payload]
                                    for evt in events:
                                        etype = (
                                            evt.get("event_type") or
                                            evt.get("type") or ""
                                        ).lower()
                                        if etype in ("trade", "order_filled"):
                                            asset = str(evt.get("asset_id", ""))[:12]
                                            price = evt.get("price", "?")
                                            size  = evt.get("size",  "?")
                                            _ts   = datetime.now().strftime("%H:%M:%S")
                                            print(f"[{_ts}] [UserWS] Fill: "
                                                  f"asset={asset}... "
                                                  f"price={price} size={size}")
                                            self.fill_event.set()  # wake position checker
                                except Exception:
                                    pass
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break

            except Exception as e:
                print(f"[UserWS] error: {type(e).__name__}: {e}")
            finally:
                self.connected = False

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


USER_WS = _UserWS()    # module-level singleton; started inside CryptoGhostScanner.run()

def book_depth_imbalance(book: dict) -> float:
    """Compute (bid_total_usdc - ask_total_usdc) / (bid_total_usdc + ask_total_usdc).
    Range: -1.0 (all asks, no bids) to +1.0 (all bids, no asks). 0.0 when balanced.
    Positive = buy pressure dominant. Negative = sell pressure dominant.
    Source: trentmkelly tick data — UP-resolving mkts avg +0.19; DOWN avg -0.13.
    Returns 0.0 when book is empty or totals are zero.
    """
    bid_total = sum(float(l.get("size", 0)) * float(l.get("price", 0))
                    for l in book.get("bids", []))
    ask_total = sum(float(l.get("size", 0)) * float(l.get("price", 0))
                    for l in book.get("asks", []))
    total = bid_total + ask_total
    return (bid_total - ask_total) / total if total > 0 else 0.0


def best_ask_info(book: dict, max_price: float = 1.0) -> Tuple[Optional[float], float]:
    """Returns (best_ask_price, cumulative_usdc_liquidity_at_prices_≤_max_price).

    Walks all ask levels in ascending price order and sums the USDC available
    at each level whose price ≤ max_price.  Pass the tier's entry-price ceiling
    as max_price so the returned liquidity figure represents only depth a market
    FOK order would actually consume — i.e. depth we can afford to fill at.

    Why this matters:
      Old: returned only top-of-book level (price × size).  A book with $0.30
           at $0.010 and $2.70 at $0.025 reported $0.30 liquidity, triggering
           the MIN_LIQUIDITY skip even though $3.00 was fully fillable.
      New: sums all levels ≤ max_price → reports $3.00 → trade fires correctly.
           Conversely, a book with only $0.30 total at ≤ max_price correctly
           blocks the trade and prevents a $0.30 partial fill on a $3 bet.

    Returns (best_ask, 0.0) when no ask levels are ≤ max_price (no fillable depth).
    Returns (None, 0.0) when the book has no asks at all.
    """
    asks = book.get("asks", [])
    if not asks:
        return None, 0.0
    sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 1)))
    best_price  = float(sorted_asks[0].get("price", 1))

    cum_liq = 0.0
    for level in sorted_asks:
        p = float(level.get("price", 1))
        s = float(level.get("size",  0))
        if p > max_price:
            break           # stop — beyond our price ceiling
        cum_liq += p * s    # USDC available at this level

    return best_price, cum_liq

# ─── TIME WINDOW CHECKS ───────────────────────────────────────────────────────
def minutes_to_close(end_ts_str: str) -> Optional[float]:
    """Return minutes until market closes. Negative = already closed."""
    try:
        end = datetime.fromisoformat(end_ts_str.replace("Z", "+00:00"))
        delta = (end - datetime.now(timezone.utc)).total_seconds() / 60
        return delta
    except Exception:
        return None

def in_entry_window(tier: int, mins_left: float) -> bool:
    """Is this the right time window to enter this tier?"""
    if mins_left <= 0:
        return False
    if tier == 1:
        return mins_left <= T1_WINDOW_MIN   # 4hr contrarian: last T1_WINDOW_MIN min
    if tier == 2:
        return 0 < mins_left <= T2_WINDOW_MIN
    return False

def _adap_update(fired_this_cycle: int) -> None:
    """Call once per scan cycle. Updates drought state and relaxes/restores thresholds."""
    global _adap_last_fire_ts, _adap_in_drought
    now = time.time()
    if fired_this_cycle > 0:
        _adap_last_fire_ts = now
        _adap_in_drought   = False
        return
    if _adap_last_fire_ts == 0.0:
        _adap_last_fire_ts = now  # initialise on first cycle
        return
    if (now - _adap_last_fire_ts) >= ADAP_DROUGHT_SECS:
        _adap_in_drought = True


def _adap_eff_conf() -> float:
    """Effective confidence minimum — lowered by ADAP_CONF_RELAX during drought."""
    if _adap_in_drought:
        return max(ADAP_CONF_FLOOR, CONFIDENCE_MIN - ADAP_CONF_RELAX)
    return CONFIDENCE_MIN


def _adap_eff_mismatch() -> float:
    """Effective KILL_MISMATCH_MIN — relaxed slightly during drought."""
    if _adap_in_drought:
        return max(0.20, KILL_MISMATCH_MIN - ADAP_MISMATCH_RELAX)
    return KILL_MISMATCH_MIN


def _adap_drought_mins() -> float:
    """Minutes since last fire (0 if never fired or not in drought)."""
    if _adap_last_fire_ts == 0.0:
        return 0.0
    return (time.time() - _adap_last_fire_ts) / 60.0


def compute_confidence(tier: int, dev: float, liq: float, intended_size: float,
                       ask: float, max_e: float, secs: float, mins: float,
                       fair_p: float = 0.5,
                       velocity: Optional[float] = None) -> float:
    """6-component composite signal quality score in [0.0, 1.0].

    Weights: mismatch(0.35) + dev(0.20) + liq(0.15) + time(0.15)
             + price(0.10) + freshness(0.05) | MICRO-LAG ARB strategy

    dev_score      — abs(dev) / tier_min.  At or above min = 1.0.
    mismatch_score — THE EDGE. |fair_p − ask| / 0.05.  5pp gap = 1.0.
                     Measures how badly market underprices the Gaussian model.
    liq_score      — depth / bet_size.  Full fill = 1.0, partial risk < 1.
    price_score    — 1 − ask/max_entry.  Cheaper token (larger payout) = 1.0.
    time_score     — proximity to resolution inside the entry window.
    momentum_score — abs(velocity) / (5 × TREND_VELOCITY_MIN).  Active = 1.0.
    """
    # ── 1. Deviation magnitude ───────────────────────────────────────────────
    _dev_min  = (T1_MIN_DEV if tier == 1 else T2_MIN_DEV)
    dev_score = min(abs(dev) / _dev_min, 1.0) if _dev_min > 0 else 0.5

    # ── 2. Mismatch score — inverted bell curve (subtle gap = best) ─────────
    # Backtest: winners have 3.4× lower oracle discrepancy than losers.
    # Large gap = oracle likely already repriced → penalise above MISMATCH_CAP.
    # Shape: linear rise [0 → cap], then declines by PENALTY per unit above cap.
    # MISMATCH_CAP=0.0 disables inversion (reverts to legacy linear).
    _raw_mismatch = min(abs(fair_p - ask) / max(1.0 - ask, 0.01), 1.0)
    if MISMATCH_CAP > 0 and _raw_mismatch > MISMATCH_CAP:
        mismatch_score = max(
            MISMATCH_CAP - (_raw_mismatch - MISMATCH_CAP) * MISMATCH_PENALTY_SCALE,
            MISMATCH_FLOOR,
        )
    else:
        mismatch_score = _raw_mismatch

    # ── 3. Liquidity quality ─────────────────────────────────────────────────
    # 1× depth (exactly enough to fill the bet) = 1.0. Partial risk < 1.0.
    # Full score at LIQ_MULT coverage (1.2x bet size). Partial risk < 1.0.
    liq_score = min(liq / max(intended_size * LIQ_MULT, 0.01), 1.0)

    # ── 4. Price discount ────────────────────────────────────────────────────
    # Cheaper ask vs entry ceiling = larger implied payout. At ceiling = 0.0.
    price_score = max(0.0, 1.0 - (ask / max_e)) if max_e > 0 else 0.0

    # ── 5. Time proximity to resolution ──────────────────────────────────────
    if tier == 2:
        # Bell-curve: ramp up before sweet spot, peak inside it, decline after
        _progress = 1.0 - (secs / T2_CANDLE_SECS) if T2_CANDLE_SECS > 0 else 0.5
        if _progress < CANDLE_SWEET_LO:
            time_score = _progress / max(CANDLE_SWEET_LO, 0.001)  # ramp up to sweet zone
        elif _progress <= CANDLE_SWEET_HI:
            time_score = 1.0  # sweet spot — full score
        else:
            # decline from 1.0 at sweet_hi toward 0 at progress_max
            time_score = max(0.0, 1.0 - (_progress - CANDLE_SWEET_HI) /
                             max(CANDLE_PROGRESS_MAX - CANDLE_SWEET_HI, 0.01))
    else:  # T3 4hr contrarian
        time_score = max(0.0, 1.0 - mins / T1_WINDOW_MIN) if T1_WINDOW_MIN > 0 else 0.5

    # ── 6. Momentum — is price still actively moving? ────────────────────────
    # Micro-lag arb: LOW velocity = BTC just ticked, oracle not yet updated = BEST.
    # HIGH velocity = BTC screaming for seconds = market makers already repriced.
    # Score 1.0 at vel <= TREND_VELOCITY_MIN (just started). 0.0 at 3x (full run).
    if velocity is not None and TREND_VELOCITY_MIN > 0:
        momentum_score = max(0.0, 1.0 - abs(velocity) / (TREND_VELOCITY_MIN * 3.0))
    else:
        momentum_score = 0.8  # unknown = assume fresh (startup / no tick history yet)

    # ── Weighted composite ────────────────────────────────────────────────────
    w_total = (CONF_WEIGHT_DEV + CONF_WEIGHT_MISMATCH + CONF_WEIGHT_LIQ +
               CONF_WEIGHT_PRICE + CONF_WEIGHT_TIME + CONF_WEIGHT_MOM)
    if w_total <= 0:
        return 0.0
    score = (
        CONF_WEIGHT_DEV      * dev_score      +
        CONF_WEIGHT_MISMATCH * mismatch_score +
        CONF_WEIGHT_LIQ      * liq_score      +
        CONF_WEIGHT_PRICE    * price_score    +
        CONF_WEIGHT_TIME     * time_score     +
        CONF_WEIGHT_MOM      * momentum_score
    ) / w_total
    return round(min(max(score, 0.0), 1.0), 4)

def compute_arb_score(
    coin: str,
    dev: float,
    ask: float,
    secs: float,
    tier: int,
    velocity: Optional[float] = None,
    buying_up: bool = True,
) -> tuple:
    """Oracle-arbitrage probability engine.

    Converts Binance price deviation into an expected UP/DOWN probability using
    Gaussian diffusion (normal CDF), then measures how far the market's ask
    price (implied probability) lags behind that fair value.

    Model:  fair_p = Φ( |dev| / (σ · √(secs / window_secs)) )

    where σ is per-coin 5-min empirical volatility and window_secs is the full
    market window (300 s T2 · 3600 s T3 · 86400 s T4).

    Returns:
        (fair_p, mismatch, arb_score)
        fair_p    — model probability [0.5, 1.0] that our side resolves YES
        mismatch  — fair_p − ask  (how much the market underprices the edge)
        arb_score — composite: mismatch × recency_boost × vel_boost  [0..1]
    """
    from math import erf, sqrt as _sqrt

    sigma       = COIN_VOL_5M.get(coin.upper(), 0.0020)
    # T2=5-min snap (300s), T3=15-min window (900s), T4=1H candle (3600s), T1=1D (86400s)
    window_secs = (300.0   if tier == 2 else
                   900.0   if tier == 1 else
                   3600.0  if tier == 4 else
                   86400.0)   # T1 daily
    time_frac   = max(secs / window_secs, 0.005)   # floor avoids div/0 at expiry
    sigma_adj   = sigma * _sqrt(time_frac)

    if sigma_adj > 0 and abs(dev) > 0:
        z      = abs(dev) / sigma_adj
        fair_p = 0.5 * (1.0 + erf(z / _sqrt(2.0)))
    else:
        fair_p = 0.5

    fair_p   = min(max(fair_p, 0.5), 0.9999)

    # Anti-longshot calibration: cheap tokens (<THRESHOLD) are structurally
    # underpriced on Polymarket — empirically win ~40% more than priced.
    # Boost fair_p slightly to reflect true probability. Conservative ×1.15 default.
    if ANTI_LONGSHOT_ENABLED and ask < ANTI_LONGSHOT_THRESHOLD:
        # UP tokens: +4% bull-market base-rate edge (×1.15 default)
        # DOWN tokens: -4% headwind from bull-market bias (×1.05 default)
        _mult = ANTI_LONGSHOT_MULT if buying_up else ANTI_LONGSHOT_MULT_DOWN
        fair_p = min(fair_p * _mult, 0.9999)

    mismatch = max(0.0, fair_p - ask)

    if not ARB_ENABLED or mismatch <= 0.0:
        return round(fair_p, 4), round(mismatch, 4), 0.0

    # Recency boost — signals in final ARB_RECENCY_WINDOW seconds get ×boost
    recency = ARB_RECENCY_BOOST if secs <= ARB_RECENCY_WINDOW else 1.0

    # Velocity boost — faster price movement = fresher oracle lag = bigger edge
    vel_boost = 1.0
    if velocity is not None and ARB_VEL_BOOST_SCALE > 0:
        vel_boost = 1.0 + min(abs(velocity) / ARB_VEL_BOOST_SCALE, 1.0)

    arb_score = min(mismatch * recency * vel_boost, 1.0)
    return round(fair_p, 4), round(mismatch, 4), round(arb_score, 4)


def get_dynamic_size(base_size: float, confidence: float, arb_score: float) -> float:
    """Scale position size by composite signal quality.

    Quality = confidence × (1 − DYN_ARB_BLEND) + arb_score × DYN_ARB_BLEND

    Piecewise-linear scaling:
      quality < DYN_THRESH_LOW  → DYN_MULT_LOW × base  (e.g. 0.5×)
      LOW..HIGH                 → smooth ramp from DYN_MULT_LOW to 1.0×
      quality ≥ DYN_THRESH_HIGH → ramp from 1.0× to DYN_MULT_ELITE (e.g. 2.0×)

    Always floors at $1.00 (Polymarket minimum order).
    """
    if not DYNAMIC_SIZING:
        return base_size

    quality = confidence * (1.0 - DYN_ARB_BLEND) + arb_score * DYN_ARB_BLEND

    lo, hi = DYN_THRESH_LOW, DYN_THRESH_HIGH
    if quality < lo:
        mult = DYN_MULT_LOW
    elif quality < hi:
        # Smooth interpolation from DYN_MULT_LOW to 1.0 across the middle band
        t    = (quality - lo) / max(hi - lo, 1e-6)
        mult = DYN_MULT_LOW + t * (1.0 - DYN_MULT_LOW)
    else:
        # Elite zone: ramp from 1.0× to DYN_MULT_ELITE
        t    = min((quality - hi) / max(1.0 - hi, 1e-6), 1.0)
        mult = 1.0 + t * (DYN_MULT_ELITE - 1.0)

    result = round(base_size * mult, 2)
    return max(result, 1.0)   # Polymarket minimum order floor


class HourBiasEngine:
    """
    Ghost Memory — adaptive hour-of-day directional bias engine.

    Improvements over v1:
      • Rolling lookback window (HOUR_BIAS_LOOKBACK_DAYS, default 30d) instead
        of hardcoded '2026-05-01' — adapts to any run length automatically.
      • Recency weighting: trades in the last HOUR_BIAS_RECENT_DAYS (default 7)
        count HOUR_BIAS_RECENT_WEIGHT × (default 2×) more than older trades.
      • Per-(coin, hour) tracking in _coin_bias alongside global _bias.
        should_skip() checks coin-specific bias first; falls back to global if
        the coin bucket lacks enough samples.
      • Lower MIN_SAMPLES default (5) and REFRESH_MIN (10) so learning starts
        faster and adapts quicker to intraday shifts.
    """
    def __init__(self):
        # global: bias[utc_hour] = {"up_n": float, "up_w": float, "dn_n": float, "dn_w": float}
        self._bias: dict = {}
        # per-coin: _coin_bias[(coin_upper, utc_hour)] = same dict
        self._coin_bias: dict = {}
        # day-of-week: _dow_bias[(dow, utc_hour)] = same dict
        # dow: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
        self._dow_bias: dict = {}
        self._last_refresh: float = 0.0
        self._lock = asyncio.Lock()

    def _compute(self):
        """Synchronous DB read — called in a thread via asyncio.to_thread.
        Merges three data sources with recency weighting.
        """
        import sqlite3 as _sq
        import os as _os
        from datetime import datetime as _dt, timedelta as _td

        _now = _dt.utcnow()
        _lookback_ts  = (_now - _td(days=HOUR_BIAS_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        _recent_ts    = (_now - _td(days=HOUR_BIAS_RECENT_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")

        bias      = {}   # global hour bucket
        coin_bias = {}   # (coin, hour) bucket
        dow_bias  = {}   # (dow, hour) bucket

        def _empty():
            return {"up_n": 0.0, "up_w": 0.0, "dn_n": 0.0, "dn_w": 0.0}

        def _add_rows(rows, weight=1.0):
            """rows: (utc_hour, outcome, status, coin, ts)"""
            for utc_hour, outcome, status, coin_raw, ts_raw in rows:
                coin_key = (coin_raw or "").upper()
                is_up = (outcome or "").upper() in ("UP", "YES", "HIGHER", "ABOVE")
                won   = status == "won"

                # global bucket
                b = bias.setdefault(utc_hour, _empty())
                if is_up:
                    b["up_n"] += weight
                    if won: b["up_w"] += weight
                else:
                    b["dn_n"] += weight
                    if won: b["dn_w"] += weight

                # per-coin bucket (only when coin is known)
                if coin_key:
                    cb = coin_bias.setdefault((coin_key, utc_hour), _empty())
                    if is_up:
                        cb["up_n"] += weight
                        if won: cb["up_w"] += weight
                    else:
                        cb["dn_n"] += weight
                        if won: cb["dn_w"] += weight

                # day-of-week bucket
                try:
                    _dt_obj = _dt.fromisoformat((ts_raw or "").replace("+00:00", ""))
                    _dow    = _dt_obj.weekday()  # 0=Mon … 6=Sun
                    db = dow_bias.setdefault((_dow, utc_hour), _empty())
                    if is_up:
                        db["up_n"] += weight
                        if won: db["up_w"] += weight
                    else:
                        db["dn_n"] += weight
                        if won: db["dn_w"] += weight
                except Exception:
                    pass

        # coin column may be absent in older DBs — COALESCE to ''
        TRADES_SQL_OLD = """
            SELECT CAST(strftime('%H', ts) AS INTEGER),
                   outcome, status, '' AS coin, ts
            FROM trades
            WHERE ts >= ?
              AND status IN ('won','lost')
              AND outcome IS NOT NULL
        """
        TRADES_SQL_NEW = """
            SELECT CAST(strftime('%H', ts) AS INTEGER),
                   outcome, status,
                   COALESCE(coin, '') AS coin, ts
            FROM trades
            WHERE ts >= ?
              AND status IN ('won','lost')
              AND outcome IS NOT NULL
        """

        def _query_db(path, timeout=5):
            """Return (old_rows, recent_rows) from a trades DB."""
            try:
                con = _sq.connect(path, timeout=timeout)
                con.execute("PRAGMA journal_mode=WAL")
                # detect coin column
                cols = [r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()]
                sql  = TRADES_SQL_NEW if "coin" in cols else TRADES_SQL_OLD
                old_rows    = con.execute(sql, (_lookback_ts,)).fetchall()
                recent_rows = con.execute(sql, (_recent_ts,)).fetchall()
                con.close()
                return old_rows, recent_rows
            except Exception as e:
                print(f"[GhostMemory] DB error ({path}): {e}")
                return [], []

        # ── 1. Live DB ────────────────────────────────────────────────────────
        old_r, rec_r = _query_db(DB_PATH)
        _add_rows(old_r, weight=1.0)
        # recent rows get extra weight on top of the base 1.0 already added
        _add_rows(rec_r, weight=HOUR_BIAS_RECENT_WEIGHT - 1.0)

        # ── 2. Paper DB ───────────────────────────────────────────────────────
        # Only merge paper data in paper mode — phantom REST fills corrupt live bias
        if PAPER_TRADE:
            _paper_path = _os.path.join(_os.path.dirname(DB_PATH), "crypto_ghost_PAPER.db")
            if _os.path.exists(_paper_path):
                old_r, rec_r = _query_db(_paper_path)
                _add_rows(old_r, weight=1.0)
                _add_rows(rec_r, weight=HOUR_BIAS_RECENT_WEIGHT - 1.0)

        # ── 3. Frank DB (market resolutions) ─────────────────────────────────
        _frank_path = _os.path.join(_os.path.dirname(DB_PATH), "frank.db")
        if _os.path.exists(_frank_path):
            try:
                con3 = _sq.connect(_frank_path, timeout=10)
                frank_rows = con3.execute("""
                    SELECT CAST(strftime('%H', start_utc) AS INTEGER),
                           winner, start_utc
                    FROM resolutions
                    WHERE start_utc >= ?
                      AND winner IN ('UP','DOWN')
                """, (_lookback_ts,)).fetchall()
                con3.close()
                for utc_hour, winner, start_utc in frank_rows:
                    w = HOUR_BIAS_RECENT_WEIGHT if start_utc >= _recent_ts else 1.0
                    b = bias.setdefault(utc_hour, _empty())
                    b["up_n"] += w; b["dn_n"] += w
                    if winner == "UP":
                        b["up_w"] += w
                    else:
                        b["dn_w"] += w
                    # day-of-week bucket for frank rows
                    try:
                        _dt_obj = _dt.fromisoformat((start_utc or "").replace("+00:00", ""))
                        _dow    = _dt_obj.weekday()
                        db = dow_bias.setdefault((_dow, utc_hour), _empty())
                        db["up_n"] += w; db["dn_n"] += w
                        if winner == "UP":
                            db["up_w"] += w
                        else:
                            db["dn_w"] += w
                    except Exception:
                        pass
            except Exception as e:
                print(f"[GhostMemory] frank DB error: {e}")

        return bias, coin_bias, dow_bias

    async def refresh(self):
        """Refresh bias tables from DB (async, non-blocking)."""
        bias, coin_bias, dow_bias = await asyncio.to_thread(self._compute)
        async with self._lock:
            self._bias      = bias
            self._coin_bias = coin_bias
            self._dow_bias  = dow_bias
            self._last_refresh = time.monotonic()
        total  = sum(v["up_n"] + v["dn_n"] for v in bias.values())
        strong = [(h, v) for h, v in bias.items()
                  if (v["up_n"] >= HOUR_BIAS_MIN_SAMPLES and
                      v["up_w"] / v["up_n"] >= HOUR_BIAS_MIN_WR) or
                     (v["dn_n"] >= HOUR_BIAS_MIN_SAMPLES and
                      v["dn_w"] / v["dn_n"] >= HOUR_BIAS_MIN_WR)]
        coin_strong = len([k for k, v in coin_bias.items()
                           if (v["up_n"] >= HOUR_BIAS_MIN_SAMPLES and
                               v["up_w"] / v["up_n"] >= HOUR_BIAS_MIN_WR) or
                              (v["dn_n"] >= HOUR_BIAS_MIN_SAMPLES and
                               v["dn_w"] / v["dn_n"] >= HOUR_BIAS_MIN_WR)])
        dow_strong = len([k for k, v in dow_bias.items()
                          if (v["up_n"] >= HOUR_BIAS_MIN_SAMPLES and
                              v["up_w"] / v["up_n"] >= HOUR_BIAS_MIN_WR) or
                             (v["dn_n"] >= HOUR_BIAS_MIN_SAMPLES and
                              v["dn_w"] / v["dn_n"] >= HOUR_BIAS_MIN_WR)])
        print(f"[GhostMemory] Refreshed — {total:.0f} weighted trades | "
              f"{len(strong)} biased global hours | {coin_strong} biased coin-hours | "
              f"{dow_strong} biased dow-hours")

    async def run_refresh_loop(self):
        """Background task: refresh on startup then every HOUR_BIAS_REFRESH_MIN min."""
        await self.refresh()
        while True:
            await asyncio.sleep(HOUR_BIAS_REFRESH_MIN * 60)
            await self.refresh()

    def _bucket_blocks(self, b: dict, buying_up: bool) -> bool:
        """Return True if bucket b blocks the given direction."""
        if buying_up:
            dn_n = b["dn_n"]
            if dn_n >= HOUR_BIAS_MIN_SAMPLES:
                if b["dn_w"] / dn_n >= HOUR_BIAS_MIN_WR:
                    return True
        else:
            up_n = b["up_n"]
            if up_n >= HOUR_BIAS_MIN_SAMPLES:
                if b["up_w"] / up_n >= HOUR_BIAS_MIN_WR:
                    return True
        return False

    def should_skip(self, buying_up: bool, utc_hour: int, coin: str = "") -> bool:
        """
        Return True if this direction should be blocked at this UTC hour.
        Checks per-coin bias first (if coin supplied + enough samples),
        then day-of-week + hour bias, then falls back to global hour bias.
        """
        if not HOUR_BIAS_ENABLED:
            return False

        # ── Per-coin check (takes priority when coin has enough data) ─────────
        if coin:
            cb = self._coin_bias.get((coin.upper(), utc_hour))
            if cb is not None:
                coin_n = cb["up_n"] + cb["dn_n"]
                if coin_n >= HOUR_BIAS_MIN_SAMPLES:
                    return self._bucket_blocks(cb, buying_up)

        # ── Day-of-week + hour check ──────────────────────────────────────────
        try:
            from datetime import datetime as _dtnow
            _dow = _dtnow.utcnow().weekday()
            db = self._dow_bias.get((_dow, utc_hour))
            if db is not None:
                dow_n = db["up_n"] + db["dn_n"]
                if dow_n >= HOUR_BIAS_MIN_SAMPLES:
                    return self._bucket_blocks(db, buying_up)
        except Exception:
            pass

        # ── Global hour fallback ──────────────────────────────────────────────
        b = self._bias.get(utc_hour)
        if b is None:
            return False
        return self._bucket_blocks(b, buying_up)

    def summary(self, utc_hour: int, coin: str = "") -> str:
        """One-line summary for the given UTC hour (+ optional coin)."""
        parts = []
        if coin:
            cb = self._coin_bias.get((coin.upper(), utc_hour))
            if cb and (cb["up_n"] or cb["dn_n"]):
                if cb["up_n"]:
                    parts.append(f"[{coin}] UP {cb['up_w']:.1f}/{cb['up_n']:.1f} "
                                 f"({100*cb['up_w']//cb['up_n']:.0f}%)")
                if cb["dn_n"]:
                    parts.append(f"[{coin}] DN {cb['dn_w']:.1f}/{cb['dn_n']:.1f} "
                                 f"({100*cb['dn_w']//cb['dn_n']:.0f}%)")
        b = self._bias.get(utc_hour)
        if b:
            if b["up_n"]:
                parts.append(f"UP {b['up_w']:.1f}/{b['up_n']:.1f} "
                             f"({100*b['up_w']//b['up_n']:.0f}%)")
            if b["dn_n"]:
                parts.append(f"DN {b['dn_w']:.1f}/{b['dn_n']:.1f} "
                             f"({100*b['dn_w']//b['dn_n']:.0f}%)")
        return " | ".join(parts) if parts else "no data"


HOUR_BIAS = HourBiasEngine()


# ── CoinDirEngine ─────────────────────────────────────────────────────────────
class CoinDirEngine:
    """
    Tier-aware coin+direction auto-blocker. Reads live DB every
    COIN_DIR_REFRESH_SECS seconds, computes EV/trade separately for T1 and T2
    so old T3/T4 data never masks current-tier performance.

    Blocked set stores (coin, direction, tier) tuples:
      tier=1  → block only for WRAITH markets
      tier=2  → block only for SPECTER markets
      tier=0  → block for ALL tiers (pre-seeded from SKIP_DOWN_BIASED_COINS)

    is_blocked(coin, buying_up, tier) checks tier-specific first, then
    falls back to tier=0 (manual seed). This means a coin blocked for T2
    can still fire freely on T1 if T1 data is clean.
    """

    def __init__(self):
        self._blocked: set = set()
        # Pre-seed from static env: tier=0 = all tiers blocked
        for coin in SKIP_DOWN_BIASED_COINS:
            self._blocked.add((coin.upper(), "DOWN", 0))
        self._lock = asyncio.Lock()

    async def refresh_loop(self):
        """Background task: refresh immediately, then every COIN_DIR_REFRESH_SECS."""
        await self._refresh()
        while True:
            await asyncio.sleep(COIN_DIR_REFRESH_SECS)
            await self._refresh()

    async def _refresh(self):
        """Run DB query in executor — never blocks the event loop."""
        try:
            loop = asyncio.get_event_loop()
            new_blocked = await loop.run_in_executor(None, self._query_db)
            async with self._lock:
                added   = new_blocked - self._blocked
                removed = self._blocked - new_blocked
                self._blocked = new_blocked
            if added:
                labels = [f"T{t}-{c}-{d}" if t else f"ALL-{c}-{d}"
                          for c, d, t in sorted(added)]
                print(f"[CoinDir] BLOCKED:   {labels}")
            if removed:
                labels = [f"T{t}-{c}-{d}" if t else f"ALL-{c}-{d}"
                          for c, d, t in sorted(removed)]
                print(f"[CoinDir] UNBLOCKED: {labels}")
        except Exception as e:
            print(f"[CoinDir] refresh error: {e}")

    def _query_db(self) -> set:
        """
        Query last COIN_DIR_LOOKBACK settled trades grouped by (coin, outcome, tier).
        Evaluates T1 and T2 independently so legacy T3/T4 data never masks them.
        Minimum samples: COIN_DIR_MIN_TRADES for T1, COIN_DIR_MIN_TRADES_T2 for T2.
        """
        # Static seeds are tier=0 (block all tiers) — always sticky
        static_seeds: set = {(c.upper(), "DOWN", 0) for c in SKIP_DOWN_BIASED_COINS}
        new_blocked: set  = set(static_seeds)

        try:
            with _db() as conn:
                rows = conn.execute("""
                    SELECT coin, outcome, tier,
                           COUNT(*)                       AS n,
                           ROUND(SUM(pnl) / COUNT(*), 4) AS ev
                    FROM (
                        SELECT coin, outcome, tier, pnl
                        FROM   trades
                        WHERE  status IN ('won', 'lost')
                          AND  tier IN (1, 2)
                        ORDER  BY id DESC
                        LIMIT  ?
                    )
                    GROUP BY coin, outcome, tier
                """, (COIN_DIR_LOOKBACK,)).fetchall()
        except Exception as e:
            print(f"[CoinDir] DB query error: {e}")
            return set(self._blocked)

        for coin, outcome, tier, n, ev in rows:
            coin_u = coin.upper()
            dir_u  = outcome.upper()
            min_n  = COIN_DIR_MIN_TRADES_T2 if tier == 2 else COIN_DIR_MIN_TRADES
            key    = (coin_u, dir_u, tier)
            seed0  = (coin_u, dir_u, 0)

            if n >= min_n and ev < COIN_DIR_EV_THRESH:
                new_blocked.add(key)
            elif key in new_blocked and seed0 not in static_seeds:
                # Unblock if EV fully recovered — never unblock manual static seeds
                if n >= min_n and ev >= 0:
                    new_blocked.discard(key)

        return new_blocked

    def is_blocked(self, coin: str, buying_up: bool, tier: int = 0) -> bool:
        """
        Return True if this coin+direction is blocked for the given tier.
        Checks tier-specific block first, then falls through to tier=0 (all-tier seed).
        tier=0 → all-tier check only (pre-seeded from SKIP_DOWN_BIASED_COINS).
        BULL_REGIME=true → UP signals always pass through (DOWN still EV-filtered).
        """
        if buying_up and BULL_REGIME:
            return False   # bull market: never suppress UP oracle-lag signals
        c = coin.upper()
        d = "UP" if buying_up else "DOWN"
        if tier in (1, 2) and (c, d, tier) in self._blocked:
            return True
        return (c, d, 0) in self._blocked   # all-tier manual seed

    def status_str(self) -> str:
        """One-liner for dashboard/logging: T1-ETH-DOWN ALL-BNB-DOWN …"""
        if not self._blocked:
            return "all clear"
        parts = []
        for c, d, t in sorted(self._blocked):
            parts.append(f"T{t}-{c}-{d}" if t else f"ALL-{c}-{d}")
        return " ".join(parts)


COIN_DIR = CoinDirEngine()


class SegmentTracker:
    """Periodic DB reader that scores each trading segment by rolling win rate.

    Dimensions tracked per trade:
      tier      — T2 / T3 / T4 independently
      coin      — BTC / ETH / SOL / XRP / BNB
      hour      — ET hour-of-day 0–23
      tier_coin — (tier, coin) combined pair

    Multiplier curve maps win-rate → size/priority scale factor:
      wr < SEG_DISABLE_BELOW → 0.0  (dead — skip)
      wr at SEG_NEUTRAL_WR   → 1.0  (neutral)
      wr at SEG_BOOST_ABOVE  → SEG_MAX_MULT  (boosted)

    The composite multiplier is the geometric mean of all activated dimensions
    so that one weak dimension moderates (but doesn't kill) a good signal.
    """

    def __init__(self):
        self._stats: dict = {}         # (dim, value) → {wins, total, wr}
        self._last_refresh: float = 0.0
        self._last_summary: float = 0.0

    # ── DB query (runs in executor — never blocks the event loop) ──────────

    def _query_db(self) -> dict:
        from collections import defaultdict
        from datetime import datetime, timezone, timedelta
        buckets: dict = defaultdict(lambda: {"wins": 0, "total": 0})
        try:
            conn = _db()
            rows = conn.execute("""
                SELECT tier, coin, ts, status
                FROM   trades
                WHERE  status IN ('won', 'lost')
                ORDER  BY ts DESC
                LIMIT  ?
            """, (SEG_WINDOW_TRADES * 25,)).fetchall()
            conn.close()
        except Exception:
            return {}

        for tier_raw, coin_raw, ts_str, status in rows:
            won  = (status == "won")
            coin = (coin_raw or "").upper()
            tier = int(tier_raw) if tier_raw else 0
            # Convert stored UTC ISO to ET hour
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                et_hour = (dt.astimezone(timezone(timedelta(hours=-4)))).hour
            except Exception:
                et_hour = -1

            for key in [
                ("tier",      tier),
                ("coin",      coin),
                ("hour",      et_hour),
                ("tier_coin", (tier, coin)),
            ]:
                b = buckets[key]
                if b["total"] < SEG_WINDOW_TRADES:
                    b["total"] += 1
                    if won:
                        b["wins"] += 1

        stats = {}
        for key, b in buckets.items():
            if b["total"] > 0:
                stats[key] = {
                    "wins":  b["wins"],
                    "total": b["total"],
                    "wr":    round(b["wins"] / b["total"], 4),
                }
        return stats

    # ── Async refresh wrapper ──────────────────────────────────────────────

    async def refresh(self):
        try:
            loop  = asyncio.get_event_loop()
            stats = await loop.run_in_executor(None, self._query_db)
            self._stats        = stats
            self._last_refresh = time.monotonic()
        except Exception as e:
            print(f"[SegTracker] refresh error: {e}")

    async def maybe_refresh(self):
        """Refresh if SEG_REFRESH_MIN minutes have elapsed."""
        if (not SEG_TRACKING_ENABLED or
                time.monotonic() - self._last_refresh < SEG_REFRESH_MIN * 60):
            return
        await self.refresh()

    # ── Multiplier helpers ─────────────────────────────────────────────────

    def _wr_to_mult(self, wr: float) -> float:
        """Map a win-rate to a [0, SEG_MAX_MULT] multiplier."""
        if wr < SEG_DISABLE_BELOW:
            return 0.0
        if wr < SEG_NEUTRAL_WR:
            t = (wr - SEG_DISABLE_BELOW) / max(SEG_NEUTRAL_WR - SEG_DISABLE_BELOW, 1e-9)
            return max(0.0, t)
        t = min((wr - SEG_NEUTRAL_WR) / max(SEG_BOOST_ABOVE - SEG_NEUTRAL_WR, 1e-9), 1.0)
        return 1.0 + t * (SEG_MAX_MULT - 1.0)

    def get_multiplier(self, tier: int, coin: str, et_hour: int) -> float:
        """Return composite segment multiplier for (tier, coin, hour).

        Returns 1.0 (neutral) when tracking is disabled or data is thin.
        Returns 0.0 when any activated dimension classifies the segment as dead.
        """
        if not SEG_TRACKING_ENABLED or not self._stats:
            return 1.0

        mults = []
        for key in [
            ("tier",      tier),
            ("coin",      coin.upper()),
            ("hour",      et_hour),
            ("tier_coin", (tier, coin.upper())),
        ]:
            stat = self._stats.get(key)
            if stat and stat["total"] >= SEG_MIN_SAMPLE:
                m = self._wr_to_mult(stat["wr"])
                if m == 0.0:
                    return 0.0    # any dead dimension → hard skip
                mults.append(m)

        if not mults:
            return 1.0   # no activated dimensions yet → neutral

        # Geometric mean: moderate without over-penalising multi-dim signals
        from functools import reduce
        import math
        geo = reduce(lambda a, b: a * b, mults) ** (1.0 / len(mults))
        return round(min(geo, SEG_MAX_MULT), 3)

    # ── Human-readable summary ─────────────────────────────────────────────

    def print_summary(self):
        ts = datetime.now().strftime("%H:%M:%S")
        if not self._stats:
            print(f"[{ts}] [SegTracker] No resolved trades yet — all segments neutral")
            return
        print(f"[{ts}] ══════ Segment Performance Summary ══════")
        for dim in ("tier", "coin", "hour", "tier_coin"):
            entries = sorted(
                [(k, v) for k, v in self._stats.items() if k[0] == dim and v["total"] >= SEG_MIN_SAMPLE],
                key=lambda x: -x[1]["wr"]
            )
            if not entries:
                continue
            print(f"  ── {dim.upper()} ──")
            for key, stat in entries:
                mult = self._wr_to_mult(stat["wr"])
                bar  = "█" * min(int(mult * 4), 8)
                flag = "⛔" if mult == 0.0 else ("⚡" if mult >= SEG_MAX_MULT else " ")
                print(f"  {flag} {str(key[1]):18s}  wr={stat['wr']:.1%}  "
                      f"({stat['wins']}/{stat['total']})  ×{mult:.2f}  {bar}")
        self._last_summary = time.monotonic()

    def maybe_print_summary(self):
        """Print summary if SEG_SUMMARY_MIN minutes have elapsed."""
        if (SEG_TRACKING_ENABLED
                and time.monotonic() - self._last_summary > SEG_SUMMARY_MIN * 60):
            self.print_summary()


SEGMENT_TRACKER = SegmentTracker()



# ─── PERFORMANCE ANALYTICS ────────────────────────────────────────────────────

class PerformanceAnalytics:
    """
    Tracks four health dimensions per tier, refreshed from DB every
    PERF_REFRESH_MIN minutes via run_in_executor (non-blocking).

      win_rate   - fraction of closed trades that resolved won
      avg_roi    - mean pnl/size_usdc across the rolling window
      avg_slip   - mean (fill_price - entry_price)/entry_price %
      fill_rate  - fraction of fire_trade() attempts that filled

    When PERF_ADAPT_ENABLED=true, exports adaptive signals:
      get_adaptive_confidence(tier) -> raised threshold for weak tiers
      get_adaptive_size_mult(tier)  -> 0.5-1.0 damper for losing tiers

    Reports auto-print every PERF_ROLLING_MIN minutes (rolling) and
    once per calendar day at startup (daily breakdown by tier + coin).
    """

    def __init__(self):
        import threading
        self._lock         = threading.Lock()
        self._stats: dict  = {}       # {tier: {n, wr, avg_roi, avg_slip, fill_rate}}
        self._last_refresh = 0.0
        self._last_rolling = 0.0
        self._last_daily   = ""       # "YYYY-MM-DD" of last daily report

    # ── DB helpers ────────────────────────────────────────────────────────────
    def _query_stats(self) -> dict:
        out = {}
        try:
            conn = _db(timeout=10)
            for tier in (1, 2):
                rows = conn.execute(
                    "SELECT status, pnl, size_usdc, fill_price, entry_price "
                    "FROM trades WHERE tier=? AND status IN ('won','lost') "
                    "AND ts >= '2026-05-01' "
                    "ORDER BY id DESC LIMIT ?",
                    (tier, PERF_WINDOW_TRADES)
                ).fetchall()
                if not rows:
                    continue
                n     = len(rows)
                wins  = sum(1 for r in rows if r[0] == "won")
                rois  = [(r[1] or 0.0) / (r[2] or 1.0) for r in rows if r[2]]
                slips = [(r[3] - r[4]) / r[4] * 100.0
                         for r in rows if r[3] and r[4] and r[4] > 0]
                out[tier] = {
                    "n": n, "wins": wins, "wr": wins / n,
                    "avg_roi":  sum(rois)  / len(rois)  if rois  else 0.0,
                    "avg_slip": sum(slips) / len(slips) if slips else 0.0,
                }
            for tier in (1, 2):
                rows = conn.execute(
                    "SELECT filled FROM fill_attempts "
                    "WHERE tier=? AND ts >= '2026-05-01' ORDER BY id DESC LIMIT ?",
                    (tier, PERF_WINDOW_TRADES)
                ).fetchall()
                fr = sum(r[0] for r in rows) / len(rows) if rows else 1.0
                if tier in out:
                    out[tier]["fill_rate"] = fr
            conn.close()
        except Exception as e:
            print(f"[PERF] stats query error: {e}")
        return out

    def _query_daily(self, date_str: str) -> dict:
        out = {}
        try:
            conn = _db(timeout=10)
            rows = conn.execute(
                "SELECT tier, coin, status, pnl, size_usdc, fill_price, entry_price "
                "FROM trades WHERE ts LIKE ? AND status IN ('won','lost')",
                (date_str + "%",)
            ).fetchall()
            conn.close()
            if not rows:
                return out
            n         = len(rows)
            wins      = sum(1 for r in rows if r[2] == "won")
            total_pnl = sum(r[3] or 0 for r in rows)
            rois      = [(r[3] or 0) / (r[4] or 1) * 100 for r in rows if r[4]]
            slips     = [(r[5] - r[6]) / r[6] * 100
                         for r in rows if r[5] and r[6] and r[6] > 0]
            out["overall"] = {
                "n": n, "wins": wins, "wr": wins / n,
                "total_pnl": round(total_pnl, 2),
                "avg_roi":   round(sum(rois)  / len(rois),  2) if rois  else 0.0,
                "avg_slip":  round(sum(slips) / len(slips), 3) if slips else 0.0,
            }
            for tier in (1, 2):
                tr = [r for r in rows if r[0] == tier]
                if tr:
                    tw = sum(1 for r in tr if r[2] == "won")
                    out["T" + str(tier)] = {
                        "n": len(tr), "wins": tw, "wr": tw / len(tr),
                        "pnl": round(sum(r[3] or 0 for r in tr), 2),
                    }
            for coin in ("BTC", "ETH", "SOL", "XRP", "BNB"):
                cr = [r for r in rows if (r[1] or "").upper() == coin]
                if cr:
                    cw = sum(1 for r in cr if r[2] == "won")
                    out[coin] = {
                        "n": len(cr), "wins": cw, "wr": cw / len(cr),
                        "pnl": round(sum(r[3] or 0 for r in cr), 2),
                    }
        except Exception as e:
            print(f"[PERF] daily query error: {e}")
        return out

    # ── Attempt / fill tracking ───────────────────────────────────────────────
    def record_attempt(self, tier: int, coin: str, token_id: str,
                       size: float, ask: float):
        """Called once per fire_trade() immediately before the FOK order."""
        if not PERF_ENABLED:
            return
        try:
            conn = _db(timeout=5)
            conn.execute(
                "INSERT INTO fill_attempts "
                "(ts, tier, coin, token_id, ask_price, attempted_size) "
                "VALUES (?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(),
                 tier, coin, token_id, ask, size)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def record_fill(self, token_id: str, fill_price: float,
                    fill_size: float, ask: float):
        """Called after a confirmed fill to mark the attempt row as filled."""
        if not PERF_ENABLED:
            return
        try:
            slip = (fill_price - ask) / ask * 100.0 if ask > 0 else 0.0
            conn = _db(timeout=5)
            conn.execute(
                "UPDATE fill_attempts "
                "SET filled=1, fill_price=?, fill_size=?, slippage_pct=? "
                "WHERE token_id=? "
                "AND id=(SELECT MAX(id) FROM fill_attempts WHERE token_id=?)",
                (fill_price, fill_size, slip, token_id, token_id)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    # ── Async refresh ─────────────────────────────────────────────────────────
    async def maybe_refresh(self):
        if not PERF_ENABLED:
            return
        if time.monotonic() - self._last_refresh < PERF_REFRESH_MIN * 60:
            return
        loop  = asyncio.get_event_loop()
        stats = await loop.run_in_executor(None, self._query_stats)
        with self._lock:
            self._stats        = stats
            self._last_refresh = time.monotonic()

    def _get(self, tier: int) -> dict:
        with self._lock:
            return self._stats.get(tier, {})

    # ── Adaptive outputs ──────────────────────────────────────────────────────
    def is_strategy_disabled(self, tier: int) -> bool:
        if not PERF_ADAPT_ENABLED:
            return False
        s = self._get(tier)
        return bool(s) and s.get("n", 0) >= PERF_MIN_SAMPLE and s["wr"] < PERF_DISABLE_WR

    def get_adaptive_confidence(self, tier: int) -> float:
        """Effective CONFIDENCE_MIN for this tier. Returns 999 if disabled."""
        if not PERF_ADAPT_ENABLED:
            return CONFIDENCE_MIN
        s = self._get(tier)
        if not s or s.get("n", 0) < PERF_MIN_SAMPLE:
            return CONFIDENCE_MIN
        wr = s["wr"]
        if wr < PERF_DISABLE_WR:
            return 999.0
        if wr < SEG_NEUTRAL_WR:
            denom = max(SEG_NEUTRAL_WR - PERF_DISABLE_WR, 1e-9)
            frac  = 1.0 - (wr - PERF_DISABLE_WR) / denom
            return CONFIDENCE_MIN + 0.15 * frac
        if wr > PERF_BOOST_WR:
            return max(CONFIDENCE_MIN - 0.05, 0.20)
        return CONFIDENCE_MIN

    def get_adaptive_size_mult(self, tier: int) -> float:
        """Size multiplier [0.5, 1.0] based on rolling avg ROI."""
        if not PERF_ADAPT_ENABLED:
            return 1.0
        s = self._get(tier)
        if not s or s.get("n", 0) < PERF_MIN_SAMPLE:
            return 1.0
        roi = s.get("avg_roi", 0.0)
        if roi < -0.50:
            return 0.50
        if roi < -0.20:
            return 0.75
        return 1.0

    # ── Reports ───────────────────────────────────────────────────────────────
    def print_rolling_report(self):
        if not PERF_ENABLED:
            return
        stats  = self._query_stats()
        ts     = datetime.now().strftime("%H:%M:%S")
        names  = {1: "4HR   ", 2: "HOURLY"}
        div    = "-" * 62
        print("\n[" + ts + "] --- ROLLING " + str(PERF_WINDOW_TRADES)
              + "-TRADE PERFORMANCE -----------------------------------------")
        print("  {:7s} {:>4s} {:>6s} {:>7s} {:>7s} {:>6s}  {}".format(
            "Tier", "N", "WR%", "ROI%", "Slip%", "Fill%", "Status"))
        print("  " + div)
        any_data = False
        for tier in (1, 2, 3, 4):
            s = stats.get(tier)
            if not s:
                continue
            any_data = True
            n    = s["n"]
            wr   = s["wr"] * 100
            roi  = s.get("avg_roi", 0) * 100
            slip = s.get("avg_slip", 0)
            fr   = s.get("fill_rate", 1.0) * 100
            disabled = PERF_ADAPT_ENABLED and n >= PERF_MIN_SAMPLE and s["wr"] < PERF_DISABLE_WR
            boosted  = PERF_ADAPT_ENABLED and n >= PERF_MIN_SAMPLE and s["wr"] > PERF_BOOST_WR
            status   = "DISABLED" if disabled else ("BOOSTED" if boosted else "nominal")
            icon     = "X" if disabled else ("^" if boosted else "v")
            extras   = []
            ca = self.get_adaptive_confidence(tier)
            if abs(ca - CONFIDENCE_MIN) > 0.001 and ca < 999.0:
                extras.append("conf>" + "{:.2f}".format(ca))
            sm = self.get_adaptive_size_mult(tier)
            if sm < 1.0:
                extras.append("sz*" + "{:.2f}".format(sm))
            extra_str = "  [" + " ".join(extras) + "]" if extras else ""
            print("  {:7s} {:>4d} {:>6.1f} {:>7.2f} {:>7.3f} {:>6.1f}  [{}] {}{}".format(
                names.get(tier, "T" + str(tier)), n, wr, roi, slip, fr,
                icon, status, extra_str))
        if not any_data:
            print("  (no closed trades in rolling window yet)")
        print("  " + div)
        for tier in (1, 2, 3, 4):
            s = stats.get(tier)
            if not s:
                continue
            if s.get("fill_rate", 1.0) < 0.60:
                print("  WARN T{} fill rate {:.0f}% -- FOK orders may be too small for book depth".format(
                    tier, s["fill_rate"] * 100))
            if abs(s.get("avg_slip", 0)) > 15.0:
                print("  WARN T{} avg slippage {:.1f}% -- market is very thin at entry".format(
                    tier, s["avg_slip"]))
        print()

    def maybe_print_rolling(self):
        if not PERF_ENABLED:
            return
        if time.monotonic() - self._last_rolling > PERF_ROLLING_MIN * 60:
            self.print_rolling_report()
            self._last_rolling = time.monotonic()

    def print_daily_report(self, date_str: str):
        if not PERF_ENABLED:
            return
        data = self._query_daily(date_str)
        ts   = datetime.now().strftime("%H:%M:%S")
        ov   = data.get("overall")
        div  = "=" * 62
        print("\n[" + ts + "] === DAILY REPORT -- " + date_str
              + " ==========================================")
        if not ov:
            print("  No closed trades today.")
        else:
            bar_n = min(int(ov["wr"] * 40), 40)
            bar   = "#" * bar_n + "." * (40 - bar_n)
            print("  Overall : {:d} trades | WR {:.1f}% | PnL ${:+.2f} | "
                  "Avg ROI {:+.2f}% | Avg Slip {:+.3f}%".format(
                      ov["n"], ov["wr"] * 100, ov["total_pnl"],
                      ov["avg_roi"], ov["avg_slip"]))
            print("  WR      [" + bar + "]")
            print("  " + "-" * 62)
            for key in ("T1", "T2"):
                s = data.get(key)
                if s:
                    print("  {:3s}  {:>4d} trades  WR {:>5.1f}%  "
                          "PnL ${:>+7.2f}  ({}/{} won)".format(
                              key, s["n"], s["wr"] * 100,
                              s["pnl"], s["wins"], s["n"]))
            print("  " + "-" * 62)
            for coin in ("BTC", "ETH", "SOL", "XRP", "BNB"):
                s = data.get(coin)
                if s:
                    print("  {:4s}  {:>4d} trades  WR {:>5.1f}%  "
                          "PnL ${:>+7.2f}  ({}/{} won)".format(
                              coin, s["n"], s["wr"] * 100,
                              s["pnl"], s["wins"], s["n"]))
        print("  " + div + "\n")

    def maybe_print_daily(self):
        if not PERF_ENABLED:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_daily:
            self.print_daily_report(today)
            self._last_daily = today


PERF_ANALYTICS = PerformanceAnalytics()



# ─── LATENCY TRACKER ──────────────────────────────────────────────────────────

class LatencyTracker:
    """
    Measures two things your entire edge depends on:

    1. TICK FRESHNESS — how old (in ms) was the directional Binance move when
       the signal fired? Fresher = stronger oracle-lag condition.
       Score decays from 2.0x at <3s to 0.7x at >45s and is applied directly
       to priority_score so the freshest signals always fire first in the top-N.

    2. EXECUTION TIMING — per-trade breakdown:
         move_age_ms   : ms between last directional tick and order submission
         order_ms      : ms to sign + transmit FOK order
         confirm_ms    : ms for exchange to respond
         total_ms      : move_age_ms + order_ms + confirm_ms
       Stored in latency_log table; percentile table prints every LATENCY_STATS_MIN.

    Feed it from two places:
      - PriceTracker WS handler: on_tick(coin, price, prev_price)
      - fire_trade():            record_execution(...)
    """

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        # Per-coin, per-direction: (monotonic_ts, magnitude_frac)
        self._last_up:   dict = {}   # coin -> (ts, mag)
        self._last_down: dict = {}   # coin -> (ts, mag)
        self._last_tick: dict = {}   # coin -> ts  (any tick, regardless of direction)
        self._last_stats = 0.0       # monotonic time of last stats print

    # ── Feed: called from PriceTracker WS handler ────────────────────────────
    def on_tick(self, coin: str, price: float, prev_price: float):
        """Record a Binance price tick. Call BEFORE updating self.prices."""
        if price <= 0 or prev_price <= 0:
            return
        now = time.monotonic()
        mag = abs(price - prev_price) / prev_price
        with self._lock:
            self._last_tick[coin] = now
            if price > prev_price:
                self._last_up[coin]   = (now, mag)
            elif price < prev_price:
                self._last_down[coin] = (now, mag)

    # ── Query helpers ─────────────────────────────────────────────────────────
    def get_move_age_secs(self, coin: str, direction: str) -> float:
        """Seconds since last tick in the given direction ('up' or 'down').
        Returns a large number (999) if no tick seen yet."""
        with self._lock:
            store = self._last_up if direction == "up" else self._last_down
            entry = store.get(coin)
        if not entry:
            return 999.0
        return time.monotonic() - entry[0]

    def get_freshness_mult(self, coin: str, direction: str) -> float:
        """Freshness multiplier applied to priority_score.
          < 3s  -> 2.0x  (oracle lag almost certainly still open)
          3-8s  -> 1.5x  (very likely still open)
          8-15s -> 1.2x  (probably still open — within recency window)
          15-30s-> 1.0x  (neutral)
          30-45s-> decay toward 0.85x
          > 45s -> 0.7x  (stale: lag window likely closed)
        """
        age = self.get_move_age_secs(coin, direction)
        if age < LATENCY_FRESH_MS_ELITE / 1000.0:
            return 2.0
        if age < LATENCY_FRESH_MS_GOOD / 1000.0:
            return 1.5
        if age < LATENCY_FRESH_MS_BASE / 1000.0:
            return 1.2
        if age < 30.0:
            return 1.0
        if age < 45.0:
            return 1.0 - 0.15 * (age - 30.0) / 15.0   # 1.0 -> 0.85
        return 0.7

    def get_move_age_ms(self, coin: str, direction: str) -> float:
        return self.get_move_age_secs(coin, direction) * 1000.0

    # ── Record execution: called from fire_trade() ────────────────────────────
    def record_execution(self, tier: int, coin: str, token_id: str,
                         move_age_ms: float, order_ms: float,
                         confirm_ms: float, freshness: float):
        """Persist one trade's timing breakdown to latency_log."""
        if not LATENCY_TRACK_ENABLED:
            return
        try:
            total_ms = move_age_ms + order_ms + confirm_ms
            conn = _db(timeout=5)
            conn.execute(
                "INSERT INTO latency_log "
                "(ts, tier, coin, token_id, move_age_ms, order_ms, "
                " confirm_ms, total_ms, freshness) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), tier, coin,
                 token_id, round(move_age_ms, 1), round(order_ms, 1),
                 round(confirm_ms, 1), round(total_ms, 1), round(freshness, 3))
            )
            conn.execute(
                "DELETE FROM latency_log WHERE id NOT IN "
                "(SELECT id FROM latency_log ORDER BY id DESC LIMIT 500)"
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    # ── Stats report ──────────────────────────────────────────────────────────
    def _percentile(self, data: list, p: float) -> float:
        if not data:
            return 0.0
        s = sorted(data)
        k = (len(s) - 1) * p / 100.0
        lo, hi = int(k), min(int(k) + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (k - lo)

    def print_stats(self):
        """Print percentile breakdown of recent execution latencies."""
        try:
            conn = _db(timeout=5)
            rows = conn.execute(
                "SELECT move_age_ms, order_ms, confirm_ms, total_ms, freshness "
                "FROM latency_log ORDER BY id DESC LIMIT 100"
            ).fetchall()
            conn.close()
        except Exception:
            return
        if not rows:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        ages    = [r[0] for r in rows]
        orders  = [r[1] for r in rows]
        confs   = [r[2] for r in rows]
        totals  = [r[3] for r in rows]
        fresh   = [r[4] for r in rows]
        p = self._percentile
        div = "-" * 68
        print("\n[" + ts + "] --- LATENCY STATS (last " + str(len(rows))
              + " fills) ----------------------------------------")
        print("  {:20s} {:>8s} {:>8s} {:>8s} {:>8s}".format(
            "Metric", "Mean", "P50", "P90", "P99"))
        print("  " + div)
        for label, col in [
            ("Move age (ms)",    ages),
            ("Order sent (ms)",  orders),
            ("Confirm (ms)",     confs),
            ("Total (ms)",       totals),
        ]:
            mean = sum(col) / len(col)
            print("  {:20s} {:>8.0f} {:>8.0f} {:>8.0f} {:>8.0f}".format(
                label, mean,
                p(col, 50), p(col, 90), p(col, 99)))
        mean_fresh = sum(fresh) / len(fresh)
        pct_elite  = sum(1 for a in ages if a < LATENCY_FRESH_MS_ELITE) / len(ages) * 100
        pct_good   = sum(1 for a in ages if a < LATENCY_FRESH_MS_GOOD)  / len(ages) * 100
        print("  " + div)
        print("  {:20s} {:>8.2f}  ({:.0f}% elite <{:.0f}s,  {:.0f}% good <{:.0f}s)".format(
            "Freshness mult", mean_fresh,
            pct_elite, LATENCY_FRESH_MS_ELITE / 1000,
            pct_good,  LATENCY_FRESH_MS_GOOD  / 1000))
        # Warn if execution is slow
        p90_total = p(totals, 90)
        if p90_total > 30000:
            print("  WARN P90 total latency {:.0f}ms -- may be missing oracle window".format(p90_total))
        p90_order = p(orders, 90)
        if p90_order > 5000:
            print("  WARN P90 order latency {:.0f}ms -- signing/network slow".format(p90_order))
        print()

    def maybe_print_stats(self):
        if not LATENCY_TRACK_ENABLED:
            return
        if time.monotonic() - self._last_stats > LATENCY_STATS_MIN * 60:
            self.print_stats()
            self._last_stats = time.monotonic()


LATENCY_TRACKER = LatencyTracker()


# ─── MARKET FETCHER ───────────────────────────────────────────────────────────
def parse_mins_left_from_title(title: str) -> Optional[float]:
    """
    Parse minutes until close from title like:
    'Bitcoin Up or Down - April 16, 1:10AM-1:15AM ET'
    Returns None if can't parse, negative if already closed.
    """
    # Find end time — the second time in a range like "1:10AM-1:15AM ET"
    match = re.search(r'\d{1,2}:\d{2}[AP]M-(\d{1,2}:\d{2}[AP]M)\s*ET', title, re.IGNORECASE)
    if not match:
        return None
    end_str = match.group(1)  # e.g. "1:15AM"

    # Find date like "April 16,"
    date_match = re.search(r'([A-Za-z]+)\s+(\d{1,2}),', title)

    try:
        now_utc    = datetime.now(timezone.utc)
        # April 2026 is EDT = UTC-4
        now_et     = now_utc - timedelta(hours=4)
        now_et_naive = now_et.replace(tzinfo=None)

        end_time = datetime.strptime(end_str.upper(), "%I:%M%p")

        if date_match:
            month = datetime.strptime(date_match.group(1), "%B").month
            day   = int(date_match.group(2))
            end_naive = now_et_naive.replace(
                month=month, day=day,
                hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0
            )
        else:
            end_naive = now_et_naive.replace(
                hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0
            )
            if end_naive < now_et_naive - timedelta(minutes=5):
                end_naive += timedelta(days=1)

        return round((end_naive - now_et_naive).total_seconds() / 60, 1)
    except Exception:
        return None


async def build_expected_slugs() -> list:
    """
    Bug #1 fix: build English-format slugs matching Polymarket's actual format.

    Correct PM slug format:
        {coin_full}-up-or-down-{month}-{day}-{HHMMam}-{HHMMam}-et

    Examples:
        bitcoin-up-or-down-april-17-1200am-1205am-et
        ethereum-up-or-down-may-5-500am-505am-et

    Returns list of (coin_label, slug) tuples covering 5-min, 1hr, and 4hr
    windows (1 back + several forward) for all coins.
    """
    now_utc = datetime.now(timezone.utc)
    now_ts  = int(now_utc.timestamp())

    # Auto-detect EDT vs EST (simple: March-November = EDT UTC-4, else UTC-5)
    mo = now_utc.month
    et_offset = timedelta(hours=-4) if 3 < mo < 12 else timedelta(hours=-5)

    MONTH_NAMES = ["january","february","march","april","may","june",
                   "july","august","september","october","november","december"]

    # Bug #10 fix included: correct full-name mapping
    coins = [("BTC","bitcoin"), ("ETH","ethereum"),
             ("SOL","solana"),  ("XRP","xrp")]

    def _fmt(dt_naive):
        """Format as '1200am', '505pm', etc."""
        h, m = dt_naive.hour, dt_naive.minute
        ampm = "am" if h < 12 else "pm"
        h12  = h % 12 or 12
        return f"{h12}{m:02d}{ampm}"

    def _slug(coin_full, end_ts, window_secs):
        start_utc = datetime.fromtimestamp(end_ts - window_secs, tz=timezone.utc)
        end_utc   = datetime.fromtimestamp(end_ts,              tz=timezone.utc)
        # Naive ET datetimes for formatting only
        s_et = (start_utc + et_offset).replace(tzinfo=None)
        e_et = (end_utc   + et_offset).replace(tzinfo=None)
        mn   = MONTH_NAMES[s_et.month - 1]
        return f"{coin_full}-up-or-down-{mn}-{s_et.day}-{_fmt(s_et)}-{_fmt(e_et)}-et"

    slugs = []

    # 5-minute windows: 1 back + 6 forward
    base_5m = ((now_ts // 300) + 1) * 300
    for i in range(-1, 7):
        end_ts = base_5m + i * 300
        for label, coin_full in coins:
            slugs.append((label, _slug(coin_full, end_ts, 300)))

    # 1-hour windows: 1 back + 3 forward
    base_1h = ((now_ts // 3600) + 1) * 3600
    for i in range(-1, 4):
        end_ts = base_1h + i * 3600
        for label, coin_full in coins:
            slugs.append((label, _slug(coin_full, end_ts, 3600)))

    # 4-hour windows: 1 back + 2 forward
    base_4h = ((now_ts // 14400) + 1) * 14400
    for i in range(-1, 3):
        end_ts = base_4h + i * 14400
        for label, coin_full in coins:
            slugs.append((label, _slug(coin_full, end_ts, 14400)))

    return slugs


_slug_diag_logged = {"sample": False}

async def direct_scan_updown(session: aiohttp.ClientSession) -> Dict[str, dict]:
    """
    Fetch crypto Up/Down markets via direct slug lookup against gamma API.
    Tries every constructed slug (5min/hourly/4hr/daily for all 4 coins).
    Concurrent fetches keep the loop fast.
    """
    HEADERS  = {"User-Agent": "Mozilla/5.0"}
    found    = {}
    now      = time.time()

    slugs = await build_expected_slugs()

    async def _try_slug(coin_label, slug):
        try:
            async with session.get(f"{GAMMA_API}/markets",
                params={"slug": slug},
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                items = data if isinstance(data, list) else data.get('data', []) if isinstance(data, dict) else []
                if not items:
                    return None
                return (coin_label, slug, items[0])
        except Exception:
            return None

    # Fire all slug lookups concurrently (cap concurrency reasonably)
    sem = asyncio.Semaphore(20)
    async def _bounded(c, s):
        async with sem:
            return await _try_slug(c, s)

    results = await asyncio.gather(*[_bounded(c, s) for c, s in slugs],
                                   return_exceptions=False)

    hit_count = 0
    for res in results:
        if not res:
            continue
        coin_label, slug, m = res
        hit_count += 1
        q   = m.get("question", "")
        end = m.get("endDate") or m.get("endDateIso", "")
        if not end or not is_crypto_market(q):
            continue
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            mins   = (end_dt.timestamp() - now) / 60
            if mins < 0 or mins > 1500:  # within 25 hours
                continue
            mid = m.get("id", "") or slug
            if mid in found:
                continue
            tokens = m.get("tokens", []) or []
            cti    = m.get("clobTokenIds") or m.get("clob_token_ids") or []
            if isinstance(cti, str):
                try:
                    import json as _j
                    cti = _j.loads(cti)
                except Exception:
                    cti = []
            outs = m.get("outcomes") or m.get("outcome_names") or []
            if isinstance(outs, str):
                try:
                    import json as _j
                    outs = _j.loads(outs)
                except Exception:
                    outs = []
            if not tokens and cti:
                tokens = []
                for i, tid in enumerate(cti):
                    out = outs[i] if i < len(outs) else ("UP" if i == 0 else "DOWN")
                    tokens.append({"token_id": str(tid), "outcome": str(out).upper()})
            primary_token = None
            for t in tokens:
                if isinstance(t, dict):
                    o = (t.get("outcome") or "").upper()
                    if o in ("UP", "YES", "HIGHER", "ABOVE"):
                        primary_token = t.get("token_id") or t.get("id")
                        break
            if not primary_token and tokens:
                t = tokens[0]
                primary_token = t.get("token_id") if isinstance(t, dict) else str(t)
            if primary_token:
                found[mid] = {
                    "question":   q,
                    "id":         mid,
                    "tokens":     tokens or [{"token_id": primary_token, "outcome": "UP"}],
                    "_mins_left": round(mins, 1),
                    "endDateIso": end,
                }
        except Exception:
            continue

    if found:
        ts = datetime.now().strftime("%H:%M:%S")
        sample = next(iter(found.values()))
        print(f"[{ts}] Slug scan: {hit_count}/{len(slugs)} slugs matched "
              f"→ {len(found)} valid markets. e.g. \"{sample['question'][:60]}\" "
              f"({sample['_mins_left']:.0f}m left)")
    elif hit_count == 0 and not _slug_diag_logged["sample"]:
        # Slug scan is backup only — gamma sweep is primary discovery.
        # Only log if this is genuinely unexpected (debug aid, fires once).
        _slug_diag_logged["sample"] = True

    if found:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Direct scan found {len(found)} markets via slug lookup")

    return found


# ── Engine v8 Change 2: Market discovery cache ─────────────────────────
# Markets are created every 5 minutes. Caching the discovery list for
# 30s eliminates redundant gamma + slug HTTP calls every 3 seconds.
# mins_to_close is recomputed FRESH on every scan from the cached
# token_markets, so timing stays accurate to the second.
_market_cache    = {"token_markets": None, "ts": 0.0}
_orderbook_cache: dict = {}   # token_id -> (timestamp, book_dict)
_MARKET_CACHE_TTL = 30.0  # seconds


async def _discover_crypto_markets(session: aiohttp.ClientSession) -> Dict[str, dict]:
    """
    Run the actual discovery (gamma sweep + slug scan + optional wallet).
    Returns the raw token_markets dict. Heavy HTTP work happens here.
    """
    HEADERS = {"User-Agent": "Mozilla/5.0"}
    token_markets: Dict[str, dict] = {}
    now = time.time()

    # ── Source 0: Whale/smart-wallet market discovery ────────────────────────
    # Watches WHALE_WALLETS list for recent trades on crypto up/down markets.
    # Populates _whale_signals cache for confidence/priority boosts in check_market.
    # Separate from your own wallet — these are external signal sources.
    if WHALE_WALLETS:
        global _whale_signals
        _new_signals: dict = {}
        for w_addr in WHALE_WALLETS:
            try:
                w_url = (f"{DATA_API}/activity?user={w_addr}"
                         f"&type=TRADE&limit=100&sortBy=TIMESTAMP&sortDirection=DESC")
                async with session.get(w_url, headers=HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=8)) as wr:
                    if wr.status != 200:
                        continue
                    w_trades = await wr.json()
                    if not isinstance(w_trades, list):
                        continue
                    for wt in w_trades:
                        age_w = now - float(wt.get("timestamp", 0))
                        if age_w > WHALE_SIGNAL_TTL:
                            continue
                        title_w = wt.get("title", "")
                        cid_w   = wt.get("conditionId", "")
                        asset_w = str(wt.get("asset", ""))
                        outcome_w = wt.get("outcome", "UP").upper()
                        if not cid_w or not is_crypto_market(title_w):
                            continue
                        direction_w = "up" if outcome_w in ("UP", "YES", "HIGHER") else "down"
                        # Merge: if same market seen both directions → "both"
                        if cid_w in _new_signals:
                            existing = _new_signals[cid_w]["direction"]
                            if existing != direction_w:
                                direction_w = "both"
                        _new_signals[cid_w] = {
                            "direction": direction_w,
                            "ts":        now - age_w,
                            "wallet":    w_addr[:10],
                            "title":     title_w[:60],
                        }
                        # Also add to token_markets for discovery
                        if asset_w and asset_w not in token_markets:
                            token_markets[asset_w] = {
                                "question": title_w,
                                "id":       cid_w,
                                "tokens":   [{"token_id": asset_w, "outcome": outcome_w}],
                                "_age_secs": age_w,
                                "_whale":   True,
                            }
            except Exception as e_w:
                pass  # whale discovery is best-effort — never block main scan
        # Merge new signals, expire old ones
        _whale_signals = {
            cid: sig for cid, sig in {**_whale_signals, **_new_signals}.items()
            if now - sig.get("ts", 0) <= WHALE_SIGNAL_TTL
        }
        if _new_signals:
            _w_count = len(_new_signals)
            _w_names = ", ".join(set(s["wallet"] for s in _new_signals.values()))
            print(f"[WHALE] {_w_count} fresh market signals from [{_w_names}...]")

    # ── Source 1 (optional): Your own wallet's recent activity ───────────────
    # Off by default. Only your FUNDER_ADDRESS — never a third party.
    use_wallet = os.getenv("WALLET_DISCOVERY", "false").strip().lower() in ("true", "1", "yes")
    if use_wallet and FUNDER_ADDRESS:
        try:
            url = (f"{DATA_API}/activity?user={FUNDER_ADDRESS}"
                   f"&type=TRADE&limit=100&sortBy=TIMESTAMP&sortDirection=DESC")
            async with session.get(url, headers=HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    trades = await r.json()
                    for t in (trades if isinstance(trades, list) else []):
                        age    = now - float(t.get("timestamp", 0))
                        if age > 1800:
                            continue
                        title  = t.get("title", "")
                        asset  = str(t.get("asset", ""))
                        cid    = t.get("conditionId", "")
                        outcome= t.get("outcome", "")
                        if not asset or not is_crypto_market(title):
                            continue
                        if asset not in token_markets:
                            token_markets[asset] = {
                                "question":  title,
                                "id":        cid,
                                "tokens":    [{"token_id": asset, "outcome": outcome}],
                                "_age_secs": age,
                            }
        except Exception as e:
            print(f"[WARN] Activity ({FUNDER_ADDRESS[:10]}...): {e}")

    # ── Source 2 (PRIMARY): Polymarket gamma /markets active sweep ───────────
    # Fix: add end_date_min with current ISO timestamp so Gamma only returns
    # markets that haven't ended yet. Also only break when FUTURE markets
    # are added (not just any crypto markets — avoids caching all-past sets).
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sweep_attempts = [
        # Primary: future-only filter + sorted by soonest ending
        f"{GAMMA_API}/markets?end_date_min={now_iso}&closed=false&order=endDate&ascending=true&limit=500",
        f"{GAMMA_API}/markets?end_date_min={now_iso}&active=true&closed=false&order=endDate&ascending=true&limit=500",
        # Fallback without timestamp (Gamma API "now" keyword)
        f"{GAMMA_API}/markets?end_date_min=now&closed=false&order=endDate&ascending=true&limit=500",
        # Crypto-specific tag (21) with future filter
        f"{GAMMA_API}/markets?tag_id=21&end_date_min={now_iso}&closed=false&order=endDate&ascending=true&limit=500",
        # Last resort: unsorted active (may return past markets — break logic handles it)
        f"{GAMMA_API}/markets?active=true&closed=false&limit=500",
    ]
    gamma_added   = 0
    gamma_future  = 0  # track how many added markets actually end in the future
    for sweep_url in sweep_attempts:
        try:
            async with session.get(sweep_url, headers=HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    continue
                data = await r.json()
                items = data if isinstance(data, list) else data.get("data", [])
                if not items:
                    continue
                before = len(token_markets)
                added_future_this_round = 0
                for m in items:
                    q = m.get("question", "")
                    if not is_crypto_market(q):
                        continue
                    if m.get("closed") is True:
                        continue
                    end = m.get("endDate") or m.get("endDateIso", "")
                    if not end:
                        continue
                    # Pre-filter: skip markets already past (don't cache stale data)
                    try:
                        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                        if end_dt.timestamp() <= now:
                            continue
                        added_future_this_round += 1
                    except Exception:
                        pass
                    # Parse tokens / clob_token_ids
                    tokens = m.get("tokens", []) or []
                    cti    = m.get("clobTokenIds") or m.get("clob_token_ids") or []
                    if isinstance(cti, str):
                        try:
                            import json as _j
                            cti = _j.loads(cti)
                        except Exception:
                            cti = []
                    outs = m.get("outcomes") or m.get("outcome_names") or []
                    if isinstance(outs, str):
                        try:
                            import json as _j
                            outs = _j.loads(outs)
                        except Exception:
                            outs = []
                    if not tokens and cti:
                        tokens = []
                        for i, tid in enumerate(cti):
                            out = outs[i] if i < len(outs) else ("UP" if i == 0 else "DOWN")
                            tokens.append({"token_id": str(tid), "outcome": str(out).upper()})
                    if not tokens:
                        continue
                    primary_token = None
                    for t in tokens:
                        if isinstance(t, dict):
                            o = (t.get("outcome") or "").upper()
                            if o in ("UP", "YES", "HIGHER", "ABOVE"):
                                primary_token = t.get("token_id") or t.get("id")
                                break
                    if not primary_token:
                        primary_token = (tokens[0].get("token_id")
                                         if isinstance(tokens[0], dict)
                                         else str(tokens[0]))
                    if primary_token in token_markets:
                        continue
                    token_markets[primary_token] = {
                        "question":   q,
                        "id":         m.get("id", "") or m.get("conditionId", ""),
                        "tokens":     tokens,
                        "endDateIso": end,
                    }
                added = len(token_markets) - before
                gamma_added  += added
                gamma_future += added_future_this_round
                # Only break when we found FUTURE markets — skip if all were past
                if added > 0 and added_future_this_round > 0:
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] Gamma sweep: {len(items)} total, "
                          f"{added} crypto matched, {added_future_this_round} future "
                          f"({sweep_url.split('?')[1][:60]})")
                    break
                elif added > 0:
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] Gamma sweep: {added} crypto found but ALL past — "
                          f"trying next query variant")
        except Exception as e:
            print(f"[WARN] Gamma sweep ({sweep_url[:60]}): {e}")
            continue
    if gamma_future == 0:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [INFO] No future crypto markets found — likely off-peak hours")

    # ── Source 3 (BACKUP): Slug-builder fallback (in case gamma sweep misses) ─
    try:
        direct = await direct_scan_updown(session)
        for mid, m in direct.items():
            token_id = m["tokens"][0]["token_id"] if m["tokens"] else None
            if token_id and token_id not in token_markets:
                token_markets[token_id] = m
    except Exception as e:
        print(f"[WARN] Slug scan: {e}")

    return token_markets


async def fetch_crypto_markets(session: aiohttp.ClientSession) -> List[dict]:
    """
    Public entry: returns markets list ready for check_market().
    Discovery runs in a background task (refresh_markets_periodically).
    This function NEVER triggers HTTP — pure in-memory cache read so the
    main loop stays sub-100ms.
    """
    global _market_cache
    now_cache = time.time()

    # Background task keeps cache warm. If first boot (no data yet), do one
    # blocking fetch so the bot has something to work with immediately.
    if _market_cache["token_markets"] is None:
        _market_cache["token_markets"] = await _discover_crypto_markets(session)
        _market_cache["ts"] = now_cache
        cache_age = "boot"
    else:
        cache_age = f"cached ({now_cache - _market_cache['ts']:.0f}s old)"

    token_markets = _market_cache["token_markets"] or {}

    # ── Filter to markets still open and within scan horizon ─────────────────
    # Runs fresh every scan — recomputes mins_to_close from cached endDates.
    now_ts = time.time()
    markets = []
    drop_no_end = 0
    drop_past   = 0
    drop_far    = 0
    bucket = {"<60m": 0, "<4h": 0, "<24h": 0, "<48h": 0, ">48h": 0}
    for token_id, m in token_markets.items():
        # Priority 1: Use exact endDateIso timestamp (most accurate)
        end_iso = m.get("endDateIso") or m.get("endDate","")
        if end_iso:
            try:
                end_dt   = datetime.fromisoformat(end_iso.replace("Z","+00:00"))
                secs_left = end_dt.timestamp() - now_ts
                mins      = secs_left / 60
                m["_secs_left"] = secs_left
                m["_mins_left"] = round(mins, 2)
            except Exception:
                mins = parse_mins_left_from_title(m.get("question",""))
                m["_mins_left"] = round(mins, 2) if mins else None
                m["_secs_left"] = mins * 60 if mins else None
        else:
            # Fallback: parse from title
            mins = parse_mins_left_from_title(m.get("question",""))
            m["_mins_left"] = round(mins, 2) if mins else None
            m["_secs_left"] = mins * 60 if mins else None

        mins = m.get("_mins_left")
        if mins is None:
            drop_no_end += 1
            continue
        if mins <= 0:
            drop_past += 1
            continue
        # Bucket the mins for diagnostics
        if mins < 60:    bucket["<60m"] += 1
        elif mins < 240: bucket["<4h"]  += 1
        elif mins < 1440: bucket["<24h"] += 1
        elif mins < 2880: bucket["<48h"] += 1
        else:             bucket[">48h"] += 1
        # Cap at 48 hours (covers daily markets + some buffer). Per-tier
        # in_entry_window() inside check_market narrows further.
        if mins > 2880:
            drop_far += 1
            continue
        markets.append(m)

    ts_now = datetime.now().strftime("%H:%M:%S")
    if token_markets:
        print(f"[{ts_now}] Discovery: {len(token_markets)} crypto found → "
              f"{len(markets)} in window  "
              f"[buckets {bucket}, dropped: no_end={drop_no_end} past={drop_past} >48h={drop_far}]")

    return markets



# ─── MAIN SCANNER CLASS ───────────────────────────────────────────────────────
async def _lookup_trade_outcome(session: aiohttp.ClientSession,
                               token_id: str, market_id: str) -> str:
    """Query DATA API first (ground truth for redeemability), then fall back
    to Gamma API.  Fixes NegRisk markets where Gamma outcomePrices settle at
    0.97-0.98 instead of 1.0, incorrectly marking won positions as 'lost'."""

    # ── Step 1: DATA API positions — most reliable for NegRisk outcomes ──
    try:
        url = f"{DATA_API}/positions"
        async with session.get(
            url,
            params={"asset_id": token_id, "sizeThreshold": "0"},
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status == 200:
                positions = await r.json()
                if isinstance(positions, list):
                    for p in positions:
                        tid = p.get("asset_id") or p.get("token_id") or ""
                        if tid == token_id:
                            cur_val = float(p.get("currentValue", 0) or 0)
                            redeemable = p.get("redeemable", False)
                            # If Polymarket says it's redeemable with value → won
                            if redeemable and cur_val > 0.01:
                                return "won"
                            # If market closed and value is ~0 → lost
                            size = float(p.get("size", 0) or 0)
                            if size > 0 and cur_val < 0.01 and not redeemable:
                                return "lost"
    except Exception:
        pass

    # ── Step 2: Gamma API fallback — use 0.5 threshold for NegRisk ──
    import json as _j
    urls = [
        f"{GAMMA_API}/markets?clob_token_ids={token_id}&closed=true",
        f"{GAMMA_API}/markets?id={market_id}&closed=true" if market_id else None,
    ]
    for url in urls:
        if not url:
            continue
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    continue
                data = await r.json()
                items = data if isinstance(data, list) else data.get("data", [])
                if not items:
                    continue
                m = items[0]
                if not m.get("closed"):
                    return None  # not settled yet
                op  = m.get("outcomePrices")
                cti = m.get("clobTokenIds")
                if isinstance(op,  str):
                    try: op  = _j.loads(op)
                    except Exception: op = []
                if isinstance(cti, str):
                    try: cti = _j.loads(cti)
                    except Exception: cti = []
                if op and cti:
                    try:
                        idx   = list(cti).index(token_id)
                        price = float(op[idx]) if idx < len(op) else 0.0
                        # Use 0.5 threshold — NegRisk winners settle at 0.97-1.0,
                        # losers at 0.0-0.03. Avoids misclassifying at 0.98/0.99.
                        return "won" if price >= 0.5 else "lost"
                    except (ValueError, IndexError):
                        pass
        except Exception:
            pass
    return None  # can't determine yet — leave position open


def get_market_duration_mins(question: str) -> Optional[float]:
    """Parse the market window length in minutes from a title like
    'BTC Up or Down - May 5, 1:20PM-1:35PM ET'.
    Returns the duration (e.g. 15.0 for a 15-min window) or None if
    no explicit time range is found (e.g. '[Hour]' keyword markets)."""
    m2 = re.findall(r'(\d{1,2}):(\d{2})([ap]m)', question.lower())
    if len(m2) < 2:
        return None
    def _mins(h, mn, ap):
        h, mn = int(h), int(mn)
        if ap == "pm" and h != 12: h += 12
        if ap == "am" and h == 12: h = 0
        return h * 60 + mn
    diff = (_mins(*m2[-1]) - _mins(*m2[0])) % (24 * 60)
    return float(diff) if diff > 0 else None


class NewsCalendarMonitor:
    """
    Background task: polls ForexFactory every 15 min, sends Telegram alerts
    before HIGH impact macro events (USD by default).
    60-min alert  -> heads up, make sure bot is live.
     5-min alert  -> imminent, BTC move incoming, T2 primed.
    No scanner filters — awareness only.
    """

    def __init__(self):
        self._alerted:  set   = set()
        self._cache:    list  = []
        self._cache_ts: float = 0.0

    async def _fetch(self, session: aiohttp.ClientSession) -> list:
        try:
            async with session.get(FF_CALENDAR_URL,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
        except Exception as exc:
            print(f"[News] fetch error: {exc}")
        return []

    async def _check_and_alert(self, session: aiohttp.ClientSession) -> None:
        now = datetime.now(timezone.utc)
        if now.timestamp() - self._cache_ts > 900 or not self._cache:
            fetched = await self._fetch(session)
            if fetched:
                self._cache    = fetched
                self._cache_ts = now.timestamp()
        if not self._cache:
            return

        high = [
            e for e in self._cache
            if e.get("impact", "").strip().lower() == "high"
            and e.get("country", "").strip().upper() in NEWS_ALERT_CURRENCIES
        ]

        from collections import defaultdict
        groups: dict = defaultdict(list)
        for e in high:
            groups[e["date"]].append(e)

        for date_str, evts in groups.items():
            try:
                evt_time = datetime.fromisoformat(date_str).astimezone(timezone.utc)
            except Exception:
                continue
            mins_away = (evt_time - now).total_seconds() / 60

            for w_label, w_mins in [("early", NEWS_ALERT_MINS_EARLY),
                                     ("imminent", NEWS_ALERT_MINS_IMMINENT)]:
                if not (w_mins - 2.5 <= mins_away <= w_mins + 2.5):
                    continue
                key = f"{date_str}_{w_label}"
                if key in self._alerted:
                    continue
                self._alerted.add(key)

                titles   = " + ".join(e["title"] for e in evts)
                currency = ", ".join(sorted({e["country"] for e in evts}))
                try:
                    et_str = (evt_time.astimezone(_ET_TZ).strftime("%I:%M %p ET")
                              if _ET_TZ else evt_time.strftime("%H:%MZ"))
                except Exception:
                    et_str = "?"

                mins_int = int(round(mins_away))
                if w_label == "imminent":
                    icon     = "\U0001f534"
                    headline = f"IMMINENT ({mins_int}min)"
                    subline  = "T2 primed — BTC move incoming"
                else:
                    icon     = "⚠️"
                    headline = f"HIGH IMPACT in {mins_int}min"
                    subline  = "Confirm bot is live and healthy"

                nl  = "\n"
                msg = (f"{icon} <b>{headline}</b>{nl}"
                       f"\U0001f4c5 {titles}{nl}"
                       f"\U0001f550 {et_str} | {currency}{nl}"
                       f"<i>{subline}</i>")

                asyncio.create_task(_tg_send(msg))
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] [News] {headline}: {titles}")

    async def run(self) -> None:
        if not NEWS_CALENDAR_ENABLED or not TELEGRAM_ENABLED:
            return
        ssl_ctx = None
        try:
            import truststore
            ssl_ctx = truststore.SSLContext(__import__("ssl").PROTOCOL_TLS_CLIENT)
        except ImportError:
            pass
        conn = aiohttp.TCPConnector(ssl=ssl_ctx) if ssl_ctx else aiohttp.TCPConnector()
        async with aiohttp.ClientSession(connector=conn) as session:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] [News] Calendar monitor started "
                  f"(HIGH/{','.join(NEWS_ALERT_CURRENCIES)} "
                  f"| -{NEWS_ALERT_MINS_EARLY}min + -{NEWS_ALERT_MINS_IMMINENT}min)")
            while True:
                try:
                    await self._check_and_alert(session)
                except Exception as exc:
                    print(f"[News] loop error: {exc}")
                await asyncio.sleep(60)


NEWS_MONITOR = NewsCalendarMonitor()


# ── CLOB Health Monitor ───────────────────────────────────────────────────────
async def clob_health_monitor():
    """Background task: pings clob.polymarket.com every 15s.
    Sets _CLOB_DOWN=True after 2 consecutive failures → pauses all fire_trade() calls.
    Sends Telegram alert on DOWN and on RESTORED. Auto-resumes when CLOB recovers.
    Handles Polymarket maintenance windows (typically 5-15 min) gracefully."""
    global _CLOB_DOWN, _CLOB_DOWN_SINCE
    _CLOB_HEALTH_URL  = "https://clob.polymarket.com"
    _CHECK_INTERVAL   = 15    # seconds between health pings
    _FAIL_THRESHOLD   = 2     # consecutive failures before declaring CLOB down
    _consecutive_fails = 0

    connector = aiohttp.TCPConnector(limit=2, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                async with session.get(
                    _CLOB_HEALTH_URL,
                    timeout=aiohttp.ClientTimeout(total=8),
                    ssl=False
                ) as resp:
                    if resp.status < 500:
                        # CLOB is reachable
                        if _CLOB_DOWN:
                            downtime = time.time() - (_CLOB_DOWN_SINCE or time.time())
                            _CLOB_DOWN       = False
                            _CLOB_DOWN_SINCE = None
                            _consecutive_fails = 0
                            msg = (f"✅ CLOB RESTORED — OracleSniper resuming. "
                                   f"Downtime: {downtime:.0f}s")
                            print(f"[CLOB-HEALTH] {msg}")
                            if TELEGRAM_ENABLED:
                                asyncio.create_task(_tg_send(msg))
                        else:
                            _consecutive_fails = 0
                    else:
                        raise aiohttp.ClientError(f"HTTP {resp.status}")
            except Exception as e:
                _consecutive_fails += 1
                if _consecutive_fails >= _FAIL_THRESHOLD and not _CLOB_DOWN:
                    _CLOB_DOWN       = True
                    _CLOB_DOWN_SINCE = time.time()
                    msg = (f"⚠️ CLOB DOWN — Polymarket maintenance detected. "
                           f"Bot paused. Will auto-resume. ({type(e).__name__})")
                    print(f"[CLOB-HEALTH] {msg}")
                    if TELEGRAM_ENABLED:
                        asyncio.create_task(_tg_send(msg))
                elif _consecutive_fails == 1 and not _CLOB_DOWN:
                    print(f"[CLOB-HEALTH] Ping failed ({type(e).__name__}) — "
                          f"waiting for 2nd failure to confirm...")
                elif _CLOB_DOWN:
                    elapsed = time.time() - (_CLOB_DOWN_SINCE or time.time())
                    print(f"[CLOB-HEALTH] Still down ({elapsed:.0f}s) — retrying...")
            await asyncio.sleep(_CHECK_INTERVAL)


class CryptoGhostScanner:
    def __init__(self):
        if not PAPER_TRADE and (not PRIVATE_KEY or not FUNDER_ADDRESS):
            print("\nERROR: Missing credentials in .env\nAdd PAPER_TRADE=true for paper trading\n")
            sys.exit(1)

        if not PAPER_TRADE:
            # Use existing .env API credentials — never try to create new ones
            # (create_or_derive_api_key returns 400 when creds already exist)
            _api_key    = os.getenv("API_KEY",        os.getenv("CLOB_API_KEY", ""))
            _api_secret = os.getenv("API_SECRET",     os.getenv("CLOB_SECRET", ""))
            _api_pass   = os.getenv("API_PASSPHRASE", os.getenv("CLOB_PASSPHRASE", ""))
            _creds = None
            if _api_key and _api_secret and _api_pass:
                try:
                    from py_clob_client_v2.clob_types import ApiCreds as ApiCredsV2
                    _creds = ApiCredsV2(api_key=_api_key, api_secret=_api_secret,
                                        api_passphrase=_api_pass)
                except ImportError:
                    try:
                        from py_clob_client.clob_types import ApiCreds
                        _creds = ApiCreds(api_key=_api_key, api_secret=_api_secret,
                                          api_passphrase=_api_pass)
                    except Exception:
                        _creds = None
            if not _creds:
                # No .env creds — derive fresh ones (first-time setup only)
                try:
                    _bare  = ClobClient(host=POLYMARKET_HOST, chain_id=137,
                                        key=PRIVATE_KEY, signature_type=0)
                    _creds = _bare.create_or_derive_api_key()
                except Exception as e:
                    print(f"[WARN] Could not derive API key: {e}")
                    _creds = None
            self.client = ClobClient(
                host=POLYMARKET_HOST, chain_id=137,
                key=PRIVATE_KEY, signature_type=0,
                creds=_creds,
            )
        else:
            self.client = None

        self.tracker        = PriceTracker()
        self.open_positions: Dict[str, dict] = {}
        self._pending: set = set()  # Bug #8: tracks in-flight fire_trade() calls
        self.session_pnl    = 0.0
        self.session_high   = 0.0
        self.trades_fired   = 0
        self.scan_count     = 0
        self.running        = True
        self._ghost_sweep_ts = 0.0
        # Adaptive risk state
        self.consecutive_losses: int          = 0
        self.pause_until:        Optional[float] = None  # monotonic time
        self._last_balance:      Optional[float] = None  # last known USDC balance

        # Ghost fill guard — tracks order IDs, blocks duplicates, flags ghost fills
        if _GHOST_GUARD_AVAILABLE:
            self.guard = GhostFillGuard(db_path=DB_PATH)
        else:
            self.guard = None

        init_db()
        self._bootstrap_loss_streak()
        self._print_banner()

    def _bootstrap_loss_streak(self):
        """Load current consecutive loss streak from DB on startup.
        Walks trades newest-first; counts consecutive 'lost' until a 'won' is found.
        Ensures the streak display is correct even after a bot restart.
        """
        try:
            conn  = _db()
            rows  = conn.execute(
                "SELECT status FROM trades WHERE status IN ('won','lost') "
                "ORDER BY id DESC LIMIT 100"
            ).fetchall()
            conn.close()
            streak = 0
            for (status,) in rows:
                if status == "lost":
                    streak += 1
                else:
                    break   # hit a win — streak ends here
            self.consecutive_losses = streak
            if streak > 0:
                print(f"[AdaptiveRisk] Loaded streak from DB: {streak}L (no sizing changes)")
        except Exception:
            pass  # never let this crash startup

    def _print_banner(self):
        wallet = FUNDER_ADDRESS[:12] + "..." if FUNDER_ADDRESS else "NOT SET"
        mode   = "** PAPER MODE — NO REAL MONEY **" if PAPER_TRADE else "!! LIVE MODE — REAL MONEY !!"
        print(f"""
+======================================================+
|         ORACLE SNIPER v10 -- Scanner                 |
|  {mode:<52s}|
+------------------------------------------------------+
|  T2 Hourly Up/Down    -> $3-5/trade -> ~$95  payout  |
|  T3 4hr Up/Down       -> $3/trade   -> ~$45  payout  |
|  T4 Daily Up/Down     -> $2/trade   -> ~$25  payout  |
+------------------------------------------------------+
|  Wallet: {wallet:<44s}|
|  Certainty floor: {MIN_NO_PRICE:.0%} | Max loss: ${MAX_DAILY_LOSS:.0f}/day      |
|  DB: {os.path.basename(DB_PATH):<47s}|
+======================================================+
        """)

    def kill_check(self) -> Optional[str]:
        # Telegram /pause command — check bot_control.json flag file
        _ctrl = os.path.join(SCRIPT_DIR, "bot_control.json")
        try:
            import json as _json
            with open(_ctrl) as _f:
                if _json.load(_f).get("paused"):
                    return "Telegram: bot paused"
        except (FileNotFoundError, Exception):
            pass
        daily = get_daily_loss()
        if daily >= MAX_DAILY_LOSS:
            return f"Daily loss limit: ${daily:.2f}"
        # Adaptive risk: check loss-streak pause
        if ADAPTIVE_RISK and self.pause_until is not None:
            remaining = self.pause_until - time.monotonic()
            if remaining > 0:
                return f"Streak pause: {remaining/60:.1f}m remaining"
            else:
                self.pause_until = None
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] [AdaptiveRisk] Pause expired — trading resumed")
        return None

    def get_size(self, tier: int, coin: str = "") -> float:
        """Return USDC position size. Scales with live balance when BET_PCT > 0."""
        base: float
        if tier == 2 and coin:
            base = T2_SIZE_PER_COIN.get(coin.upper(), T2_SIZE)
        else:
            base = {1: T1_SIZE, 2: T2_SIZE}.get(tier, 2.0)
        # Hour-of-day size multiplier — T2 only (Ghost Bot forensics, 2026-05-10)
        # Scales bet size up during proven ET hours. Applied before BET_PCT and
        # adaptive-risk so MAX_BET_SIZE and streak reductions still cap the final value.
        if tier == 2 and T2_SIZE_HOUR_MULTS:
            _hmult = T2_SIZE_HOUR_MULTS.get(_et_hour(), 1.0)
            if _hmult != 1.0:
                base = round(base * _hmult, 2)
        # Adaptive risk: shrink size after LOSS_STREAK_REDUCE consecutive losses
        if ADAPTIVE_RISK and self.consecutive_losses >= LOSS_STREAK_REDUCE:
            base = round(base * LOSS_SIZE_FACTOR, 2)
            base = max(base, 1.0)   # floor at $1 so we don't go below min order
        return base

    def get_max_entry(self, tier: int) -> float:
        return {1: T1_MAX_ENTRY, 2: T2_MAX_ENTRY,
                3: T1_MAX_ENTRY, 4: T4_MAX_ENTRY}.get(tier, 0.15)

    def get_total_exposure(self) -> float:
        """Total USDC committed across all open + pending positions."""
        return sum(
            (t.get("fill_size") or t.get("size", 0.0))
            for t in self.open_positions.values()
        )

    def get_coin_exposure(self, coin: str) -> float:
        """USDC committed to open + pending positions for a specific coin."""
        c = coin.upper()
        return sum(
            (t.get("fill_size") or t.get("size", 0.0))
            for t in self.open_positions.values()
            if (t.get("coin") or "").upper() == c
        )

    async def _verify_token_side(self, session: aiohttp.ClientSession,
                                  token_id: str, intended_outcome: str) -> bool:
        """
        Pre-fire wrong-side shield: query gamma to confirm token_id actually
        maps to intended_outcome (UP/DOWN). Catches the v8.x class of bugs
        where outcome metadata was missing and tokens[0] fallback fired the
        wrong side. Fails open on network errors so it never blocks a trade
        it can't inspect.
        """
        UP_ALIASES   = ("UP", "YES", "HIGHER", "ABOVE")
        DOWN_ALIASES = ("DOWN", "NO", "LOWER", "BELOW")
        intended_up  = intended_outcome.upper() in UP_ALIASES
        try:
            async with session.get(
                f"{POLYMARKET_HOST.rstrip('/')}/markets",
                params={"clob_token_ids": token_id},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status != 200:
                    return True  # can't verify → fail open
                data  = await r.json()
                items = data.get("data", data) if isinstance(data, dict) else data
                if not isinstance(items, list) or not items:
                    return True
                m   = items[0]
                cti = m.get("clobTokenIds") or m.get("clob_token_ids") or []
                outs= m.get("outcomes") or m.get("outcome_names") or []
                if isinstance(cti, str):
                    try: cti = __import__("json").loads(cti)
                    except Exception: cti = []
                if isinstance(outs, str):
                    try: outs = __import__("json").loads(outs)
                    except Exception: outs = []
                if not cti or not outs:
                    return True
                try:
                    idx = [str(t) for t in cti].index(str(token_id))
                except ValueError:
                    return True  # token not in list → fail open
                actual = str(outs[idx]).upper() if idx < len(outs) else ""
                actual_up = actual in UP_ALIASES
                if actual_up != intended_up:
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] ⚠ WRONG-SIDE SHIELD: {token_id[:14]}… "
                          f"is {actual} but we intended {intended_outcome} — ABORTED")
                    return False
                return True
        except Exception:
            return True  # network error → fail open

    async def fire_trade(self, tier: int, coin: str, token_id: str,
                         market: dict, ask: float, outcome: str = "UP",
                         session: aiohttp.ClientSession = None,
                         size: float = None,
                         dual_fire: bool = False,
                         ghost_score: int = 0) -> bool:
        # CLOB maintenance gate — pause all trades while Polymarket is down
        global _CLOB_DOWN
        if _CLOB_DOWN:
            return False

        # Bug #8 fix: include _pending in position count to prevent race condition
        if token_id in self.open_positions or token_id in self._pending:
            return False

        # Dedup guard: check DB for any existing open row with this token_id.
        # Catches the case where open_positions was cleared (restart) but the
        # DB still has an unresolved open trade — prevents double-firing the
        # same market and the duplicate activity spam that follows.
        try:
            _chk = _db(timeout=3)
            _dup = _chk.execute(
                "SELECT COUNT(*) FROM trades WHERE token_id=? AND status='open'",
                (token_id,)
            ).fetchone()[0]
            _chk.close()
            if _dup > 0:
                return False
        except Exception:
            pass  # DB unavailable — allow fire, resolver dedup handles it
        if (len(self.open_positions) + len(self._pending)) >= MAX_OPEN_POSITIONS:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] SKIP: max positions ({MAX_OPEN_POSITIONS})")
            return False
        # Dual-fire bypasses kill_check: one side MUST win — pauses/limits don't apply
        if not dual_fire and self.kill_check():
            return False

        # ── Global and per-coin exposure guards ──────────────────────────────
        # Check before reserving the pending slot so rejected trades don't
        # consume position capacity. Use passed size (dynamic) if available.
        _req = size if size is not None else self.get_size(tier, coin)
        if MAX_TOTAL_EXPOSURE > 0:
            _total = self.get_total_exposure()
            if _total + _req > MAX_TOTAL_EXPOSURE:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] SKIP {coin}: total exposure ${_total:.2f}+${_req:.2f} "
                      f"> cap ${MAX_TOTAL_EXPOSURE:.2f}")
                return False
        if MAX_COIN_EXPOSURE > 0 and coin:
            _coin_exp = self.get_coin_exposure(coin)
            if _coin_exp + _req > MAX_COIN_EXPOSURE:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] SKIP {coin}: coin exposure ${_coin_exp:.2f}+${_req:.2f} "
                      f"> cap ${MAX_COIN_EXPOSURE:.2f}")
                return False

        self._pending.add(token_id)  # reserve slot before first await
        PERF_ANALYTICS.record_attempt(tier, coin, token_id, _req, ask)
        _t_fire  = time.monotonic()  # latency: order submission start

        size     = _req   # use dynamic size (or fallback computed above)
        question = market.get("question", "")[:70]

        # ── Ghost fill guard pre-check ────────────────────────────────────────
        if self.guard and not self.guard.can_fire(token_id, coin, ask_price=ask):
            self._pending.discard(token_id)
            return False

        # ── Pre-fire wrong-side shield ────────────────────────────────────────
        # Verify token_id actually maps to intended outcome before placing order.
        # Prevents the v8.x class of bugs where missing outcome metadata caused
        # tokens[0] fallback to fire the wrong side (UP token bought as DOWN).
        if session and not PAPER_TRADE:
            if not await self._verify_token_side(session, token_id, outcome):
                self._pending.discard(token_id)
                return False

        try:
            if PAPER_TRADE:
                order_id   = f"PAPER-{int(time.time()*1000)}"
                fill_price = ask   # signal price (no real fill in paper mode)
                fill_size  = size  # intended size
            else:
                # FOK market order: fills immediately at best available price or
                # cancels entirely — eliminates ghost fills at the source.
                # Uses USDC amount directly (no manual share calculation needed).
                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=size,          # USDC to spend
                    side=BUY,
                    order_type=OrderType.FOK
                )
                # Let the v2 client auto-detect neg_risk per token via its
                # internal get_neg_risk() API call (result is cached).
                # Forcing neg_risk=True caused invalid signature on any market
                # that uses the regular exchange_v2 contract instead of
                # neg_risk_exchange_v2 — wrong verifyingContract in EIP-712.
                _t_sign    = time.monotonic()   # latency: HMAC sign start
                signed = self.client.create_market_order(order_args)
                _t_send    = time.monotonic()   # latency: network send start
                resp       = self.client.post_order(signed, OrderType.FOK)
                _t_confirm = time.monotonic()   # latency: confirmed
                order_id = resp.get("orderID", "") if resp else ""

                # ── Parse actual fill price and size from response ─────────────
                # CLOB V2 response format (confirmed from live trade data):
                #   takingAmount = shares received (full integer units, NOT 6-decimal)
                #   makingAmount = USDC paid (full dollar units, NOT 6-decimal)
                # e.g. buy 34 shares at $0.073 for $2.47 →
                #   takingAmount=34, makingAmount=2.469
                #   fill_price = 2.469/34 = $0.0726/share ✓
                #   fill_size  = 2.469 USDC ✓
                fill_price = ask   # default: signal price
                fill_size  = size  # default: intended size
                _actual_usdc = 0.0  # USDC actually matched — used for ghost detection
                if resp:
                    try:
                        taking = float(resp.get("takingAmount") or resp.get("taking_amount") or 0)
                        making = float(resp.get("makingAmount") or resp.get("making_amount") or 0)
                        if taking > 0 and making > 0:
                            fill_size    = round(making, 6)          # USDC paid
                            fill_price   = round(making / taking, 6) # $/share
                            _actual_usdc = making
                    except (ValueError, TypeError, ZeroDivisionError):
                        pass   # keep signal defaults

                # ── Ghost fill guard: reject orders that didn't actually match ──
                # FOK returns an orderID even when cancelled (no liquidity).
                # If makingAmount=0 (no USDC taken), the order was never filled.
                # Punisher fix (L43): retry FOK once immediately — thin books often
                # refresh in <5ms between our first attempt and the retry.
                if order_id and _actual_usdc < 0.01:
                    ts = datetime.now().strftime("%H:%M:%S")
                    _retry_filled = False
                    try:
                        _r2_args   = MarketOrderArgs(
                            token_id=token_id, amount=size,
                            side=BUY, order_type=OrderType.FOK)
                        _r2_signed = self.client.create_market_order(_r2_args)
                        _r2_resp   = self.client.post_order(_r2_signed, OrderType.FOK)
                        if _r2_resp:
                            _r2_taking = float(_r2_resp.get("takingAmount") or
                                               _r2_resp.get("taking_amount") or 0)
                            _r2_making = float(_r2_resp.get("makingAmount") or
                                               _r2_resp.get("making_amount") or 0)
                            if _r2_making > 0.01:
                                resp         = _r2_resp
                                order_id     = _r2_resp.get("orderID", order_id)
                                fill_size    = round(_r2_making, 6)
                                fill_price   = round(_r2_making / _r2_taking, 6) \
                                               if _r2_taking > 0 else ask
                                _actual_usdc = _r2_making
                                _retry_filled = True
                                print(f"[{ts}] [Retry] FOK retry matched {coin} "
                                      f"@ ${fill_price:.4f} — thin book recovered")
                    except Exception as _re:
                        pass   # retry error → fall through to skip below
                    if not _retry_filled:
                        print(f"[{ts}] [Ghost] FOK not matched for {coin} "
                              f"(both attempts 0) — skipping position")
                        self._pending.discard(token_id)
                        return False

            if order_id:
                # ── Register with ghost guard ─────────────────────────────────
                if self.guard and not PAPER_TRADE:
                    self.guard.register_order(
                        order_id, token_id, coin,
                        tier=f"T{tier}", ask_price=ask, size_usdc=size
                    )

                self.open_positions[token_id] = {
                    "tier": tier, "coin": coin, "question": question,
                    "entry": ask,        "size":       size,
                    "fill_price": fill_price, "fill_size": fill_size,
                    "order_id": order_id, "ts": time.time(),
                }
                PERF_ANALYTICS.record_fill(token_id, fill_price, fill_size, ask)
                if LATENCY_TRACK_ENABLED:
                    _lat_dir = "up" if outcome == "UP" else "down"
                    _mv_age  = LATENCY_TRACKER.get_move_age_ms(coin, _lat_dir)
                    _ord_ms  = (locals().get("_t_confirm", _t_fire)
                                - locals().get("_t_send", _t_fire)) * 1000
                    LATENCY_TRACKER.record_execution(
                        tier, coin, token_id, _mv_age, _ord_ms, 0.0,
                        LATENCY_TRACKER.get_freshness_mult(coin, _lat_dir))
                log_trade(tier, coin, market.get("id",""),
                          token_id, question, ask, size, order_id, outcome,
                          fill_price=fill_price, fill_size=fill_size,
                          ghost_score=ghost_score)
                self.trades_fired += 1

                # Show fill accuracy: signal price vs actual fill
                fill_tag  = ""
                if not PAPER_TRADE and abs(fill_price - ask) > 0.0001:
                    fill_tag = f" [fill@{fill_price:.4f}]"
                payout    = round(fill_size / fill_price - fill_size, 2) if fill_price > 0 else 0
                ts        = datetime.now().strftime("%H:%M:%S")
                mode_tag  = "[PAPER]" if PAPER_TRADE else "[LIVE] "
                tier_names = {1:"WRAITH",2:"SPECTER"}
                # Latency breakdown: sign time + network round-trip (Punisher L43)
                _sign_ms = round((locals().get("_t_send", _t_fire)
                                  - locals().get("_t_sign", _t_fire)) * 1000, 1)
                _net_ms  = round((locals().get("_t_confirm", _t_fire)
                                  - locals().get("_t_send", _t_fire)) * 1000, 1)
                _lat_tag = f" | sign:{_sign_ms:.0f}ms net:{_net_ms:.0f}ms" if not PAPER_TRADE else ""
                print(f"[{ts}] {mode_tag} T{tier} {tier_names.get(tier,'')} | "
                      f"{coin} @ ${ask:.3f}{fill_tag} | Size:${fill_size:.2f} -> +${payout:.2f}"
                      f"{_lat_tag} | '{question[:40]}'")
                log_event("📋", f"T{tier} {coin} BUY @ ${ask:.3f}{fill_tag} | "
                                f"+${payout:.2f} | {question[:38]}")
                write_stats_json()

                # Fire-and-forget Telegram fire alert.
                if TELEGRAM_ENABLED:
                    try:
                        asyncio.create_task(_tg_send(
                            f"👻 <b>GHOST LATTICE</b> | 🎯 <b>FIRE T{tier} {coin} {outcome}</b> {mode_tag.strip()}\n"
                            f"Entry <code>${ask:.3f}</code>  "
                            f"Size <code>${size}</code>  "
                            f"Payout <code>+${payout:.2f}</code>\n"
                            f"{question[:80]}"
                        ))
                    except Exception:
                        pass
                return True
            else:
                if not PAPER_TRADE:
                    print(f"  Order rejected: {question[:40]}")
        except Exception as e:
            print(f"  Trade error: {e}")
        finally:
            self._pending.discard(token_id)  # Bug #8: always release pending slot
        return False

    async def check_positions(self, session: aiohttp.ClientSession):
        """In paper mode: sync open_positions with DB. In live: poll Polymarket."""
        if not self.open_positions:
            return

        if PAPER_TRADE:
            # Sync with DB — remove positions the resolver already closed
            try:
                conn       = _db()
                still_open = {row[0] for row in conn.execute(
                    "SELECT token_id FROM trades WHERE status='open'"
                ).fetchall()}
                conn.close()
                closed = [tid for tid in list(self.open_positions.keys())
                          if tid not in still_open]
                for tid in closed:
                    trade = self.open_positions.pop(tid, {})
                    ts    = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] Position cleared from memory: {trade.get('question','?')[:40]}")
            except Exception as e:
                print(f"[WARN] Paper position check: {e}")
            return

        # Live mode: poll Polymarket positions API
        try:
            url = f"{DATA_API}/positions"  # Bug #2 fix: correct Polymarket data API
            async with session.get(url,
                params={"user": FUNDER_ADDRESS, "sizeThreshold": "0"},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return
                positions = await r.json()

            active     = {p.get("asset_id") or p.get("token_id") for p in positions}
            closed_ids = []
            for tid, trade in list(self.open_positions.items()):
                if tid not in active:
                    # Bug #3 fix: query actual Gamma settlement, not pnl>0 math
                    market_id = trade.get("market_id", "")
                    outcome   = await _lookup_trade_outcome(session, tid, market_id)
                    if outcome is None:
                        continue  # not settled yet — keep position open

                    # Pnl from signal price/size (resolver may refine with actual fills).
                    # fill_price now stores $/share correctly (post L18 fix);
                    # fill_size stores USDC paid. Use those when available.
                    fp  = trade.get("fill_price") or trade.get("entry")
                    fs  = trade.get("fill_size")  or trade.get("size")
                    # Sanity: reject corrupted fill_price (e.g. pre-L18 ~14x values)
                    if fp and fp > 1.0:
                        fp = trade.get("entry")
                        fs = trade.get("size")
                    if not fp:
                        fp = ask
                    if not fs:
                        fs = size
                    pnl = (round(fs / fp - fs, 2) if outcome == "won" else -fs)

                    self.session_pnl  += pnl
                    self.session_high  = max(self.session_high, self.session_pnl)
                    ts   = datetime.now().strftime("%H:%M:%S")
                    icon = "✓" if outcome == "won" else "✗"

                    # Real vs estimated pnl annotation
                    real_tag = ""
                    if not PAPER_TRADE and trade.get("fill_price") and abs(fp - trade.get("entry", fp)) > 0.0001:
                        real_tag = f" [fill@{fp:.4f}]"

                    print(f"[{ts}] {icon} CLOSED T{trade['tier']} {outcome.upper()}{real_tag} | "
                          f"{'+ ' if pnl>=0 else ''}${pnl:.2f} | '{trade['question'][:40]}'")

                    # ── Adaptive risk: update consecutive loss streak ──────────
                    if ADAPTIVE_RISK:
                        if outcome == "won":
                            if self.consecutive_losses > 0:
                                print(f"[{ts}] [AdaptiveRisk] Win — streak reset "
                                      f"(was {self.consecutive_losses} losses)")
                            self.consecutive_losses = 0
                            self.pause_until = None   # clear any active pause
                        else:
                            self.consecutive_losses += 1
                            if self.consecutive_losses >= LOSS_STREAK_PAUSE and self.pause_until is None:
                                self.pause_until = time.monotonic() + LOSS_PAUSE_MINUTES * 60
                                print(f"[{ts}] [AdaptiveRisk] {self.consecutive_losses} consecutive "
                                      f"losses — PAUSING {LOSS_PAUSE_MINUTES:.0f}m")
                            elif self.consecutive_losses >= LOSS_STREAK_REDUCE:
                                reduced = round(self.get_size(trade.get("tier", 2), trade.get("coin","")) / LOSS_SIZE_FACTOR * LOSS_SIZE_FACTOR, 2)
                                print(f"[{ts}] [AdaptiveRisk] {self.consecutive_losses} losses — "
                                      f"sizing at {LOSS_SIZE_FACTOR:.0%} (≈${reduced:.2f}/trade)")

                    conn = _db(timeout=10)
                    conn.execute(
                        "UPDATE trades SET status=?,pnl=?,closed_at=? WHERE token_id=? AND status='open'",
                        (outcome, pnl, datetime.now(timezone.utc).isoformat(), tid)
                    )
                    conn.commit()
                    conn.close()
                    closed_ids.append(tid)
            for tid in closed_ids:
                del self.open_positions[tid]
        except Exception as e:
            print(f"[WARN] Position check: {e}")


    async def scan_updown_markets(self, session: aiohttp.ClientSession,
                                  markets: List[dict]):
        fired     = 0
        skip_ask  = 0; skip_liq = 0; skip_cert = 0; skip_win = 0; signals = 0; skip_silent = 0; skip_vel = 0; skip_book = 0; skip_ws_qual = 0
        _recent_fires: dict = {}  # (coin,tier,direction) -> monotonic_ts

        async def check_market(m):
            """
            Decision pipeline:
              cheap-string filters → trend gate → bias/hour gate →
              orderbook fetch → ask/cert/liq gates → fire.
              When LAZY_ORDERBOOK=true (default), the trend direction
              picks which token's orderbook we fetch — saves ~85% of
              HTTP calls per scan. Strategy-neutral in 95%+ of cases.
            """
            nonlocal fired, skip_ask, skip_liq, skip_cert, skip_win, signals, skip_silent, skip_vel, skip_book, skip_ws_qual
            q    = m.get("question", "")
            coin = get_coin(q)
            if not coin or "up or down" not in q.lower():
                return
            tier = classify_tier(q)
            if tier not in (1, 2):
                return
            mins = m.get("_mins_left", 9999)
            secs = m.get("_secs_left", 9999)
            if not in_entry_window(tier, mins):
                skip_win += 1
                return
            # oracle-lag-sniper time gates — per-tier, research-backed windows:
            #
            # T2  (5-min market, 300s total):  last 60s = T=240-270s.
            #   Research confirms oracle-lag for 5-min markets fires in last 30-60s —
            #   MMs stop repricing as resolution approaches. (Medium/Benjamin-Cup 2026)
            #   Candle progress gate and T2_WINDOW_MIN gate REMOVED (legacy guards).
            #   Kill filters, confidence, ROI gates still BYPASSED (oracle-lag all tiers).
            if tier == 2 and (secs > T2_MAX_SECS_LEFT or secs < T2_MIN_SECS_LEFT):
                skip_win += 1
                return
            # T1/T3/T4 (15-min, hourly, daily): fire at ≥5 min remaining.
            #   JonathanPetersonn repo (61.4% WR, 5,017 trades): fires at ≥5 min.
            #   MMs on longer markets reprice slowly; lag window is earlier and wider.
            if tier == 1 and secs < T1_MIN_SECS_LEFT:
                skip_win += 1
                return

            # ── Punisher L1 warmup quality gate ──────────────────────────────
            # Before spending any HTTP or compute budget, confirm we have at least
            # 3 live ticks for this coin in the last 15s with no single jump >5¢.
            # Guarantees the direction signal is based on real live data, not a
            # stale price that's been sitting in the dict since the last reconnect.
            if not self.tracker.check_ws_quality(coin):
                skip_ws_qual += 1
                return

            # ── Regime: DEAD market detection ─────────────────────────────────
            # If Binance price has been essentially flat for REGIME_DEAD_SECS,
            # there is no oracle-lag momentum to snipe. Skip to avoid firing
            # into a stale, unmoving market (polyquant: REGIME_DEAD filter).
            # Uses velocity over the full tick_history window to detect flatness.
            if REGIME_DEAD_ENABLED and tier == 2:  # T2 is the oracle-lag tier
                _vel_long = self.tracker.get_velocity(coin, n=20)
                if _vel_long is not None and abs(_vel_long) < REGIME_DEAD_MIN_VEL:
                    skip_vel += 1
                    return  # dead market — no Binance momentum to exploit

            tokens = m.get("tokens", []) or []
            if not tokens:
                return

            # ── Trend gate: deviation vs correct reference price ──────────────
            # T3: 1h open is the natural reference.
            # T4 FIX 2026-05-08: Polymarket T4 markets are 1H candle direction markets
            #   (e.g. "Ethereum Up or Down - May 8, 10AM ET" = 10AM-11AM ET 1H candle).
            #   Old bug: used 4H candle open. The 4H candle opens at 8AM ET, so if ETH
            #   dropped from 8AM → now, dev<0 → fires DOWN. But the 1H candle (10AM-11AM)
            #   may have opened lower and closed higher → resolves UP → systematic loss.
            #   Fix: T4 now uses 1H candle open, matching the actual market resolution.
            # T2 CRITICAL FIX: must compare current Binance vs price AT MARKET START.
            #   Old bug: used current 5m candle open. For a 15-min market firing at
            #   minute 8, the 5m candle rolled at minute 5 → different reference →
            #   direction can invert: BTC dipped at 1:25PM then recovered → dev(5m)<0
            #   → buys DOWN, but vs 1:20PM market start it's UP → systematic loss.
            #   Fix: parse market duration from title, back-compute market start time,
            #   look up the 5m candle that was open then from opens_5m_history.
            #   For keyword-only T2 (no time range, e.g. "[Hour]"): fall back to 1h.
            _tf     = "1h"   # fallback timeframe for deviation
            MIN_TREND_STRENGTH = (T1_MIN_DEV if tier == 1 else T2_MIN_DEV)
            if MIN_TREND_STRENGTH == 0.0:
                MIN_TREND_STRENGTH = 0.0005  # floor: need at least 0.05% move

            _market_start_ts: Optional[float] = None  # set below for time-range markets
            _mkt_dur_mins:   Optional[float] = None  # window length (mins)
            if tier in (1, 2):
                # T1 contrarian + T2 oracle-lag both anchor dev to their market's own start.
                # T1: if BTC rose since 1:00PM (15-min market), we fade that move → need
                #     dev vs 1:00PM open, not the 1h candle (which may have opened hours ago).
                # T2: same — stale-ask signal requires dev vs this market's specific open.
                _mkt_dur_mins = get_market_duration_mins(q)
                if _mkt_dur_mins and _mkt_dur_mins > 0:
                    _elapsed_secs    = _mkt_dur_mins * 60.0 - secs
                    _market_start_ts = time.time() - max(_elapsed_secs, 0.0)
                    dev = self.tracker.get_deviation_t2(coin, _market_start_ts)
                    # T2 only: 1h-dev fallback when market-start window is flat but
                    # hourly trend is strong. T1 contrarian skips this — its signal
                    # is specifically the market-start deviation (the move to fade).
                    if tier == 2 and (dev is None or abs(dev) < MIN_TREND_STRENGTH):
                        _dev_1h = self.tracker.get_deviation(coin, "1h")
                        if _dev_1h is not None and abs(_dev_1h) >= 0.0015:
                            dev = _dev_1h   # use hourly direction instead
                else:
                    # Keyword-only market (e.g. "[Hour]"): use 1h candle open
                    dev = self.tracker.get_deviation(coin, "1h")
            else:
                dev = self.tracker.get_deviation(coin, _tf)
            # 5-minute market flag — detected from title time range ≤ 5 min
            _is_5m = (tier == 2 and _mkt_dur_mins is not None and _mkt_dur_mins <= 5.5)
            if dev is None:
                skip_cert += 1
                return
            if abs(dev) < MIN_TREND_STRENGTH:
                skip_cert += 1
                return

            # ── 15-minute trend confirmation (Punisher filter) ────────────────
            # For T2 only: require the 15-min trend to AGREE with the 5-min
            # signal direction. Counter-trend 5m plays are the primary bleed
            # source — if 15m is pointing the other way by ≥ 0.10%, skip it.
            # Returns None during warmup (<2 candles) → fail-open, let through.
            if tier == 2:
                _dev_15m = self.tracker.get_deviation_15m(coin)
                if _dev_15m is not None and abs(_dev_15m) >= DEV_15M_THRESH:
                    _15m_up = _dev_15m > 0
                    _5m_up  = dev > 0
                    if _15m_up != _5m_up:   # 15m opposes 5m direction
                        skip_cert += 1
                        return

            # Compute entry ceiling and intended size BEFORE orderbook fetch so
            # best_ask_info() can sum cumulative depth up to our price ceiling.
            max_e         = self.get_max_entry(tier)
            intended_size = self.get_size(tier, coin)
            # ── 5m market overrides: tighter ceiling + last-45s gate ────────────
            if _is_5m:
                max_e = min(max_e, T2_5M_MAX_ENTRY)  # 10¢ vs 15¢ for hourly
                if secs > T2_5M_WINDOW_SEC:           # only fire in last 45s
                    skip_win += 1
                    return

            best_token_id = None
            best_ask      = None
            best_liq      = 0.0
            tok_outcome   = "UP"

            # ── Market state gate (ghost scanner signal) ──────────────────────────
            # Data: observations_full_20260508.db (1.46M obs, Apr-May 2026).
            # TRENDING_UP  (pos>=0.75+vel+): UP 68.7%, DOWN  0.9% → block DOWN fires
            # TRENDING_DOWN(pos<=0.25+vel-): UP  2.0%, DOWN 62.0% → block UP fires
            # Firing AGAINST a confirmed trend = near-guaranteed loss. Hard reject.
            _pos_tf = "5m" if tier == 2 else _tf
            _mkt_state = self.tracker.get_market_state(coin, _pos_tf, _market_start_ts)
            _want_up_for_gate = (dev > 0)
            # Market state gate disabled — pure oracle lag: fire on deviation + cheap ask
            # if _mkt_state == "TRENDING_UP" and not _want_up_for_gate:
            #     skip_cert += 1
            #     return   # trending UP, we'd buy DOWN = 0.9% WR
            # if _mkt_state == "TRENDING_DOWN" and _want_up_for_gate:
            #     skip_cert += 1
            #     return   # trending DOWN, we'd buy UP = 2.0% WR

            # ── Ghost Brain gate ──────────────────────────────────────────────
            # Always read the score (cached 12s) so it gets logged to DB and
            # the learning loop accumulates real data even when gate is disabled.
            # _gb_score = None means brain is offline/stale → stored as NULL in DB,
            # excluded from learning, and never blocks a trade (fail-open).
            _gb_score, _gb_state, _gb_learned = _read_ghost_brain(coin)
            if LAZY_ORDERBOOK:
                # ── Predict cheap side from trend, fetch only that token ──
                # T1/T2/T4: oracle-lag — cheap side AGREES with Binance move (WITH trend).
                # T3: contrarian (fade) — 15-min market-start dev is the primary signal;
                #     we buy the side the market HASN'T moved toward yet (mean-reversion).
                want_up           = (dev < 0) if tier == 1 else (dev > 0)
                target_token_id   = None
                target_outcome    = "UP" if want_up else "DOWN"
                up_aliases   = ("UP", "YES", "HIGHER", "ABOVE")
                down_aliases = ("DOWN", "NO", "LOWER", "BELOW")
                for tok in tokens:
                    if not isinstance(tok, dict):
                        continue
                    out = (tok.get("outcome", "") or "").upper()
                    tid = tok.get("token_id") or tok.get("id")
                    if not tid:
                        continue
                    if want_up and out in up_aliases:
                        target_token_id = tid
                        target_outcome  = out
                        break
                    if not want_up and out in down_aliases:
                        target_token_id = tid
                        target_outcome  = out
                        break
                # Fallback: outcome metadata missing → use first token
                if not target_token_id:
                    tok = tokens[0]
                    target_token_id = tok.get("token_id") if isinstance(tok, dict) else str(tok)
                    target_outcome  = (tok.get("outcome", "UP") or "UP").upper() if isinstance(tok, dict) else "UP"

                if not target_token_id or target_token_id in self.open_positions:
                    return

                # T2 = last 60s (needs fresh), T3/T4 can reuse cached book for
                # several scans — cuts HTTP load from ~130 calls/scan to ~30.
                _ob_age = 500 if tier == 2 else (1000 if tier == 1 else 2000)
                book = await get_orderbook(session, target_token_id, max_age_ms=_ob_age)
                if not book:
                    skip_book += 1
                    return
                a, liq_l = best_ask_info(book, max_price=max_e)
                if a is None or a <= 0:
                    skip_silent += 1  # ask above ceiling or no valid asks — was invisible
                    return
                best_token_id = target_token_id
                best_ask      = a
                best_liq      = liq_l
                tok_outcome   = target_outcome
            else:
                # ── Legacy v7 path: fetch both orderbooks, pick cheaper ──
                _ob_age = 500 if tier == 2 else (1000 if tier == 1 else 2000)
                for tok in tokens:
                    if isinstance(tok, dict):
                        tid = tok.get("token_id") or tok.get("id")
                        out = tok.get("outcome", "UP").upper()
                    else:
                        tid = str(tok); out = "UP"
                    if not tid or tid in self.open_positions:
                        continue
                    book = await get_orderbook(session, tid, max_age_ms=_ob_age)
                    if not book:
                        continue
                    a, liq_l = best_ask_info(book, max_price=max_e)
                    if a is None or a <= 0:
                        continue
                    if best_ask is None or a < best_ask:
                        best_ask      = a
                        best_liq      = liq_l
                        best_token_id = tid
                        tok_outcome   = out
                if not best_token_id and tokens:
                    tok = tokens[0]
                    best_token_id = tok.get("token_id") if isinstance(tok, dict) else str(tok)
                    tok_outcome   = tok.get("outcome", "UP").upper() if isinstance(tok, dict) else "UP"
                    book = await get_orderbook(session, best_token_id, max_age_ms=_ob_age)
                    if book:
                        best_ask, best_liq = best_ask_info(book, max_price=max_e)

            token_id  = best_token_id
            ask       = best_ask
            liq       = best_liq
            buying_up = tok_outcome.upper() in ("UP","YES","HIGHER","ABOVE")

            # Direction agreement — soft check only (brain data mode: both sides open).
            # T1 contrarian: aligned = buying_up when dev<0, or not buying_up when dev>0.
            # T2 oracle-lag: aligned = buying_up when dev>0, or not buying_up when dev<0.
            # We no longer hard-kill misaligned direction — the confidence formula's
            # dev_score already penalises weak/wrong-direction signals. The brain needs
            # loss data on both sides to learn directional bias per coin per hour.
            if tier == 1:
                _dir_aligned = (buying_up and dev < 0) or (not buying_up and dev > 0)
            else:
                _dir_aligned = (buying_up and dev > 0) or (not buying_up and dev < 0)
            # _dir_aligned used downstream by score_market dev_score weighting.
            # No hard kill here — let confidence + ghost brain gate quality.

            # ── Enhanced trend detection: velocity + momentum + volatility ──────
            # Tier-aware: T4 daily markets skip this filter entirely — their edge
            # is the 24h open deviation, not 10-tick (seconds) momentum.
            # T1 (contrarian) uses a relaxed momentum threshold (half of T2).
            # T2 (5-min oracle-lag) uses the full strict filter.
            _vel = _mom = _vol = None
            _trend_applies = TREND_ENHANCED and tier in (1, 2)
            if _trend_applies:
                _n   = TREND_N_TICKS
                _vel = self.tracker.get_velocity(coin, _n)
                _mom = self.tracker.get_momentum(coin, _n)
                _vol = self.tracker.get_volatility(coin, _n)

                # Tier-specific momentum threshold: T3 is more relaxed
                _mom_min = TREND_MOMENTUM_MIN if tier == 2 else TREND_MOMENTUM_MIN * 0.5

                if _vel is None or _mom is None or _vol is None:
                    # Insufficient tick history — let through during warmup
                    pass
                else:
                    # Velocity: price must be moving in the trade direction.
                    # T1 (WRAITH): velocity magnitude gate applies — contrarian needs real momentum.
                    # T2 (SPECTER): velocity magnitude gate REMOVED — oracle-lag edge exists even in
                    #   slow markets; stale MMs haven't repriced regardless of Binance speed.
                    #   Direction gate still applies (buying direction must match Binance move).
                    if tier != 2 and abs(_vel) < TREND_VELOCITY_MIN:
                        skip_vel += 1
                        return  # price stalled — no edge in either direction
                    # T2 only: vel must agree with direction (stale-ask window — reversal is fatal).
                    # T1 contrarian: vel direction exempt — market-start dev is the primary signal.
                    if tier == 2:
                        if (buying_up and _vel < 0) or (not buying_up and _vel > 0):
                            skip_vel += 1
                            return  # price moving opposite to our trade direction

                    # Momentum: are enough recent ticks aligned? (T3 threshold halved)
                    if abs(_mom) < _mom_min:
                        skip_cert += 1
                        return  # choppy / oscillating — not a clean trend
                    # T2 only: momentum direction must agree.
                    # T1 contrarian: momentum direction exempt (wider window, dev is primary signal).
                    if tier == 2:
                        if (buying_up and _mom < 0) or (not buying_up and _mom > 0):
                            skip_cert += 1
                            return  # net tick direction contradicts our bet

                    # Volatility: market alive but not spiking?
                    if _vol < TREND_VOLATILITY_MIN:
                        skip_cert += 1
                        return  # flat/dead market
                    if _vol > TREND_VOLATILITY_MAX:
                        skip_cert += 1
                        return  # extreme spike noise
            # T4 daily: trend filter bypassed — edge is 24h open deviation only
            # ────────────────────────────────────────────────────────────────────

            # ── Whole-coin skip (independent of BIAS_FILTERS) ──
            _coin_u = (coin or "").upper()
            if _coin_u in SKIP_COINS:
                skip_cert += 1
                return  # silent — counted in scan summary

            # ── Per-coin daily trade cap — stop hammering same coin all day ──
            if MAX_TRADES_PER_COIN_DAY > 0:
                _daily_coin = get_daily_coin_trades(_coin_u)
                if _daily_coin >= MAX_TRADES_PER_COIN_DAY:
                    skip_cert += 1
                    return  # silent — daily cap hit for this coin

            # ── Bias-aware filters (data-backed, on actual cheap side) ──
            if BIAS_FILTERS_ENABLED:
                _h       = _et_hour()
                if COIN_DIR.is_blocked(_coin_u, buying_up, tier):
                    skip_cert += 1
                    return  # silent — CoinDirEngine auto-blocked this coin+direction
                if buying_up and _h in DEAD_UP_HOURS_ET:
                    skip_cert += 1
                    return  # silent
                if not buying_up and _h in DEAD_DOWN_HOURS_ET:
                    skip_cert += 1
                    return  # silent
                # ── Ghost Memory hour bias gate (v2: coin-aware) ───────────
                _utc_h = datetime.now(timezone.utc).hour
                if HOUR_BIAS.should_skip(buying_up, _utc_h, _coin_u):
                    skip_cert += 1
                    return  # silent

            # Trend filter already gated above. Just final invariants.
            if not token_id or ask is None or ask <= 0:
                skip_silent += 1  # fallback invariant — no valid token/ask after both paths
                return
            if token_id in self.open_positions:
                return

            # max_e and intended_size already computed above (before orderbook fetch)
            no_implied = round(1 - ask, 4)

            # Silent pre-checks — counted in scan summary, not printed per-market
            if ask > max_e:
                skip_ask += 1
                return
            # T4 resolution arb: must be in the near-certain zone (94-99¢).
            # Below 94¢ the market isn't settled yet — skip until MM leaves stale ask.
            if tier == 4 and ask < T4_MIN_ENTRY:
                skip_ask += 1
                return
            # Entry floor gate — T2 and T3 have separate floors (different dead zones).
            # T2_MIN_ENTRY=0.10 (ep<0.10 = 2.9%WR dead zone, -$249 on 104 trades)
            # T1_MIN_ENTRY=0.15 (ep<0.15 = losing zone confirmed in live data)
            _min_e = T1_MIN_ENTRY if tier == 1 else (T2_MIN_ENTRY if tier == 2 else 0.0)
            if _min_e > 0 and ask < _min_e:
                skip_ask += 1
                return
            # oracle-lag-sniper all tiers: MIN_NO_PRICE gate disabled.
            # Ask ceiling (T*_MAX_ENTRY) is the price gate; certainty floor adds no value.
            if False and no_implied < MIN_NO_PRICE:
                skip_cert += 1
                return
            # 5m certainty gate — active (hourly MIN_NO_PRICE gate is disabled)
            if _is_5m and no_implied < T2_5M_MIN_NO:
                skip_cert += 1
                return
            # Cumulative depth check: liq = sum of USDC at ask levels ≤ max_e.
            # Must cover the full intended bet size to avoid partial fills.
            # e.g. T2 $3 bet: need ≥ $3.00 of cumulative depth at prices ≤ $0.025.
            if liq < intended_size * LIQ_MULT:
                skip_liq += 1
                return

            # ── Oracle discrepancy gate (T2 + T3) ────────────────────────────────
            # Require Binance's move to be large RELATIVE to what Polymarket is
            # pricing for our side. Score = abs(dev) / ask.
            #   High score = Binance moved a lot but market is still cheap → real edge
            #   Low score  = Binance barely moved or market is already expensive → skip
            # Both direction (signed dev vs buying_up) AND magnitude (score threshold)
            # must agree — a coin flat on Binance but cheap on Polymarket is noise.
            # Oracle discrepancy gate: T2 only (ask ≤ $0.10 range).
            # At T3/T4 higher ask prices disc_score = dev/ask is naturally low even
            # for valid oracle-lag setups — gate only makes sense in the cheap zone.
            # T2 data: XRP disc≈0.433 (pass), BTC disc≈0.109 (borderline, now cut at 0.10).
            disc_score = abs(dev) / ask if ask > 0 else 0.0
            if ORACLE_DISC_ENABLED and tier == 2 and disc_score < ORACLE_DISC_MIN_RATIO:
                skip_cert += 1
                return

            # ── Oracle arbitrage score (PATH B) — runs FIRST so fair_p ──────────
            # feeds the mismatch component of confidence (highest weight: 0.25).
            fair_p, mismatch, arb_score = compute_arb_score(
                coin=coin, dev=dev, ask=ask, secs=secs, tier=tier, velocity=_vel,
                buying_up=buying_up,
            )
            # ── Pre-trade signal quality hard limits ─────────────────────────
            # Run BEFORE compute_confidence to reject clearly bad signals without
            # wasting cycles on the full 6-component calculation.
            #
            # Timing + price compensator: slight signals still pass when BOTH:
            #   (a) candle is in the sweet spot window (oracle lag freshest)
            #   (b) entry price is cheap (favorable payout, lower part of range)
            # This prevents over-penalizing micro-edge signals at optimal moments.
            _cprog        = 1.0 - (secs / T2_CANDLE_SECS) if (tier == 2 and T2_CANDLE_SECS > 0) else 1.0
            _sweet_timing = CANDLE_SWEET_LO <= _cprog <= CANDLE_SWEET_HI
            _cheap_entry  = ask <= max_e * TIMING_PRICE_CHEAP_THRESH
            _tp_ok        = TIMING_PRICE_OVERRIDE and tier == 2 and _sweet_timing and _cheap_entry
            # filter 1 — bypassed all tiers (oracle-lag-sniper).
            # Gaussian fair_p underestimates true WR for stale MM asks across all tiers.
            _km_score = min(abs(fair_p - ask) / max(1.0 - ask, 0.01), 1.0)
            if False and _km_score < _adap_eff_mismatch() and not _tp_ok:
                skip_cert += 1
                return
            # filter 2 — bypassed all tiers (oracle-lag-sniper).
            # MIN_TREND_STRENGTH already enforces a dev floor upstream; double-gating
            # with KILL_DEV_MIN blocks valid oracle-lag signals on T1/T2/T4.
            _kd_min   = {2: 0.001, 1: 0.005, 4: 0.004}.get(tier, 0.001)
            _kd_score = min(abs(dev) / (5.0 * _kd_min), 1.0) if _kd_min > 0 else 1.0
            if False and _kd_score < KILL_DEV_MIN and not _tp_ok:
                skip_cert += 1
                return
            # filter 3 — liquidity: depth must cover at least LIQ_MULT x bet size
            # 1.2x = 20% slippage buffer. Full score when liq >= LIQ_MULT * size.
            _kl_score = min(liq / (LIQ_MULT * max(intended_size, 1.0)), 1.0)
            if _kl_score < KILL_LIQ_MIN:
                skip_liq += 1
                return
            # filter 4 — bad price zone: handled upstream by T*_MAX_ENTRY checks
            # filter 5 — late noise: hard floor across all tiers (T2 also has T2_MIN_SECS_LEFT)
            if secs < KILL_SECS_MIN:
                skip_cert += 1
                return
            # filter 6 — conflict: same coin+tier+direction fired recently
            # Akey et al: elite signals (arb_score >= ARB_OVERRIDE_SCORE) bypass cooldown.
            # Top-1% bots fire 40× more than retail — execution speed IS the edge.
            _fire_key = (coin, tier, "up" if buying_up else "down")
            _fire_age = time.monotonic() - _recent_fires.get(_fire_key, 0.0)
            _elite_bypass = ELITE_COOLDOWN_BYPASS and arb_score >= ARB_OVERRIDE_SCORE
            if _fire_age < KILL_COOLDOWN_SECS and not _elite_bypass:
                skip_cert += 1
                return

            # filter 7 — depth imbalance: skip if book is strongly against our direction
            # Enabled when KILL_DEPTH_IMBALANCE > -1.0. Conservative default = disabled.
            if KILL_DEPTH_IMBALANCE > -1.0:
                _imb = book_depth_imbalance(book)
                # For UP bets: negative imbalance means sell pressure; very negative = skip
                # For DOWN bets: flip sign so same threshold applies in both directions
                _dir_imb = _imb if buying_up else -_imb
                if _dir_imb < KILL_DEPTH_IMBALANCE:
                    skip_cert += 1
                    return

            # filter 8 — dev ceiling: underlying moved too much → oracle likely updated
            # Backtest: BTC_move >= 0.07% → 0% WR even in best entry range (0.05-0.08).
            if KILL_DEV_MAX > 0 and abs(dev) > KILL_DEV_MAX:
                skip_cert += 1
                return

            # filter 9 — velocity ceiling: BTC moving too fast = market makers repriced
            # Micro-lag arb edge lives at LOW velocity (oracle just starting to lag).
            if KILL_VELOCITY_MAX > 0 and _vel is not None and abs(_vel) > KILL_VELOCITY_MAX:
                skip_cert += 1
                return

            # ── Confidence score (6-component) ────────────────────────────────
            # dev(0.30) + mismatch(0.25) + liq(0.15) + price(0.15)
            # + time(0.10) + momentum(0.05). Trades below CONFIDENCE_MIN (0.65)
            # are rejected. fair_p from ARB engine feeds mismatch_score directly.
            confidence = compute_confidence(
                tier=tier, dev=dev, liq=liq, intended_size=intended_size,
                ask=ask, max_e=max_e, secs=secs, mins=mins,
                fair_p=fair_p, velocity=_vel,
            )
            # Direction alignment penalty (8% discount for misaligned direction).
            # Brain data mode: both sides fire, but aligned direction is preferred.
            # Not a hard kill — confidence gate still decides. Wires _dir_aligned into score.
            if not _dir_aligned:
                confidence *= 0.92
            # Hour confidence weight -- boost high-EV hours, reduce negative-EV hours
            _hw = _et_hour()
            if HOUR_BOOST_FACTOR != 1.0 and _hw in HOUR_BOOST_HOURS_ET:
                confidence = min(confidence * HOUR_BOOST_FACTOR, 1.0)
            elif HOUR_REDUCE_FACTOR != 1.0 and _hw in HOUR_REDUCE_HOURS_ET:
                confidence = confidence * HOUR_REDUCE_FACTOR

            # oracle-lag-sniper all tiers: confidence gate bypassed.
            # Model underscores price_score (= 0 near ceiling) and mismatch component
            # (Gaussian underestimates true WR on stale MM asks). Disabled across all tiers.
            if False and confidence < _adap_eff_conf():
                skip_cert += 1
                return

            # ── Dynamic position sizing ────────────────────────────────────────
            # Scale USDC bet size based on composite signal quality (confidence
            # + arb_score blend). Upsizes elite signals, downsizes marginal ones.
            # Cap by available liquidity so we never request more than the book
            # can fill (leaves a 10% buffer above minimum depth).
            dyn_size = get_dynamic_size(intended_size, confidence, arb_score)
            dyn_size = min(dyn_size, liq * 0.90)   # don't exceed 90% of book depth
            dyn_size = max(round(dyn_size, 2), 1.0) # floor at $1

            # ── Expected ROI gate ──────────────────────────────────────────────
            # Probability-weighted, confidence-adjusted ROI gate.
            # OLD (broken): used confidence/ask — treated quality score as win prob,
            #   inflating EV by 20-50x for cheap tokens -> 1302% "expected" ROI.
            # NEW: fair_p = actual model win probability (Gaussian ARB engine).
            #   confidence = signal quality discount applied on top.
            #
            # adj_roi_rate = (fair_p/ask - 1) * confidence
            #   fair_p/ask - 1  = probability-weighted payout minus cost (true EV)
            #   * confidence    = quality discount (penalises uncertain signals)
            #   normalized      = per-dollar rate, independent of position size
            #
            # Example: fair_p=0.70, ask=0.06, conf=0.68 -> 10.67 * 0.68 = 7.25
            #          fair_p=0.53, ask=0.06, conf=0.65 ->  7.83 * 0.65 = 5.09
            #          fair_p=0.51, ask=0.08, conf=0.65 ->  5.38 * 0.65 = 3.49 (marginal)
            # oracle-lag-sniper all tiers: ROI gate bypassed.
            # Gaussian fair_p underestimates true WR at higher ask prices — adj_roi
            # fails MIN_EXPECTED_ROI even when empirical WR is strongly positive.
            if False and ask > 0:
                _adj_roi_rate = (fair_p / ask - 1.0) * confidence
                if _adj_roi_rate < MIN_EXPECTED_ROI:
                    skip_cert += 1
                    return

            # ── Whale signal boost ─────────────────────────────────────────────
            # If a discovery wallet recently traded this market in the same
            # direction, boost confidence (+WHALE_CONF_BOOST) and priority.
            # "both" directions (whale hedging) = no directional preference.
            _cid = m.get("id", "") or m.get("conditionId", "")
            _whale_hit = _whale_signals.get(_cid)
            if _whale_hit:
                _whale_dir = _whale_hit.get("direction", "both")
                _our_dir   = "up" if buying_up else "down"
                if _whale_dir == "both":
                    pass  # whale hedging — neutral, no boost
                elif _whale_dir == _our_dir:
                    confidence = min(confidence + WHALE_CONF_BOOST, 1.0)
                # Direction mismatch: mild confidence reduction (whale went other way)
                else:
                    confidence = max(confidence - 0.02, 0.0)

            # Arb-amplified priority: arb_score [0,1] adds up to +100% boost
            _whale_priority_mult = (WHALE_PRIORITY_BOOST
                                    if _whale_hit and _whale_hit.get("direction") != "both"
                                    else 1.0)
            priority_score = disc_score * no_implied * (1.0 + arb_score) * _whale_priority_mult
            # Override requires BOTH statistical confidence (arb_score) AND
            # confirmed active velocity — price must still be moving fast,
            # not just have moved. Prevents low-velocity stale signals from
            # bypassing TOP_N even when the statistical mismatch is large.
            arb_override   = False  # ARB_ENABLED=false

            # ── Tick freshness boost ─────────────────────────────────────────
            # Fresh Binance move (<3s) = oracle lag almost certainly still open.
            # Stale move (>45s) gets mild priority penalty vs concurrent fresh
            # signals so the top-N always favours the most timely opportunity.
            _direction = "up" if buying_up else "down"
            _freshness = (LATENCY_TRACKER.get_freshness_mult(coin, _direction)
                          if LATENCY_TRACK_ENABLED else 1.0)
            priority_score *= _freshness

            # ── Hard freshness gate ──────────────────────────────────────────────
            # Soft multiplier above only re-orders signals; stale signals still fire.
            # This hard gate KILLS signals where the oracle lag window has expired.
            # Research: window ≈55s; production move_age P50=88s → firing post-window.
            # Mark this (coin,tier,direction) as recently fired for conflict filter
            _recent_fires[_fire_key] = time.monotonic()

            return {
                "tier":           tier,
                "coin":           coin,
                "token_id":       token_id,
                "market":         m,
                "ask":            ask,
                "outcome":        tok_outcome,
                "no_implied":     no_implied,
                "liq":            liq,
                "mins":           mins,
                "secs":           secs,
                "dev":            dev,
                "disc_score":     disc_score,
                "priority_score": priority_score,
                "confidence":     confidence,
                "dyn_size":       dyn_size,
                "seg_mult":       1.0,
                # Arb engine metrics (PATH B)
                "fair_p":         fair_p,
                "mismatch":       mismatch,
                "arb_score":      arb_score,
                "arb_override":   arb_override,
                # Enhanced trend metrics (None when TREND_ENHANCED=false or warming up)
                "velocity":       _vel,
                "momentum":       _mom,
                "volatility":     _vol,
                "signal_ts":      time.monotonic(),
                "freshness":      _freshness,
                "ghost_score":    _gb_score,
            }


        # ── Collect all valid signals across every market in this cycle ──────
        raw = await asyncio.gather(*[check_market(m) for m in markets])
        all_signals = [s for s in raw if s is not None]
        signals = len(all_signals)

        # ── Ghost scorer: log utility score for every signal (data only, no gate) ──
        # Fired flag set True for top-N that will actually trade.
        _top_n_ids = {id(s) for s in all_signals[:TOP_SIGNALS_PER_CYCLE]}
        for _gs_sig in all_signals:
            _gs_log_signal_score(_gs_sig, fired=id(_gs_sig) in _top_n_ids)

        # ── Rank by priority score (desc) — best edge fires first ────────────
        all_signals.sort(key=lambda s: s["priority_score"], reverse=True)

        # ── Print every ranked candidate so you can see what was skipped ─────
        ts_now = datetime.now().strftime("%H:%M:%S")
        for rank, sig in enumerate(all_signals, 1):
            _override    = sig.get("arb_override", False)
            will_fire    = rank <= TOP_SIGNALS_PER_CYCLE or _override
            tag   = "⚡" if _override and rank > TOP_SIGNALS_PER_CYCLE else ("★" if will_fire else "·")
            label = f"ARB #{rank}" if _override and rank > TOP_SIGNALS_PER_CYCLE else (
                    f"FIRE #{rank}" if will_fire else f"SKIP #{rank}")
            # Build optional enhanced-trend suffix
            _trend_str = ""
            if sig.get("velocity") is not None:
                _trend_str = (f" vel={sig['velocity']*1e6:+.1f}µ"
                              f" mom={sig['momentum']:+.2f}"
                              f" vol={sig['volatility']*1e4:.1f}‱")
            # Build arb suffix
            _arb_str = ""
            if sig.get("arb_score", 0) > 0:
                _arb_str = (f" fair={sig['fair_p']:.3f}"
                            f" miss={sig['mismatch']:.3f}"
                            f" arb={sig['arb_score']:.3f}")
            # Build size annotation: base → dyn with multiplier
            _base_raw = self.get_size(sig["tier"], sig["coin"])
            _dyn_sz   = sig.get("dyn_size", _base_raw)
            _mult     = _dyn_sz / _base_raw if _base_raw > 0 else 1.0
            _sz_str   = f" sz=${_dyn_sz:.2f}({_mult:.1f}x)"
            # Segment multiplier annotation
            _sm = sig.get("seg_mult", 1.0)
            _seg_str = f" seg={_sm:.2f}" if _sm != 1.0 else ""
            print(f"[{ts_now}] {tag} {label}/{len(all_signals)} "
                  f"T{sig['tier']} {sig['coin']} {sig['outcome']} | "
                  f"conf={sig['confidence']:.2f} score={sig['priority_score']:.4f} "
                  f"ask={sig['ask']:.3f} dev={sig['dev']:+.3%} "
                  f"disc={sig['disc_score']:.3f} cert={sig['no_implied']:.0%} "
                  f"liq=${sig['liq']:.1f}{_sz_str}{_seg_str}{_arb_str}{_trend_str}")

        # ── Fire top N + any ARB overrides beyond the cap ───────────────────
        # ARB overrides bypass TOP_SIGNALS_PER_CYCLE when arb_score is high
        # enough (>= ARB_OVERRIDE_SCORE). They are deduplicated so a signal
        # already in the top-N doesn't fire twice.
        _top_ids  = {id(s) for s in all_signals[:TOP_SIGNALS_PER_CYCLE]}
        _arb_extra = [s for s in all_signals[TOP_SIGNALS_PER_CYCLE:]
                      if s.get("arb_override") and id(s) not in _top_ids]
        _fire_queue = list(all_signals[:TOP_SIGNALS_PER_CYCLE]) + _arb_extra

        for sig in _fire_queue:
            _is_override = sig.get("arb_override") and id(sig) not in {id(s) for s in all_signals[:TOP_SIGNALS_PER_CYCLE]}
            _arb_tag = (f" ⚡ARB override arb={sig.get('arb_score',0):.3f}"
                        f" fair={sig.get('fair_p',0):.3f}" if _is_override else
                        f" arb={sig.get('arb_score',0):.3f} fair={sig.get('fair_p',0):.3f}")
            log_event("🎯", f"Signal T{sig['tier']} {sig['coin']} {sig['outcome']} "
                             f"ask={sig['ask']:.3f} certainty={sig['no_implied']:.0%} "
                             f"mins={sig['mins']:.1f}")
            if await self.fire_trade(
                    sig["tier"], sig["coin"], sig["token_id"],
                    sig["market"], sig["ask"],
                    outcome=sig["outcome"], session=session,
                    size=sig.get("dyn_size"),
                    ghost_score=sig.get("ghost_score", 0)):
                fired += 1
                await asyncio.sleep(0.3)

        return fired, skip_ask, skip_liq, skip_cert, skip_win, signals, skip_silent, skip_vel, skip_book, skip_ws_qual

    async def refresh_opens_periodically(self):
        async with aiohttp.ClientSession() as session:
            while self.running:
                await self.tracker.fetch_opens(session)
                await asyncio.sleep(60)   # refresh every 60s (was 900) so opens stay current

    async def refresh_markets_periodically(self):
        """Background task: keeps _market_cache warm every 30s.
        Main loop never waits on Gamma API — sub-100ms scans."""
        global _market_cache
        await asyncio.sleep(5)
        connector = aiohttp.TCPConnector(
            limit=10, limit_per_host=10, keepalive_timeout=60,
            ttl_dns_cache=300, use_dns_cache=True, ssl=False,
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            while self.running:
                try:
                    fresh = await _discover_crypto_markets(session)
                    _market_cache["token_markets"] = fresh
                    _market_cache["ts"] = time.time()
                except Exception as e:
                    print(f"[WARN] Market refresh bg: {e}")
                await asyncio.sleep(10)  # 10s (was 30s) — catch new T2 markets entering window faster

    async def refresh_positions_periodically(self):
        # Checks positions immediately on WS fill events,
        # or every POSITION_POLL_INTERVAL seconds as a REST fallback.
        connector = aiohttp.TCPConnector(
            limit=5, limit_per_host=5, keepalive_timeout=60,
            ttl_dns_cache=300, use_dns_cache=True, ssl=False,
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            while self.running:
                try:
                    await self.check_positions(session)
                except Exception as e:
                    print(f"[WARN] Position bg check: {e}")
                try:
                    await asyncio.wait_for(
                        USER_WS.fill_event.wait(),
                        timeout=POSITION_POLL_INTERVAL,
                    )
                    USER_WS.fill_event.clear()
                except asyncio.TimeoutError:
                    pass

    async def run(self):
        global _TICK_SCAN_EVENT
        _TICK_SCAN_EVENT = asyncio.Event()   # F1: tick-triggered scan event

        USER_WS.start()
        ws_task        = asyncio.create_task(self.tracker.run_websocket())
        opens_task     = asyncio.create_task(self.refresh_opens_periodically())
        markets_task   = asyncio.create_task(self.refresh_markets_periodically())
        news_task      = asyncio.create_task(NEWS_MONITOR.run())
        clob_health_task  = asyncio.create_task(clob_health_monitor())
        hour_bias_task    = asyncio.create_task(HOUR_BIAS.run_refresh_loop())
        coin_dir_task     = asyncio.create_task(COIN_DIR.refresh_loop())
        # BUG FIX: WS_CLIENT was instantiated but start() was never called.
        # Without this, the entire WS orderbook loop never ran — every orderbook
        # fetch fell through to HTTP (50-200ms). With it, books are WS-pushed
        # into _orderbook_cache at ~5-20ms, matching v8's 30-150ms scan target.
        if WS_ENABLED:
            asyncio.create_task(WS_CLIENT.start())
        positions_task = asyncio.create_task(self.refresh_positions_periodically())

        print("[Init] Waiting for Binance price feed...")
        for _ in range(30):
            if self.tracker.prices:
                break
            await asyncio.sleep(1)

        print(f"[Init] Prices: {self.tracker.prices}")

        connector = aiohttp.TCPConnector(
            limit=40, limit_per_host=20, keepalive_timeout=60,
            ttl_dns_cache=300, use_dns_cache=True, ssl=False,
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            while self.running:
                t0 = time.time()
                self.scan_count += 1

                # ── Periodic CLOB balance refresh (every 30 scans ≈ 30s) ────────
                if self.scan_count % 30 == 1 and not PAPER_TRADE:
                    try:
                        from py_clob_client_v2.clob_types import (
                            BalanceAllowanceParams, AssetType)
                        _br = self.client.get_balance_allowance(
                            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                        _raw = float(_br.get("balance", 0)) if isinstance(_br, dict) else 0.0
                        self._last_balance = _raw / 1_000_000
                    except Exception:
                        pass

                # ── Live stats JSON dump (every 60 scans ≈ 60s) ─────────────────
                if self.scan_count % 60 == 0:
                    try:
                        import json as _json
                        # Runtime stats (always available — no DB needed)
                        _stats_payload = {
                            "ts": datetime.now().isoformat(),
                            "balance": self._last_balance,
                            "scan_count": self.scan_count,
                            "session_pnl": round(self.session_pnl, 2),
                            "trades_fired": self.trades_fired,
                            "loss_streak": self.consecutive_losses,
                            "prices": {k: v for k, v in self.tracker.prices.items() if v},
                            "recent_trades": [],
                            "all_time_trades": None,
                            "all_time_wins": None,
                            "all_time_pnl": None,
                            "today_trades": None,
                            "today_wins": None,
                            "today_pnl": None,
                        }
                        # DB stats (best-effort — skip if locked)
                        try:
                            _con = _db(timeout=1)
                            _cur = _con.cursor()
                            _cur.execute(
                                "SELECT id,ts,tier,coin,direction,entry_price,"
                                "size_usdc,outcome,pnl FROM trades "
                                "ORDER BY id DESC LIMIT 30")
                            _cols = [d[0] for d in _cur.description]
                            _stats_payload["recent_trades"] = [
                                dict(zip(_cols, row)) for row in _cur.fetchall()]
                            _cur.execute(
                                "SELECT COUNT(*),"
                                "SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END),"
                                "ROUND(SUM(pnl),2) FROM trades WHERE ts>='2026-05-01'")
                            _r = _cur.fetchone()
                            _stats_payload.update({
                                "all_time_trades": _r[0],
                                "all_time_wins": _r[1],
                                "all_time_pnl": _r[2],
                            })
                            _cur.execute(
                                "SELECT COUNT(*),"
                                "SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END),"
                                "ROUND(SUM(pnl),2) FROM trades WHERE ts>=date('now')")
                            _r2 = _cur.fetchone()
                            _stats_payload.update({
                                "today_trades": _r2[0],
                                "today_wins": _r2[1],
                                "today_pnl": _r2[2],
                            })
                            _con.close()
                        except Exception:
                            pass
                        _stats_path = os.path.join(SCRIPT_DIR, "live_stats.json")
                        _stats_tmp  = _stats_path + ".tmp"
                        with open(_stats_tmp, "w") as _f:
                            _json.dump(_stats_payload, _f, indent=2)
                            _f.flush()
                            os.fsync(_f.fileno())
                        os.replace(_stats_tmp, _stats_path)
                    except Exception:
                        pass

                fired     = 0
                skip_ask  = 0
                skip_liq  = 0
                skip_cert = 0
                skip_win  = 0
                signals   = 0
                skip_silent = 0
                skip_vel  = 0
                skip_book = 0
                skip_ws_qual = 0
                ws_token_ids: List[str] = []

                t234_fired = 0
                _sa = _sl = _sc = _sw = _sig = _ss = _sv = _sb = _swq = 0
                try:
                    markets = await asyncio.wait_for(
                        fetch_crypto_markets(session), timeout=20.0)

                    if PREWARM_ORDERBOOKS or WS_ENABLED:
                        prewarm_token_ids: List[str] = []
                        for mkt in markets:
                            secs = mkt.get("_secs_left") or 0
                            if secs < 90 and secs > 0:
                                for tok in (mkt.get("tokens") or []):
                                    if not isinstance(tok, dict):
                                        continue
                                    tid = tok.get("token_id") or tok.get("id")
                                    if not tid:
                                        continue
                                    if PREWARM_ORDERBOOKS:
                                        prewarm_token_ids.append(str(tid))
                                    if WS_ENABLED:
                                        ws_token_ids.append(str(tid))
                        if prewarm_token_ids:
                            asyncio.create_task(
                                batch_prewarm_orderbooks(session, prewarm_token_ids))
                        if ws_token_ids and WS_ENABLED:
                            asyncio.create_task(WS_CLIENT.subscribe(ws_token_ids))

                    result = await asyncio.wait_for(
                        self.scan_updown_markets(session, markets), timeout=30.0)
                    if isinstance(result, tuple):
                        t234_fired, _sa, _sl, _sc, _sw, _sig, _ss, _sv, _sb, _swq = result
                    else:
                        t234_fired = result or 0
                        _sa = _sl = _sc = _sw = _sig = _ss = _sv = _sb = _swq = 0

                    fired        += t234_fired
                    skip_ask     += _sa
                    skip_liq     += _sl
                    skip_cert    += _sc
                    skip_win     += _sw
                    signals      += _sig
                    skip_vel     += _sv
                    skip_ws_qual += _swq

                except asyncio.TimeoutError:
                    log_event("\u26a0\ufe0f", "scan_updown_markets timeout")
                except Exception as e:
                    log_event("\u26a0\ufe0f", f"scan error: {e}")

                elapsed = time.time() - t0
                prices_str = "  ".join(
                    f"{c}=${p:,.0f}" for c, p in self.tracker.prices.items())
                _balance_str = (f"  Bal:${self._last_balance:.2f}"
                                if self._last_balance is not None else "")
                _risk_str = ""
                if ADAPTIVE_RISK and self.consecutive_losses > 0:
                    if (self.pause_until is not None
                            and self.pause_until > time.monotonic()):
                        rem = (self.pause_until - time.monotonic()) / 60
                        _risk_str = f"  \u26d4PAUSED({rem:.0f}m)"
                    elif self.consecutive_losses >= LOSS_STREAK_REDUCE:
                        _risk_str = (f"  \u26a0{self.consecutive_losses}L"
                                     f"@{LOSS_SIZE_FACTOR:.0%}")
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Scan {self.scan_count}"
                    f" | Fired:{fired} | Skip ask:{skip_ask} cert:{skip_cert}"
                    f" liq:{skip_liq} win:{skip_win} vel:{skip_vel}"
                    f" wsq:{skip_ws_qual} | Sig:{signals}"
                    f" | {elapsed * 1000:.0f}ms{_balance_str}{_risk_str}"
                )
                print(f"         {prices_str}")
                log_scan_stats(len(markets), signals, skip_ask, skip_liq,
                               skip_cert, skip_win, int(elapsed * 1000),
                               skip_vel)
                await asyncio.sleep(SCAN_INTERVAL)


async def main():
    scanner = CryptoGhostScanner()
    await scanner.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOPPED] GHOST LATTICE stopped.")
