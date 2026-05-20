# GHOST LATTICE v10 — Setup Guide
## SPECTER + WRAITH | Polymarket Crypto Oracle-Lag Bot

---

## What This Is

GHOST LATTICE is an automated trading bot for Polymarket's 5-minute and hourly
BTC/ETH/SOL/BNB/XRP Up-Down markets. It exploits the ~5-minute lag between
Binance real-time prices and Chainlink's on-chain oracle updates.

- **T1 WRAITH** — Contrarian: fires AGAINST Binance momentum on 15-min/hourly markets
- **T2 SPECTER** — Oracle-lag: fires WITH Binance momentum on 5-min markets before oracle catches up
- Flat $6.66 sizing per trade. No compounding, no dynamic sizing by default.
- Lotto wins: $30–$120+ on 4–9¢ entries when oracle is maximally stale.

---

## Requirements

- Windows 10/11 (bot uses Windows-native asyncio)
- Python 3.12+ (3.14 recommended) — https://python.org/downloads
- A Polymarket account funded with USDC on Polygon
- A Polygon wallet private key (MetaMask or similar)

---

## Step 1 — Install Dependencies

Run `config\install.bat` as administrator, or manually:

```
pip install aiohttp>=3.9.0 py-clob-client>=0.16.0 web3>=6.0.0 python-dotenv>=1.0.0 websockets>=12.0
```

---

## Step 2 — Get Your Polymarket Credentials

1. Go to https://polymarket.com and connect your wallet
2. Go to **Settings → API Keys** → create a new API key
3. You will receive: API Key, Secret, Passphrase — paste all three into `.env`
4. Your **Proxy Address** is shown on the same API settings page
5. Your **Private Key** is your wallet's private key (MetaMask → Account Details → Export)

> ⚠️ Never share your private key with anyone. The bot only uses it to sign
> on-chain redemption transactions.

---

## Step 3 — Configure `.env`

Open `.env` in any text editor and fill in the CREDENTIALS section:

```
PRIVATE_KEY=0x<your_wallet_private_key>
POLYMARKET_PROXY_ADDRESS=0x<your_proxy_address>
CLOB_API_KEY=<your_api_key>
CLOB_SECRET=<your_secret>
CLOB_PASSPHRASE=<your_passphrase>
```

**Telegram alerts (optional):**
Create a Telegram bot via @BotFather and fill in:
```
TELEGRAM_BOT_TOKEN=<your_bot_token>
TELEGRAM_CHAT_ID=<your_chat_id>
```
Leave as `YOUR_..._HERE` to disable alerts.

**Key settings to review in `.env`:**
- `PAPER_TRADE=true` — default. Change to `false` for live money.
- `DAILY_LOSS_LIMIT=350` — bot stops trading if daily losses hit this amount.
- `T1_SIZE_USDC=6.66` / `T2_SIZE_USDC=6.66` — flat bet size per trade.
- `BULL_REGIME=true` — never blocks UP signals (leave true in bull markets).

---

## Step 4 — Run

**Test first (paper mode):**
```
start_PAPER.bat
```
No real money. Simulates trades against the live orderbook.

**Go live:**
```
start_LIVE.bat
```
Launches the full dashboard — scanner, resolver, and redeemer in separate windows.

---

## Files

| File | Role |
|------|------|
| `GHOST_LATTICE.py` | Main scanner — finds and fires trades |
| `GHOST_LATTICE_resolver.py` | Checks Chainlink oracle, marks trades won/lost |
| `GHOST_LATTICE_redeemer.py` | Redeems winning CTF positions on-chain |
| `GHOST_LATTICE_dashboard.py` | Terminal UI — spawns all three above |
| `GHOST_LATTICE_fill_guard.py` | Ghost fill protection (prevents double-fills) |
| `ghost_brain.py` | ML signal scoring layer |
| `.env` | All configuration — edit this |
| `start_LIVE.bat` | Launch live trading |
| `start_PAPER.bat` | Launch paper/simulation mode |

---

## Safety

- Start with `PAPER_TRADE=true` for at least a few hours to confirm the bot
  is seeing markets and signals before switching to live.
- The bot will NOT fire any trades if Binance WebSocket or Polymarket
  WebSocket fail to connect — it waits for both.
- `DAILY_LOSS_LIMIT` is your circuit breaker. Set it to an amount you are
  comfortable losing in a single day.
- After first launch, confirm the DB file `GHOST_LATTICE.db` is created in
  the same folder. This is the live trade database.

---

*GHOST LATTICE v10 | Build 2026-05-19*
