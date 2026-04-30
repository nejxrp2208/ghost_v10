"""
MarketGhost Stats — see what's been collected
"""
import sqlite3, os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "marketghost.db")

if not os.path.exists(DB_PATH):
    print("No marketghost.db yet. Run marketghost.py first to start collecting.")
    input("\nPress Enter...")
    exit()

conn = sqlite3.connect(DB_PATH)

n_markets  = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
n_snaps    = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
n_resolved = conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]

print(f"""
╔═══════════════════════════════════════════════════════════╗
║              MARKETGHOST COLLECTION REPORT               ║
╚═══════════════════════════════════════════════════════════╝

  DATASET SIZE
  ────────────
  Markets tracked   : {n_markets}
  Snapshots taken   : {n_snaps}
  Markets resolved  : {n_resolved}
""")

if n_resolved > 0:
    # UP/DOWN distribution
    up_wins = conn.execute("SELECT COUNT(*) FROM resolutions WHERE winner='UP'").fetchone()[0]
    dn_wins = conn.execute("SELECT COUNT(*) FROM resolutions WHERE winner='DOWN'").fetchone()[0]
    print(f"""  OUTCOME DISTRIBUTION
  ─────────────────────
  UP won   : {up_wins}  ({up_wins/n_resolved*100:.1f}%)
  DOWN won : {dn_wins}  ({dn_wins/n_resolved*100:.1f}%)
""")

    # By coin
    print("  BY COIN")
    print("  ───────")
    coin_stats = conn.execute("""
        SELECT coin,
               COUNT(*) as total,
               SUM(CASE WHEN winner='UP' THEN 1 ELSE 0 END) as up_wins
        FROM resolutions
        GROUP BY coin
        ORDER BY total DESC
    """).fetchall()
    for coin, total, up in coin_stats:
        up_pct = up/total*100 if total else 0
        print(f"  {coin:<4}: {total} markets | UP {up}/{total} ({up_pct:.1f}%)")

    # Price-at-entry analysis
    print("\n  EDGE DISCOVERY (needs 50+ resolved to be meaningful)")
    print("  ──────────────────────────────────────────────────")
    # For each resolution, what was the DOWN ask in the last 5 min?
    edge = conn.execute("""
        SELECT r.winner,
               s.down_ask,
               s.up_ask,
               s.mins_to_close
        FROM resolutions r
        JOIN snapshots s ON r.market_id = s.market_id
        WHERE s.mins_to_close BETWEEN 0 AND 5
          AND s.up_ask > 0 AND s.down_ask > 0
    """).fetchall()

    if edge:
        # Bin by DOWN ask price
        buckets = {}
        for winner, down_ask, up_ask, mins in edge:
            # Round down_ask to nearest 0.05
            bucket = round(down_ask * 20) / 20
            if bucket not in buckets:
                buckets[bucket] = {'down_wins': 0, 'total': 0}
            buckets[bucket]['total'] += 1
            if winner == 'DOWN':
                buckets[bucket]['down_wins'] += 1
        print(f"  DOWN-side buy outcomes by late-window price ({len(edge)} observations)")
        print(f"  {'Price':<8} {'Trades':<10} {'DOWN won':<12} {'Win Rate':<10} {'Breakeven':<10}")
        for price in sorted(buckets.keys()):
            s = buckets[price]
            wr = s['down_wins']/s['total']*100 if s['total'] else 0
            if price > 0:
                # If you bought at $price, payout = $1. Breakeven WR = price * 100
                be = price * 100
                edge_str = f"{wr-be:+.1f}pp" if s['total'] >= 5 else "(low N)"
            else:
                edge_str = ""
            if s['total'] >= 3:
                print(f"  ${price:<7.2f} {s['total']:<10} {s['down_wins']:<12} {wr:<9.1f}% {edge_str}")
else:
    print("  No resolutions yet. Need to wait for markets to end + Binance candles to close.")

# Oldest/newest
oldest = conn.execute("SELECT MIN(ts_utc), MAX(ts_utc) FROM snapshots").fetchone()
if oldest and oldest[0]:
    print(f"\n  COLLECTION WINDOW")
    print(f"  ─────────────────")
    print(f"  First snapshot : {oldest[0]}")
    print(f"  Latest snapshot: {oldest[1]}")

conn.close()
input("\nPress Enter to close...")
