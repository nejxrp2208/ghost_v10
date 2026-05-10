# GhostScanner v10
## Polymarket 4-Tier Crypto Sniper Bot

---

## Strategy Overview

Targets cheap longshot tokens on Polymarket crypto Up/Down markets. Fires only in the final minutes when Binance has already moved but Polymarket hasn't repriced yet.

| Tier | Market | Entry ceiling | Window | Signal |
|------|--------|--------------|--------|--------|
| T1 | "Will BTC hit $X?" strike markets | $0.03 | Any | Binance spot ≥ strike |
| T2 | BTC/ETH/BNB Up or Down (5-min) | $0.03 | Last 3 min | Binance deviation + stale ask |
| T3 | Up or Down (1-hr) | $0.15 | Last 60 min | 0.5% deviation |
| T4 | Up or Down (4-hr) | $0.20 | Last 4 hrs | 0.4% deviation |

**Key filters (v111 overlay):**
- `MIN_NO_PRICE=0.97` — other side must be ≥97% certain before entering
- `SKIP_DOWN_BIASED_COINS=BNB,ETH` — skip cheap-DOWN on high UP base-rate coins
- `T2_MAX_SECS_LEFT=60` / `T2_MIN_SECS_LEFT=5` — stale-ask snipe window

---

## Architecture

```
ghost_v10/
├── crypto_ghost_scanner.py    ← Main engine: finds + enters trades
├── crypto_ghost_resolver.py   ← Resolves trades (Chainlink → Polymarket)
├── crypto_ghost_redeemer.py   ← Claims winnings on-chain (Polygon)
├── crypto_ghost_dashboard.py  ← Live terminal dashboard
├── marketghost.py             ← Silent data collector (no trades)
├── marketghost_stats.py       ← Stats/analysis on collected data
├── .env                       ← Config (keys, sizing, filters)
├── crypto_ghost_PAPER.db      ← Paper trades DB
└── scanner.db                 ← Real-time order book snapshots
```

---

## VPS Deployment

The bot runs 24/7 on a VPS managed via PM2.

**Server:** `root@70.34.204.152` — Ubuntu 24.04, Stockholm  
**Repo:** `/root/ghost_v10`

### Deploy changes

```bash
# 1. Local — push to GitHub
git add <files> && git commit -m "..." && git push origin master

# 2. VPS — pull + restart
cd /root/ghost_v10 && git pull && pm2 restart <process>
```

### PM2 processes

```bash
pm2 ls                        # show all processes
pm2 logs marketghost          # live logs
pm2 restart marketghost       # restart data collector
pm2 restart scanner           # restart scanner
pm2 restart resolver          # restart resolver
pm2 restart redeemer          # restart redeemer
pm2 restart ghostscanner-rt   # restart real-time scanner
pm2 restart all               # restart everything
pm2 save                      # save state (persists across reboots)
```

---

## Local Setup

### 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate        # Linux/Mac
venv\Scripts\activate           # Windows
pip install aiohttp python-dotenv web3 websockets
```

### 2. Configure .env

```env
PAPER_TRADE=true               # Keep true until 100+ trades validated
PORTFOLIO_SIZE=200.00

# Wallet (Polymarket)
PRIVATE_KEY=
POLYMARKET_PROXY_ADDRESS=
FUNDER_ADDRESS=

# Polymarket API
API_KEY=
API_SECRET=
API_PASSPHRASE=

# Trade sizing
T1_SIZE_USDC=5.0
T2_SIZE_USDC=3.0
T2_SIZE_BTC=5.0
T2_SIZE_ETH=3.0
T2_SIZE_BNB=3.0

# Telegram (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### 3. Run locally

```bash
python crypto_ghost_scanner.py   # scanner
python crypto_ghost_resolver.py  # resolver (separate terminal)
python marketghost.py            # data collector (separate terminal)
```

---

## Resolution Chain

Trades resolve via (in order of priority):
1. **Chainlink AggregatorV3 on Polygon** — same feed PM's UMA oracle uses
2. **Coinbase 1m candles** — close approximation
3. **Binance USDT klines** — last resort

Settlement rule (per Polymarket docs):
```
p_end >= p_start  →  UP wins  (flat = UP, ties go to UP)
p_end <  p_start  →  DOWN wins (strict drop required)
```

---

## MarketGhost Data Collector

`marketghost.py` runs silently alongside the bot — no trading, read-only.

**Adaptive sampling** (focuses on final 10 minutes):
```
5-10 min left  → snapshot every 60s
2-5  min left  → every 20s
1-2  min left  → every 5s
< 1  min left  → every 2s  (+1s if extreme compression < $0.01)
```

**Collected per snapshot:** bid/ask (CLOB), Binance price, trend_dev_1h, trend_dev_1m, cheap_side, ask_velocity, required_move_pct, compression zone.

```bash
python marketghost_stats.py    # print analysis
```

---

## Useful DB Queries

```sql
-- Today's P&L
SELECT SUM(pnl) FROM trades
WHERE date(closed_at) = date('now') AND status IN ('won','lost');

-- Win rate by tier
SELECT tier,
       COUNT(*) as trades,
       ROUND(SUM(CASE WHEN status='won' THEN 1.0 ELSE 0 END)/COUNT(*)*100,1) as wr_pct,
       ROUND(SUM(pnl),2) as total_pnl
FROM trades WHERE status IN ('won','lost')
GROUP BY tier;

-- Compression depth distribution (marketghost.db)
SELECT compression_zone_depth, COUNT(*), ROUND(AVG(min_cheap_ask_ever),4)
FROM markets_compression_summary
GROUP BY compression_zone_depth;

-- MarketGhost resolution stats
SELECT winner, COUNT(*) FROM resolutions GROUP BY winner;
```

---

## Notes

- **Paper mode** (`PAPER_TRADE=true`) is mandatory until 100+ trades are validated — never disable without explicit decision
- Scanner and MarketGhost use separate databases — they don't interfere
- Resolver runs Chainlink queries against Polygon RPC — needs internet access to `polygon-rpc.com`
- The redeemer only runs in live mode — no on-chain transactions in paper mode
