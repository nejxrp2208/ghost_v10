"""
GHOST LATTICE v10 — Redeemer (on-chain CTF redemption)
=======================================================
Automatically redeems winning Polymarket positions on-chain (LIVE mode)
or simulates redemption for analytics (PAPER mode).

Features
--------
- Dual-contract support: standard CTF + NegRiskAdapter (for neg-risk markets)
- Dynamic gas pricing with a hard cap (no more 500-gwei burn)
- Transaction receipts confirmed before DB marks the position redeemed
- Persistent dedup via DB flag (safe across restarts)
- Skips $0-value positions (no point paying gas to redeem losses)
- Paper mode: simulates redemption so dashboards show end-to-end flow

DB: GHOST_LATTICE.db | Spawned by: GHOST_LATTICE_dashboard.py
Run standalone: python GHOST_LATTICE_redeemer.py
"""

import asyncio
import aiohttp
import sqlite3
import os
import sys
import json
import time
from datetime import datetime, timezone

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    from dotenv import load_dotenv
    SD = os.path.dirname(os.path.abspath(__file__))
    for ep in [os.path.join(SD, ".env")]:
        if os.path.exists(ep):
            load_dotenv(ep, override=True)
            print(f"[ENV] Loaded: {ep}")
            break
except ImportError:
    pass

try:
    from web3 import Web3
except ImportError:
    print("[ERROR] web3 not installed. Run: pip install web3")
    print("[INFO]  PAPER mode doesn't need web3 — install only for LIVE.")
    Web3 = None

# ─── CONFIG ───────────────────────────────────────────────────────────────────
PRIVATE_KEY   = os.getenv("PRIVATE_KEY", "")
FUNDER        = os.getenv("POLYMARKET_PROXY_ADDRESS",
                  os.getenv("FUNDER_ADDRESS", ""))
PAPER_TRADE   = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true","1","yes")
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
DB_PATH       = os.path.join(SCRIPT_DIR,
                  "GHOST_LATTICE_PAPER.db" if PAPER_TRADE else "GHOST_LATTICE.db")

POLL_INTERVAL    = int(os.getenv("REDEEMER_POLL_SEC", "30"))
MIN_REDEEM_VALUE = float(os.getenv("MIN_REDEEM_VALUE", "0.10"))
GAS_CAP_GWEI     = float(os.getenv("REDEEM_GAS_CAP_GWEI", "150"))
GAS_LIMIT        = int(os.getenv("REDEEM_GAS_LIMIT", "250000"))

DATA_API         = "https://data-api.polymarket.com"
RPC_URLS = [
    "https://gateway.tenderly.co/public/polygon",   # most reliable — no in-flight limit
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
]

# Polygon mainnet contract addresses — only needed for LIVE
if Web3:
    USDC_ADDRESS           = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")   # USDC.PoS — ACTUAL collateral for all Polymarket CTF positions (confirmed by RESCUE_REDEEM.py)
    CTF_ADDRESS            = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")   # Gnosis CTF — redeemPositions
    CTF_ADDRESS_V1         = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")   # legacy V1 exchange
    # NegRiskAdapter — same address on Polygon mainnet AND Amoy (not testnet-only).
    # 0xe2222d... is the NegRisk EXCHANGE (trading only) — wrong contract for redeemPositions.
    NEG_RISK_ADAPTER_ADDR  = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")   # NegRiskAdapter — redeemPositions

CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSets",          "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "type": "function",
    },
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def check_payout_denominator(w3, cid_hex: bytes) -> int:
    """Return on-chain payoutDenominator.
    0  = oracle not resolved yet — skip tx, save gas.
    >0 = resolved — safe to redeem.
    -1 = RPC call failed — proceed anyway (don't block on infra error)."""
    try:
        contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
        return contract.functions.payoutDenominator(cid_hex).call()
    except Exception as e:
        print(f"[WARN] payoutDenominator check failed: {e}")
        return -1

NEG_RISK_ABI = [{
    "inputs": [
        {"name": "conditionId", "type": "bytes32"},
        {"name": "amounts",     "type": "uint256[]"},
    ],
    "name": "redeemPositions",
    "outputs": [],
    "type": "function",
}]

USDC_ABI = [{
    "inputs":  [{"name":"account","type":"address"}],
    "name":    "balanceOf",
    "outputs": [{"name":"","type":"uint256"}],
    "type":    "function",
}]

