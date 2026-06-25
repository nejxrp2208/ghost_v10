# Fills data schema — `data/<person>/fills.csv`

One row per resolved trade. **Analysis columns only — no token_ids, wallets, or keys.**

| column     | type   | meaning |
|------------|--------|---------|
| ts         | ISO-8601 UTC | fire timestamp |
| coin       | str    | BTC \| ETH |
| side       | str    | Up \| Down |
| price      | float  | entry/fill price (prob paid, 0-1) |
| stake      | float  | size_usdc staked |
| status     | str    | won \| lost |
| pnl        | float  | realized P&L in $. **GROSS** — equals shares×1−stake on a win; taker fee NOT subtracted |
| slug       | str    | market slug (`coin-updown-dur-startts`); duration via `-5m-`/`-15m-` |
| move_bps   | float  | signed leader move at fire (bot filters on abs()) |
| secs_left  | int    | seconds to close at fire |

## Derived (compute, don't store)
- `utc_hour = int(ts[11:13])`
- `weekday  = datetime.fromisoformat(ts).weekday()`  (0=Mon)
- `duration = "5m" if "-5m-" in slug else "15m"`
- flat-$10 normalized net (strips sizing): `pnl/stake*10 - 0.072*(1-price)*10`

## How to export your own (run on your VPS, then commit the CSV)
```python
import sqlite3, csv
c = sqlite3.connect("ghost_predator.db").cursor()
cols = ['ts','coin','outcome','entry_price','size_usdc','status','pnl','slug','move_bps','secs_left']
rows = c.execute(f"SELECT {','.join(cols)} FROM positions WHERE status IN ('won','lost') ORDER BY ts")
with open("fills.csv","w",newline="") as f:
    w = csv.writer(f); w.writerow(['ts','coin','side','price','stake','status','pnl','slug','move_bps','secs_left'])
    w.writerows(rows)
```
