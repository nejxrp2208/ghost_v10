"""
audit_chainlink.py — query Chainlink price feeds DIRECTLY on Polygon to
verify every closed trade against the EXACT data Polymarket's UMA oracle
uses for resolution.

This is the ground truth. No exchange-API approximations.

How it works:
  1. Connect to a public Polygon RPC.
  2. Load the Chainlink AggregatorV3 contract for each coin.
  3. For each trade, find the Chainlink round whose `updatedAt` is the
     latest one <= start_utc, and same for end_utc. Those are the prices
     that were "active" at PM's settlement timestamps.
  4. Apply PM's documented rule:
        p_end >= p_start → UP wins
        p_end <  p_start → DOWN wins
  5. Compare to bot's recorded status, flag any disagreements.

Setup:
    pip install web3

Usage:
    python audit_chainlink.py             # all closed trades
    python audit_chainlink.py --wins-only # only audit rows marked 'won'

Read-only. Reports only — does NOT touch the DB.
"""

import sqlite3
import os
import sys
import re
import time
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:
    _ET_TZ = None

try:
    from web3 import Web3
except ImportError:
    print("ERROR: web3 not installed. Run:  pip install web3")
    sys.exit(1)

PAPER = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true","1","yes")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "crypto_ghost_PAPER.db" if PAPER else "crypto_ghost.db")
WINS_ONLY = "--wins-only" in sys.argv

# Same RPC list the redeemer uses
RPC_URLS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
]

# Chainlink price feed addresses on Polygon mainnet (chain id 137)
# Source: https://docs.chain.link/data-feeds/price-feeds/addresses?network=polygon
CHAINLINK_FEEDS = {
    "BTC": "0xc907E116054Ad103354f2D350FD2514433D57F6f",  # BTC/USD
    "ETH": "0xF9680D99D6C9589e2a93a78A04A279e509205945",  # ETH/USD
    "SOL": "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC",  # SOL/USD
    "XRP": "0x785ba89291f676b5386652eB12b30cF361020694",  # XRP/USD
}