# ─── DB ───────────────────────────────────────────────────────────────────────
def _db_connect():
    """SQLite connection — WAL mode + 15s busy-wait, safe to share with scanner/resolver."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA wal_autocheckpoint=100")  # prevent WAL bloat
    return conn

def ensure_schema():
    """Add redemption columns to trades table if missing."""
    if not os.path.exists(DB_PATH):
        return
    conn = _db_connect()
    for ddl in [
        "ALTER TABLE trades ADD COLUMN redeemed INTEGER DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN redeemed_at TEXT",
        "ALTER TABLE trades ADD COLUMN redeem_tx_hash TEXT",
        "ALTER TABLE trades ADD COLUMN condition_id TEXT",
    ]:
        try:
            conn.execute(ddl)
        except Exception:
            pass
    conn.commit()
    conn.close()

def log_event(icon, msg):
    try:
        conn = _db_connect()
        conn.execute("INSERT INTO events (ts, icon, msg) VALUES (?,?,?)",
                     (datetime.now().strftime("%H:%M:%S"), icon, str(msg)[:120]))
        conn.execute("DELETE FROM events WHERE id NOT IN "
                     "(SELECT id FROM events ORDER BY id DESC LIMIT 100)")
        conn.commit()
        conn.close()
    except Exception:
        pass

def mark_redeemed(condition_id: str, tx_hash: str):
    """Mark position as redeemed in both trades table AND redeemed_conditions table.
    The redeemed_conditions table is the persistent dedup store — it never misses
    even when no matching trade row exists (e.g. positions redeemed from old dbs).
    Retries 3x with backoff — on-chain tx already submitted, MUST write to DB."""
    import time as _t
    now = datetime.now(timezone.utc).isoformat()
    for attempt in range(3):
        conn = None
        try:
            conn = _db_connect()
            # Update trades if a matching row exists
            conn.execute(
                "UPDATE trades SET redeemed=1, redeemed_at=?, redeem_tx_hash=?, condition_id=? "
                "WHERE (condition_id=? OR token_id LIKE ?) AND status='won' AND COALESCE(redeemed,0)=0",
                (now, tx_hash, condition_id, condition_id, f"%{condition_id[-20:]}%")
            )
            # Always write to redeemed_conditions — this is the restart-safe dedup store
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS redeemed_conditions "
                    "(condition_id TEXT PRIMARY KEY, status TEXT, attempt_count INTEGER, "
                    "last_attempt TEXT, tx_hash TEXT, cash_pnl REAL)"
                )
            except Exception:
                pass
            conn.execute(
                "INSERT OR REPLACE INTO redeemed_conditions "
                "(condition_id, status, attempt_count, last_attempt, tx_hash, cash_pnl) "
                "VALUES (?, 'redeemed', 1, ?, ?, 0)",
                (condition_id, now, tx_hash)
            )
            conn.commit()
            return  # success
        except Exception as e:
            if attempt < 2:
                _t.sleep(0.5 * (attempt + 1))
            else:
                print(f"[DB-CRITICAL] mark_redeemed failed 3x for {condition_id[:20]}: {e}")

        finally:
            if conn:
                try: conn.close()
                except Exception: pass
def get_unredeemed_condition_ids() -> set:
    """Return all condition_ids already redeemed — checks both tables so
    no position is ever double-redeemed across restarts."""
    try:
        conn = _db_connect()
        ids = set()
        # From trades table
        try:
            rows = conn.execute(
                "SELECT DISTINCT condition_id FROM trades "
                "WHERE redeemed=1 AND condition_id IS NOT NULL"
            ).fetchall()
            ids |= {r[0] for r in rows if r[0]}
        except Exception:
            pass
        # From redeemed_conditions table (restart-safe dedup)
        try:
            rows = conn.execute(
                "SELECT condition_id FROM redeemed_conditions WHERE status='redeemed'"
            ).fetchall()
            ids |= {r[0] for r in rows if r[0]}
        except Exception:
            pass
        conn.close()
        return ids
    except Exception:
        return set()

def paper_simulate_redemption():
    """
    Paper mode: mark all 'won' trades as redeemed with a fake tx hash.
    Returns (count, total_usd).
    """
    try:
        conn = _db_connect()
        cur  = conn.execute(
            "SELECT id, tier, coin, pnl FROM trades "
            "WHERE status='won' AND COALESCE(redeemed,0)=0"
        )
        rows = cur.fetchall()
        now  = datetime.now(timezone.utc).isoformat()
        count = 0
        total_usd = 0.0
        for tid, tier, coin, pnl in rows:
            conn.execute(
                "UPDATE trades SET redeemed=1, redeemed_at=?, "
                "redeem_tx_hash='PAPER_SIM' WHERE id=?", (now, tid)
            )
            count += 1
            total_usd += (pnl or 0)
            log_event("💰", f"[PAPER] Redeemed T{tier} {coin} +${pnl:+.2f}")
        conn.commit()
        conn.close()
        return count, total_usd
    except Exception as e:
        print(f"[WARN] paper_simulate_redemption: {e}")
        return 0, 0.0

# ─── WEB3 ─────────────────────────────────────────────────────────────────────
def get_web3():
    if not Web3:
        return None
    for rpc in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    return None

def get_usdc_balance(w3) -> float:
    if not FUNDER or not Web3:
        return 0.0
    try:
        usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
        bal  = usdc.functions.balanceOf(Web3.to_checksum_address(FUNDER)).call()
        return bal / 1e6
    except Exception:
        return 0.0

def suggest_gas_price(w3) -> int:
    try:
        gp = w3.eth.gas_price
        cap = int(GAS_CAP_GWEI * 1e9)
        return min(max(gp, int(30e9)), cap)
    except Exception:
        return int(50e9)

def get_clob_balance() -> float:
    """Read-only CLOB balance fetch — no update_balance_allowance call."""
    try:
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
        except ImportError:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

        api_key        = os.getenv("API_KEY",        os.getenv("CLOB_API_KEY", ""))
        api_secret     = os.getenv("API_SECRET",     os.getenv("CLOB_SECRET", ""))
        api_passphrase = os.getenv("API_PASSPHRASE", os.getenv("CLOB_PASSPHRASE", ""))
        creds  = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=PRIVATE_KEY,
            creds=creds,
            signature_type=0,
        )
        result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        raw = float(result.get("balance", 0)) if isinstance(result, dict) else 0.0
        bal = raw / 1_000_000 if raw > 10_000 else raw
        return bal
    except Exception as e:
        return 0.0


def sync_clob_balance() -> float:
    """
    Tell the Polymarket CLOB to re-scan the on-chain pUSD balance and credit
    any redeemed winnings back into the trading account.

    This is the critical step that was missing: after on-chain CTF redemption,
    pUSD lands in the EOA wallet but the CLOB doesn't know until you call
    update_balance_allowance().  Without this, wins pile up in the wallet but
    the scanner never sees the money and keeps draining.
    """
    try:
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
        except ImportError:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

        api_key        = os.getenv("API_KEY",        os.getenv("CLOB_API_KEY", ""))
        api_secret     = os.getenv("API_SECRET",     os.getenv("CLOB_SECRET", ""))
        api_passphrase = os.getenv("API_PASSPHRASE", os.getenv("CLOB_PASSPHRASE", ""))
        creds  = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=PRIVATE_KEY,
            creds=creds,
            signature_type=0,
        )
        client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        import time; time.sleep(3)
        result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        raw = float(result.get("balance", 0)) if isinstance(result, dict) else 0.0
        # CLOB returns balance in raw 6-decimal USDC units (e.g. 219903027 = $219.90)
        bal = raw / 1_000_000 if raw > 10_000 else raw
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] 💳 CLOB synced — trading balance now: ${bal:.2f}")
        return bal
    except Exception as e:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [WARN] CLOB sync failed: {e}")
        return 0.0

# ─── FETCH REDEEMABLE ─────────────────────────────────────────────────────────
async def fetch_redeemable(session, wallet: str) -> list:
    try:
        url = f"{DATA_API}/positions"
        async with session.get(url,
            params={"user": wallet, "redeemable": "true", "limit": "500"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return []
            data = await r.json()
            if not isinstance(data, list):
                return []
            return [p for p in data if p.get("redeemable") is True
                    and float(p.get("currentValue", 0) or 0) >= MIN_REDEEM_VALUE]
    except Exception as e:
        print(f"[WARN] fetch_redeemable: {e}")
        return []

# ─── REDEEM ───────────────────────────────────────────────────────────────────
def build_tx_standard_ctf(w3, contract, condition_hex: bytes, index_set: list,
                          account, nonce: int, gas_price: int):
    return contract.functions.redeemPositions(
        USDC_ADDRESS,
        b"\x00" * 32,
        condition_hex,
        index_set,
    ).build_transaction({
        "from":     account.address,
        "nonce":    nonce,
        "gas":      GAS_LIMIT,
        "gasPrice": gas_price,
    })

def build_tx_neg_risk(w3, contract, condition_hex: bytes, amounts: list,
                      account, nonce: int, gas_price: int):
    return contract.functions.redeemPositions(
        condition_hex, amounts,
    ).build_transaction({
        "from":     account.address,
        "nonce":    nonce,
        "gas":      GAS_LIMIT,
        "gasPrice": gas_price,
    })

def redeem_one(w3, account, pos: dict, nonce: int) -> tuple:
    condition_id  = pos.get("conditionId", "") or ""
    outcome_index = int(pos.get("outcomeIndex", 0) or 0)
    negative_risk = bool(pos.get("negativeRisk", False))
    size          = float(pos.get("size", 0) or 0)
    title         = (pos.get("title", "Unknown") or "Unknown")[:45]
    cash_pnl      = float(pos.get("cashPnl", 0) or 0)

    if not condition_id:
        return False, "", nonce

    try:
        cid_hex = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x")
                                else condition_id)
    except Exception as e:
        print(f"[WARN] bad conditionId {condition_id}: {e}")
        return False, "", nonce

    # ── Gate 1: oracle must have resolved this condition ──────────────────────
    payout_denom = check_payout_denominator(w3, cid_hex)
    if payout_denom == 0:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [SKIP] Oracle not resolved: {title[:42]} "
              f"(payoutDenominator=0) — will retry next poll")
        return False, "", nonce

    gas_price = suggest_gas_price(w3)

    # ── Gate 2: snapshot pUSD balance BEFORE tx ───────────────────────────────
    bal_before = get_usdc_balance(w3)

    try:
        if negative_risk:
            contract = w3.eth.contract(address=NEG_RISK_ADAPTER_ADDR, abi=NEG_RISK_ABI)
            amounts = [0, 0]
            amounts[outcome_index] = int(size * 1e6)
            tx = build_tx_neg_risk(w3, contract, cid_hex, amounts,
                                   account, nonce, gas_price)
        else:
            contract = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
            index_set = [1] if outcome_index == 0 else [2]
            tx = build_tx_standard_ctf(w3, contract, cid_hex, index_set,
                                       account, nonce, gas_price)

        signed  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        raw     = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        tx_hex  = tx_hash.hex()

        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
            if receipt.status != 1:
                print(f"[FAIL] Redeem reverted: {title} | tx: {tx_hex}")
                return False, tx_hex, nonce + 1
        except Exception as e:
            # Receipt timed out — tx may have landed or may not.
            # Do NOT mark redeemed: we cannot confirm balance change.
            print(f"[WARN] Receipt timeout for {title}: {e} — NOT marking done, will retry")
            return False, tx_hex, nonce + 1

        # ── Gate 3: verify pUSD actually landed in wallet ─────────────────────
        import time as _time; _time.sleep(4)   # brief settle window
        bal_after = get_usdc_balance(w3)
        delta     = bal_after - bal_before

        ts   = datetime.now().strftime("%H:%M:%S")
        icon = "✅" if cash_pnl >= 0 else "❌"

        if delta < 0.01:
            print(f"[{ts}] ⚠️  Tx confirmed but pUSD UNCHANGED "
                  f"(before=${bal_before:.2f}, after=${bal_after:.2f}) — "
                  f"likely CLOB auto-settled; EOA held no CTF tokens. "
                  f"NOT marking redeemed. tx: {tx_hex[:22]}...")
            log_event("⚠️", f"Zero-delta redeem {title[:35]} | tx:{tx_hex[:16]}")
            # Return False so caller does NOT add to already_done.
            # Next poll will try again and log same outcome until resolved.
            return False, tx_hex, nonce + 1

        print(f"[{ts}] 💰 {icon} Redeemed: {title} | P&L: ${cash_pnl:+.2f} "
              f"| +${delta:.2f} pUSD | gas: {gas_price/1e9:.0f}gwei | tx: {tx_hex[:20]}...")
        log_event("💰", f"{icon} Redeemed {title[:38]} | P&L: ${cash_pnl:+.2f} (+${delta:.2f})")

        mark_redeemed(condition_id, tx_hex)
        return True, tx_hex, nonce + 1

    except Exception as e:
        print(f"[WARN] Redeem failed for {condition_id[:20]}: {e}")
        return False, "", nonce

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def run():
    mode = "PAPER" if PAPER_TRADE else "LIVE"
    print(f"""
