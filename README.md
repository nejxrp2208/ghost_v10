# Crypto Ghost Scanner v2.0
## Polymarket 4-Tier Crypto Sniper

---

## STRATEGY OVERVIEW

Based on the April 3-8 BTC rally analysis ($90k+ in wins):

| Tier | Market Type | Entry | Payout | Signal |
|------|------------|-------|--------|--------|
| T1 | "Will BTC hit $X?" strike markets | $0.01-0.03 | ~$495 | Binance spot ≥ strike |
| T2 | "BTC Up or Down [Hour]" | $0.03-0.10 | ~$95 | Final 20 min + 0.3% deviation |
| T3 | "BTC Up or Down [4hr]" | $0.05-0.15 | ~$45 | Final 60 min + 0.5% deviation |
| T4 | "BTC Up or Down [Day]" | $0.10-0.20 | ~$25 | Final 4 hrs + 0.4% deviation |

---

## SETUP

### Step 1: Place files
Copy the entire `CryptoGhostScanner` folder to:
```
C:\Users\ramos\OneDrive\Desktop\CryptoGhostScanner\
```

### Step 2: Install dependencies
Double-click `install.bat` OR run:
```
pip install aiohttp py-clob-client web3 python-dotenv websockets
```

### Step 3: .env file
The scanner automatically finds your existing UnifiedBot .env file.
No changes needed — it uses the same PRIVATE_KEY and POLYMARKET_PROXY_ADDRESS.

Optionally add these to your .env to customize sizing:
```
T1_SIZE_USDC=5.0
T2_SIZE_USDC=3.0
T3_SIZE_USDC=3.0
T4_SIZE_USDC=2.0
```

### Step 4: Start
Double-click `start_crypto_scanner.bat`
This opens 3 windows: Scanner, Resolver, Dashboard.

---

## FILE STRUCTURE

```
CryptoGhostScanner/
├── crypto_ghost_scanner.py   ← Main engine (run this)
├── crypto_ghost_resolver.py  ← Position monitor + PnL tracker
├── crypto_ghost_dashboard.py ← Live terminal dashboard
├── start_crypto_scanner.bat  ← Launch all 3 windows
├── install.bat               ← Install dependencies
├── requirements_crypto.txt   ← Python packages
└── crypto_ghost.db           ← SQLite DB (auto-created)
```

---

## HOW T1 (STRIKE CROSSING) WORKS

1. At startup, scanner pre-loads all "Will BTC hit $X?" markets from Polymarket
2. Binance WebSocket streams real-time BTC/ETH/SOL/XRP prices
3. When BTC spot price crosses a strike (e.g. $67k), the YES token is
   worth $1.00 but stale asks may still sit at $0.01
4. Scanner fires immediately → $5 buys ~500 shares → payout ~$495
5. Once a strike is fired, it's marked and won't fire again

---

## TUNING

Edit top of `crypto_ghost_scanner.py`:

```python
T1_MAX_ENTRY = 0.03   # Raise to 0.05 to catch more (lower payout)
MIN_NO_PRICE = 0.90   # Lower to 0.85 for more T2/T3/T4 trades
T2_WINDOW_MIN = 20    # Raise to 30 if missing signals
MAX_DAILY_LOSS = 50.0 # Kill switch
```

---

## DATABASE QUERIES

```sql
-- Today's P&L
SELECT SUM(pnl) FROM trades WHERE ts LIKE '2024-%' AND status IN ('won','lost');

-- By tier
SELECT tier, COUNT(*), SUM(pnl) FROM trades GROUP BY tier;

-- Win rate
SELECT 
  SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate
FROM trades WHERE status IN ('won','lost');
```

---

## NOTES

- This bot is SEPARATE from UnifiedBot — different DB, different strategy
- Both can run at the same time (they use different databases)
- The DB file is `crypto_ghost.db` in the CryptoGhostScanner folder
- T1 strikes only fire ONCE per strike level (no duplicates)
- The Binance WebSocket auto-reconnects if connection drops
