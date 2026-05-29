# World Event Bot

A fully local, demo-ready paper trading bot for [Polymarket](https://polymarket.com) prediction markets, focused on **World Events, Government, Geopolitics, Economy, Tech, and AI** markets.

> **Paper trading only.** No real money, no wallet, no private keys.

> **Doctrine note for AI agents and contributors:** This README is for human orientation only. The authoritative source for signal thresholds, filters, API contracts, and DB schema is [`_config/`](_config/). The project follows [ICM](https://github.com/RinDig/Interpreted-Context-Methdology) (Jake Van Clief) + the Boris Cherny CLAUDE.md workflow — read [CLAUDE.md](CLAUDE.md) and [CONTEXT.md](CONTEXT.md) before changing code. Do not edit thresholds here without updating the matching `_config/*.md` file.

---

## Features

| Area | Details |
|------|---------|
| **Market Scanner** | Pulls 100 active events via Gamma API, filters crypto/weather/sports, applies CLOB quality gate |
| **Liquidity Scoring** | Spread, bid/ask depth, book balance — scored 0–100 |
| **Signal Engine** | 4 rule-based signals (underpriced YES, underpriced NO, near-certain, balanced probe) |
| **Paper Trader** | Simulated fills, confidence-scaled sizing, position tracking |
| **P&L Engine** | Unrealized + realized P&L, win rate, cumulative chart |
| **Dashboard** | 6-tab Streamlit UI (Scanner, Liquidity, Trades, Positions, P&L, Logs) |
| **Storage** | SQLite — markets, positions, trades, signals, logs |

---

## Project Structure

```
polymarket_event_bot/
├── app.py              # Streamlit dashboard (entry point)
├── config.py           # All constants — reads from .env
├── database.py         # SQLite layer (all DB access)
├── gamma_client.py     # Polymarket Gamma API client
├── clob_client.py      # Polymarket CLOB order-book client
├── liquidity_score.py  # Pure order-book metric calculations
├── market_scanner.py   # Scan pipeline (Gamma → filter → CLOB → DB)
├── signal_engine.py    # Signal generation rules
├── paper_trader.py     # Simulated trade execution
├── pnl.py              # P&L calculations
├── requirements.txt
├── .env.example
└── README.md
```

---

## Windows Setup

### Prerequisites

- Python 3.10+ — https://www.python.org/downloads/
- Git (optional) — https://git-scm.com/

### Step-by-step

**1. Open PowerShell** and navigate to the bot folder:
```powershell
cd $HOME\polymarket_event_bot
```

**2. Create a virtual environment:**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

If you get an execution policy error, run first:
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

**3. Install dependencies:**
```powershell
pip install -r requirements.txt
```

**4. Configure environment:**
```powershell
Copy-Item .env.example .env
notepad .env   # edit values if desired
```

**5. Launch the dashboard:**
```powershell
streamlit run app.py
```

Your browser will open automatically at `http://localhost:8501`.

---

## Usage Guide

### Running a scan

1. Open the **Market Scanner** tab.
2. Click **Run Full Scan** — this fetches up to 100 events, filters them, and queries the CLOB for each valid token (~1–2 minutes).
3. After the scan completes you'll see a table of valid markets with quality scores.

### Generating trades

1. After scanning, click **Generate Signals & Trade** in the Market Scanner tab.
2. The signal engine evaluates every valid market against 4 rules.
3. Signals meeting the confidence threshold are paper-traded automatically.
4. View signals in **Paper Trades** tab, open positions in **Open Positions** tab.

### Closing positions

- In **Open Positions**: expand a position, set an exit price, and click **Close**.
- Or use **Close All Positions** from the Market Scanner tab to exit everything at mid-price.

### Resetting

- **Reset Paper Account** in the sidebar wipes all trades/positions and restores starting balance.
- **Clear Market Cache** removes scanned market data so the next scan starts fresh.

---

## Filters Applied

### Excluded (hard reject)
- Crypto: bitcoin, ethereum, BTC, ETH, Solana, DeFi, NFT, blockchain…
- Weather: hurricane, earthquake, temperature, flood…
- Sports: NFL, NBA, MLB, soccer, tennis, golf, olympics, championship…

### Prioritized (targeted keywords)
- Government, geopolitics, election, economy, finance, policy
- Technology, AI, artificial intelligence, silicon valley, regulation
- Military, sanctions, NATO, nuclear, treaty, G7, G20, IMF
- Federal Reserve, inflation, GDP, interest rate, recession, tariff

### Quality gate (all must pass)
| Metric | Threshold |
|--------|-----------|
| Spread | ≤ 0.04 |
| Liquidity | ≥ $1,000 |
| Volume 24h | ≥ $2,500 |
| Order book | Must exist and not be crossed |

---

## Signal Rules

The bot uses a four-tier signal system (T0-NO, T1-LOCK, T2-STRONG, T3-MOMENTUM) tuned for same-day resolution with a structural NO bias.

**Authoritative source:** [`_config/signal-doctrine.md`](_config/signal-doctrine.md). Do not duplicate or summarize the tier thresholds here — drift between this README and the doctrine file is exactly the failure mode the doctrine file exists to prevent.

---

## APIs Used

| API | Endpoint | Auth |
|-----|----------|------|
| Gamma (events) | `https://gamma-api.polymarket.com/events` | None |
| CLOB (order book) | `https://clob.polymarket.com/book?token_id=…` | None |

Both are public, read-only endpoints.

---

## Configuration Reference

All settings live in `.env` (copy from `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `STARTING_BALANCE` | 10000 | Paper account starting cash ($) |
| `MAX_TRADE_SIZE` | 100 | Max dollar size per trade |
| `MAX_OPEN_POSITIONS` | 10 | Max simultaneous open positions |
| `MAX_SPREAD` | 0.04 | Reject markets with spread above this |
| `MIN_VOLUME_24HR` | 50000 | Reject markets below this 24h volume ($) |
| `DB_PATH` | polymarket_event_bot.db | SQLite database file path |
| `SCAN_INTERVAL` | 300 | Seconds between auto-scans (future use) |

---

## Disclaimer

This tool is for **educational and paper trading purposes only**.  
It does not connect to any wallet, does not execute real trades, and does not store any private keys or credentials.  
Prediction market trading involves significant risk. Past simulated results do not guarantee future real performance.