╔═══════════════════════════════════════════════════════════╗
║   CRYPTO GHOST AUTO-REDEEMER v3.0                        ║
║   Mode: {mode:<8}  |  Poll: {POLL_INTERVAL:>3}s                     ║
║   Wallet: {(FUNDER[:20] + '...') if FUNDER else 'NOT SET':<26}             ║
║   DB:    {os.path.basename(DB_PATH):<30}           ║
╚═══════════════════════════════════════════════════════════╝
    """)

    ensure_schema()

    # ── PAPER MODE ────────────────────────────────────────────────────────────
    if PAPER_TRADE:
        print("[Redeemer] PAPER mode — simulating redemption for 'won' trades.")
        print(f"[Redeemer] Polls every {POLL_INTERVAL}s. Ctrl+C to stop.\n")
        total_sim = 0
        while True:
            try:
                n, usd = paper_simulate_redemption()
                if n:
                    total_sim += n
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}] 💰 [PAPER] Simulated {n} redemption(s) "
                          f"| +${usd:+.2f} | Session total: {total_sim}")
            except Exception as e:
                print(f"[WARN] paper loop: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    # ── LIVE MODE ─────────────────────────────────────────────────────────────
    if not Web3:
        print("[ERROR] web3 library required for LIVE mode. Run: pip install web3")
        return
    if not PRIVATE_KEY:
        print("[ERROR] No PRIVATE_KEY in .env — cannot sign redemptions.")
        return
    if not FUNDER:
        print("[ERROR] No POLYMARKET_PROXY_ADDRESS in .env — cannot query positions.")
        return

    w3 = get_web3()
    if not w3:
        print("[ERROR] Could not connect to any Polygon RPC. Retrying in loop...")
    else:
        clob_bal = get_clob_balance()
        print(f"[Redeemer] Connected to Polygon | CLOB trading balance: ${clob_bal:.2f}")

    account = w3.eth.account.from_key(PRIVATE_KEY) if w3 else None
    if account:
        print(f"[Redeemer] Signer: {account.address}")

    already_done = get_unredeemed_condition_ids()
    print(f"[Redeemer] {len(already_done)} previously-redeemed positions loaded from DB.\n")

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                if not w3 or not w3.is_connected():
                    w3 = get_web3()
                    if w3:
                        account = w3.eth.account.from_key(PRIVATE_KEY)
                        print(f"[Redeemer] RPC reconnected.")
                if not w3:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                positions = await fetch_redeemable(session, FUNDER)
                new_positions = [p for p in positions
                                 if p.get("conditionId") not in already_done]

                ts = datetime.now().strftime("%H:%M:%S")
                if new_positions:
                    total_val = sum(float(p.get("currentValue", 0) or 0)
                                    for p in new_positions)
                    print(f"[{ts}] 💰 Found {len(new_positions)} redeemable "
                          f"| Total value: ${total_val:.2f}")

                    nonce = w3.eth.get_transaction_count(account.address, "pending")
                    success = 0
                    for pos in new_positions:
                        ok, tx_hex, nonce = redeem_one(w3, account, pos, nonce)
                        if ok:
                            cid = pos.get("conditionId")
                            already_done.add(cid)
                            mark_redeemed(cid, tx_hex)
                            success += 1
                        await asyncio.sleep(0.5)

                    if success > 0:
                        bal_onchain = get_usdc_balance(w3)
                        print(f"[{ts}] Redeemed {success}/{len(new_positions)} "
                              f"| On-chain pUSD: ${bal_onchain:.2f}")
                        sync_clob_balance()
                else:
                    print(f"[{ts}] No redeemable positions "
                          f"| Next check in {POLL_INTERVAL}s")

                # Always sync CLOB balance every cycle — ensures scanner sees
                # CLOB-settled wins even when no on-chain CTF redemptions occur.
                sync_clob_balance()

                await asyncio.sleep(POLL_INTERVAL)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[Redeemer] Error in main loop: {e}")
                await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[STOPPED] Redeemer stopped.")
