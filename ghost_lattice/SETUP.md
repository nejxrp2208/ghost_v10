# GHOST LATTICE v3 — Setup Guide
## WRAITH + PHANTOM | Polymarket Crypto Oracle-Lag Bot

---

## What This Is

GHOST_LATTICE v3 is an automated trading bot for Polymarket's 5-minute and
hourly BTC / ETH / SOL / BNB / XRP Up-Down markets. It exploits the lag
between Binance real-time prices and Chainlink's on-chain oracle updates,
combined with ML-scored signal selection (ghost_brain).

- **T1 WRAITH**  — Contrarian: fires AGAINST Binance momentum on hourly markets, entry 12–20¢
- **T2 PHANTOM** — Oracle-lag: fires WITH Binance momentum, entry 1–8¢ (cheap-zone)
- Flat $10 USDC per trade by default (configurable per coin)
- Auto-spawning dashboard launches scanner, resolver, and redeemer together

---

## Requirements

- Python **3.13** (recommended — per v3 README)
- A Polymarket account funded with USDC on Polygon
- A Polygon wallet private key (MetaMask Export, or any standard wallet)
- Internet connection (Binance WS + Polymarket WS + Polygon RPC)

---

## Step 1 — Install Dependencies

```
pip install -r requirements.txt
```

This installs: `aiohttp`, `websockets`, `python-dotenv`, `urllib3`,
`py-clob-client`, `web3`, `rich`.

Windows users can alternatively double-click `config/install.bat`.

---

## Step 2 — Get Your Polymarket Credentials

1. Go to https://polymarket.com and connect your wallet.
2. Settings → API Keys → create a new API key.
3. Receive: **API Key**, **Secret**, **Passphrase**.
4. Your **Proxy Address** is on the same API settings page.
5. Your **Private Key** comes from your wallet (MetaMask → Account Details → Export).

> ⚠️ Never share your private key. The bot uses it only to sign on-chain
> redemption transactions (CTF positions → USDC).

---

## Step 3 — Configure `.env`

Copy `.env.example` to `.env` and fill in:

```ini
PRIVATE_KEY=0x<your_wallet_private_key>
POLYMARKET_PROXY_ADDRESS=0x<your_proxy_address>
CLOB_API_KEY=<your_api_key>
CLOB_SECRET=<your_secret>
CLOB_PASSPHRASE=<your_passphrase>

# Telegram (optional — leave blank to disable)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Mode
PAPER_TRADE=true

# Sizing (flat USDC per trade, per tier)
T1_SIZE_USDC=4.00
T2_SIZE_USDC=4.00
```

The v3 code has **178 optional `os.getenv()` reads** with sensible defaults —
you only need to set the variables above for a working baseline. Tune
strategy filters (`T1_MIN_ENTRY`, `T2_MIN_DEV`, `CONFIDENCE_MIN`, etc.)
by reading `GHOST_LATTICE.py` and adding the relevant lines to `.env`.

---

## Step 4 — Run

### Local (Windows / Mac / Linux) — dashboard auto-spawns the stack

```
python3.13 GHOST_LATTICE_dashboard.py
```

The dashboard spawns three subprocesses:
- `GHOST_LATTICE.py`          — main scanner
- `GHOST_LATTICE_resolver.py` — trade resolver
- `GHOST_LATTICE_redeemer.py` — on-chain redeemer

On Linux/macOS, subprocess output goes to `GHOST_LATTICE.log`,
`GHOST_LATTICE_resolver.log`, and `GHOST_LATTICE_redeemer.log` in this
folder. On Windows each spawns in a new console.

Closing the dashboard kills all spawned subprocesses (via `atexit`).

### Standalone (no dashboard)

```
python3.13 GHOST_LATTICE.py             # scanner only
python3.13 GHOST_LATTICE_resolver.py    # resolver only
python3.13 GHOST_LATTICE_redeemer.py    # redeemer only
```

### VPS (PM2)

> ⚠️ The dashboard auto-spawns processes. On a VPS with PM2 you typically
> want **only** PM2 to manage processes — run the three scripts as
> separate PM2 services and DO NOT run the dashboard, or run the
> dashboard locally pointing at the VPS DB (display-only).

```bash
pm2 start GHOST_LATTICE.py          --name ghost-lattice          --interpreter python3
pm2 start GHOST_LATTICE_resolver.py --name ghost-lattice-resolver --interpreter python3
pm2 start GHOST_LATTICE_redeemer.py --name ghost-lattice-redeemer --interpreter python3
```

---

## Files

| File | Role |
|------|------|
| `GHOST_LATTICE.py` | Main scanner — discovers markets, scores signals, fires trades |
| `GHOST_LATTICE_resolver.py` | Polls Gamma + Chainlink, marks trades won/lost in DB |
| `GHOST_LATTICE_redeemer.py` | Redeems winning CTF positions on Polygon for USDC |
| `GHOST_LATTICE_dashboard.py` | Rich terminal UI; spawns the stack when run directly |
| `GHOST_LATTICE_fill_guard.py` | Ghost-fill protection (prevents double-fills, dup orders) |
| `ghost_brain.py` | ML signal scoring layer (0–100 utility score) |
| `ghost_brain.json` | Persisted brain state (per-coin scores, learned thresholds) |
| `gas_watcher.py` | Polygon gas-price watcher (used by redeemer for fee tuning) |
| `telegram_alerts.py` | Telegram trade-alert sender (used by scanner) |
| `.env` | All credentials + tunables (you create from `.env.example`) |
| `config/requirements_crypto.txt` | Pinned core deps (subset of `requirements.txt`) |
| `config/install.bat` | Windows one-click installer |

---

## Database

- **Paper mode** (`PAPER_TRADE=true`): writes to `GHOST_LATTICE_PAPER.db`
- **Live mode** (`PAPER_TRADE=false`): writes to `GHOST_LATTICE.db`

Both are SQLite. DB is auto-created on first run. The resolver and redeemer
write to the **same** DB as the scanner — they share state via the file.

---

## Safety

- Always run `PAPER_TRADE=true` for at least a few hours before going live.
  Verify: signals fire, resolver marks them won/lost, redeemer skips
  (it only runs in live mode).
- The scanner refuses to trade if Binance or Polymarket WS is disconnected.
- Set `DAILY_LOSS_LIMIT` (or env-equivalent) as your circuit breaker.
- After first run, confirm the DB file exists in this folder.

---

*GHOST_LATTICE v3 — Share Pack 2026-05-25*