AGG_V3_ABI = [
    {"inputs": [], "name": "decimals",
     "outputs": [{"name":"","type":"uint8"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "latestRoundData",
     "outputs": [{"name":"roundId","type":"uint80"},
                 {"name":"answer","type":"int256"},
                 {"name":"startedAt","type":"uint256"},
                 {"name":"updatedAt","type":"uint256"},
                 {"name":"answeredInRound","type":"uint80"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name":"_roundId","type":"uint80"}], "name": "getRoundData",
     "outputs": [{"name":"roundId","type":"uint80"},
                 {"name":"answer","type":"int256"},
                 {"name":"startedAt","type":"uint256"},
                 {"name":"updatedAt","type":"uint256"},
                 {"name":"answeredInRound","type":"uint80"}],
     "stateMutability": "view", "type": "function"},
]


def connect_polygon():
    for rpc in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                print(f"[RPC] Connected: {rpc}")
                return w3
        except Exception:
            continue
    return None


def parse_market_times(question):
    m = re.search(r'(\w+)\s+(\d+),\s+(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)',
                  question or "", re.IGNORECASE)
    if not m: return None, None
    month_name, day, h1, m1, ap1, h2, m2, ap2 = m.groups()
    months = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
              "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
    mo = months.get(month_name.lower())
    if not mo: return None, None
    def to24(h_str, ap):
        h = int(h_str)
        if ap.upper() == "PM" and h != 12: h += 12
        if ap.upper() == "AM" and h == 12: h = 0
        return h
    year = datetime.now(timezone.utc).year
    try:
        if _ET_TZ is not None:
            start_et = datetime(year, mo, int(day), to24(h1, ap1), int(m1), tzinfo=_ET_TZ)
            end_et = datetime(year, mo, int(day), to24(h2, ap2), int(m2), tzinfo=_ET_TZ)
            start_utc = start_et.astimezone(timezone.utc)
            end_utc = end_et.astimezone(timezone.utc)
        else:
            start_utc = datetime(year, mo, int(day), to24(h1, ap1), int(m1),
                                 tzinfo=timezone.utc) + timedelta(hours=4)
            end_utc = datetime(year, mo, int(day), to24(h2, ap2), int(m2),
                               tzinfo=timezone.utc) + timedelta(hours=4)
        if end_utc < start_utc:
            end_utc += timedelta(days=1)
        return start_utc, end_utc
    except Exception:
        return None, None


class ChainlinkFeed:
    """
    Looks up the Chainlink price as-of any past timestamp.

    Strategy:
      - latestRoundData() gives current round + updatedAt
      - For a target timestamp in the past, estimate how many rounds back
        we need (Polygon BTC/USD heartbeat is ~27s, so ~30s/round avg).
      - Land near the estimate, then walk in the right direction by 1
        round at a time until we find the latest round whose updatedAt
        is <= target.
      - Cache per-timestamp so multiple trades sharing a timestamp cost 1.
    """
    AVG_SECONDS_PER_ROUND = 30

    def __init__(self, w3, address):
        self.w3 = w3
        self.contract = w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=AGG_V3_ABI
        )
        self.decimals = self.contract.functions.decimals().call()
        self.scale = 10 ** self.decimals
        self._refresh_latest()
        self.cache = {}

    def _refresh_latest(self):
        rd = self.contract.functions.latestRoundData().call()
        self._latest_rid = rd[0]
        self._latest_updated = rd[3]
        self._latest_answer = rd[1]

    def _get_round(self, rid):
        try:
            rd = self.contract.functions.getRoundData(rid).call()
            if rd[3] == 0:
                return None
            return rd
        except Exception:
            return None

    def price_at(self, target_unix):
        if target_unix in self.cache:
            return self.cache[target_unix]

        # Target at or after latest -> use latest
        if target_unix >= self._latest_updated:
            price = self._latest_answer / self.scale
            self.cache[target_unix] = price
            return price

        # Estimate rounds back
        seconds_back = self._latest_updated - target_unix
        rounds_back = max(1, seconds_back // self.AVG_SECONDS_PER_ROUND)
        candidate_rid = self._latest_rid - rounds_back

        rd = self._get_round(candidate_rid)
        if rd is None:
            # Estimate landed in a phase gap; do a slow walk from latest.
            return self._slow_walk(target_unix)

        rid, answer, _, updated_at, _ = rd

        # Walk to refine: find the LATEST round with updated_at <= target
        if updated_at > target_unix:
            steps = 0
            while updated_at > target_unix and steps < 5000:
                candidate_rid -= 1
                rd = self._get_round(candidate_rid)
                if rd is None:
                    return None
                _, answer, _, updated_at, _ = rd
                steps += 1
            if steps >= 5000:
                return None
        else:
            # updated_at <= target — see if a later round also fits
            while True:
                next_rid = candidate_rid + 1
                if next_rid > self._latest_rid:
                    break
                rd = self._get_round(next_rid)
                if rd is None:
                    break
                _, next_answer, _, next_updated, _ = rd
                if next_updated > target_unix:
                    break
                candidate_rid = next_rid
                answer = next_answer
                updated_at = next_updated

        price = answer / self.scale
        self.cache[target_unix] = price
        return price

    def _slow_walk(self, target_unix):
        rid = self._latest_rid
        answer = self._latest_answer
        updated_at = self._latest_updated
        steps = 0
        while updated_at > target_unix and steps < 10000:
            rid -= 1
            rd = self._get_round(rid)
            if rd is None:
                return None
            _, answer, _, updated_at, _ = rd
            steps += 1
        return answer / self.scale if steps < 10000 else None


def pm_rule_status(outcome, p_start, p_end):
    if p_start is None or p_end is None:
        return None
    bought_up = (outcome or "").upper() in ("UP","YES","HIGHER","ABOVE")
    market_resolves_up = p_end >= p_start
    if bought_up:
        return "won" if market_resolves_up else "lost"
    return "lost" if market_resolves_up else "won"


def main():
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        return

    print("Connecting to Polygon...")
    w3 = connect_polygon()
    if w3 is None:
        print("ERROR: Could not connect to any Polygon RPC.")
        return

    print("Loading Chainlink price feeds...")
    feeds = {}
    for coin, addr in CHAINLINK_FEEDS.items():
        try:
            feeds[coin] = ChainlinkFeed(w3, addr)
            print(f"  {coin}/USD  {addr}  ({feeds[coin].decimals} decimals)")
        except Exception as e:
            print(f"  {coin}: failed ({e})")

    conn = sqlite3.connect(DB_PATH)
    where = "status = 'won'" if WINS_ONLY else "status IN ('won','lost')"
    rows = conn.execute(f"""
        SELECT id, coin, outcome, status,
               size_usdc, entry_price, pnl,
               price_start, price_end, question
        FROM trades WHERE {where}
        ORDER BY id ASC
    """).fetchall()
    conn.close()

    print(f"\nChainlink-direct audit (= Polymarket's actual UMA source)")
    print(f"Trades: {len(rows)}  Mode: {'WINS ONLY' if WINS_ONLY else 'ALL CLOSED'}\n")
    print(f"{'ID':>3} {'Coin':4} {'Side':4} {'Bot':6} {'CL':6} "
          f"{'CL Δ%':>9} {'CL_start':>11} {'CL_end':>11} "
          f"{'BotPnL':>9} {'CL PnL':>9} {'Δ':>9}  note")
    print("-" * 130)

    real_wins = real_losses = fake_wins = missed_wins = 0
    unknown = errored = 0
    bot_total = cl_total = 0.0
    fakes = []
    misses = []

    for tid, coin, side, status, size, entry, pnl, ps_db, pe_db, q in rows:
        bot_total += (pnl or 0)
        feed = feeds.get((coin or "").upper())
        if feed is None:
            errored += 1
            cl_total += (pnl or 0)
            print(f"{tid:>3} {coin:4} {side:4} {status:6} {'?':6}   no feed for {coin}")
            continue
        start_utc, end_utc = parse_market_times(q or "")
        if not start_utc or not end_utc:
            unknown += 1
            cl_total += (pnl or 0)
            print(f"{tid:>3} {coin:4} {side:4} {status:6} {'?':6}   can't parse question")
            continue

        try:
            ps_cl = feed.price_at(int(start_utc.timestamp()))
            pe_cl = feed.price_at(int(end_utc.timestamp()))
        except Exception as e:
            ps_cl = pe_cl = None

        if ps_cl is None or pe_cl is None:
            errored += 1
            cl_total += (pnl or 0)
            print(f"{tid:>3} {coin:4} {side:4} {status:6} {'ERR':6}   Chainlink lookup failed")
            continue

        cl_status = pm_rule_status(side, ps_cl, pe_cl)
        cl_pct = ((pe_cl - ps_cl) / ps_cl * 100) if ps_cl else 0

        if cl_status == "won":
            cl_pnl = round((size / entry) - size, 2) if entry > 0 else 0
        else:
            cl_pnl = -float(size)
        delta = cl_pnl - (pnl or 0)
        cl_total += cl_pnl

        if cl_status == status:
            if status == "won": real_wins += 1
            else: real_losses += 1
            note = "ok"
        else:
            if status == "won":
                fake_wins += 1
                fakes.append((tid, coin, side, pnl or 0, cl_pnl, ps_cl, pe_cl, q))
                note = "*** FAKE WIN per Chainlink"
            else:
                missed_wins += 1
                misses.append((tid, coin, side, pnl or 0, cl_pnl, ps_cl, pe_cl, q))
                note = "*** missed win per Chainlink"

        print(f"{tid:>3} {coin:4} {side:4} {status:6} {cl_status:6} "
              f"{cl_pct:>+9.4f} {ps_cl:>11.2f} {pe_cl:>11.2f} "
              f"{pnl or 0:>+9.2f} {cl_pnl:>+9.2f} {delta:>+9.2f}  {note}")

    print("\n" + "=" * 70)
    print(f"Real wins:                    {real_wins}")
    print(f"Real losses:                  {real_losses}")
    print(f"FAKE wins (need flip):        {fake_wins}")
    print(f"Missed wins (bot said lost):  {missed_wins}")
    print(f"Unknown (no parse):           {unknown}")
    print(f"Errored:                      {errored}")
    print()
    print(f"Bot's recorded total:         ${bot_total:+,.2f}")
    print(f"Chainlink-truth total:        ${cl_total:+,.2f}")
    print(f"Difference:                   ${cl_total - bot_total:+,.2f}")

    if fakes:
        print("\n=== FAKE WINS per Chainlink ===")
        for tid, coin, side, bp, rp, ps, pe, q in fakes:
            print(f"  #{tid} {coin} {side}  bot=won  CL=lost  "
                  f"bot_pnl=${bp:+.2f}  cl_pnl=${rp:+.2f}  CL: {ps:.2f} -> {pe:.2f}")
            if q: print(f"        {q[:90]}")

    if misses:
        print("\n=== MISSED WINS per Chainlink ===")
        for tid, coin, side, bp, rp, ps, pe, q in misses:
            print(f"  #{tid} {coin} {side}  bot=lost  CL=won  "
                  f"bot_pnl=${bp:+.2f}  cl_pnl=${rp:+.2f}  CL: {ps:.2f} -> {pe:.2f}")
            if q: print(f"        {q[:90]}")

    print("\nThis was read-only. Chainlink is what Polymarket's UMA oracle uses,")
    print("so this audit reflects PM's actual settlement.")


if __name__ == "__main__":
    main()
