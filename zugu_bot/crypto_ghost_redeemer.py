"""
Crypto Ghost Auto-Redeemer v3.0
================================
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

Run
---
    python crypto_ghost_redeemer.py
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
                  "crypto_ghost_PAPER.db" if PAPER_TRADE else "crypto_ghost.db")

POLL_INTERVAL    = int(os.getenv("REDEEMER_POLL_SEC", "30"))
MIN_REDEEM_VALUE = float(os.getenv("MIN_REDEEM_VALUE", "0.10"))
GAS_CAP_GWEI     = float(os.getenv("REDEEM_GAS_CAP_GWEI", "150"))
GAS_LIMIT        = int(os.getenv("REDEEM_GAS_LIMIT", "250000"))

DATA_API         = "https://data-api.polymarket.com"
RPC_URLS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
]

# Polygon mainnet contract addresses — only needed for LIVE
if Web3:
    USDC_ADDRESS           = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
    CTF_ADDRESS            = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
    NEG_RISK_ADAPTER_ADDR  = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

CTF_ABI = [{
    "inputs": [
        {"name": "collateralToken",    "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId",        "type": "bytes32"},
        {"name": "indexSets",          "type": "uint256[]"},
    ],
    "name": "redeemPositions",
    "outputs": [],
    "type": "function",
}]

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
    """SQLite connection with 10s busy-wait, safe to share with scanner/resolver."""
    return sqlite3.connect(DB_PATH, timeout=10)

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
    """Mark all trades matching this conditionId as redeemed."""
    try:
        conn = _db_connect()
        conn.execute(
            "UPDATE trades SET redeemed=1, redeemed_at=?, redeem_tx_hash=?, condition_id=? "
            "WHERE (condition_id=? OR token_id LIKE ?) AND status='won' AND COALESCE(redeemed,0)=0",
            (datetime.now(timezone.utc).isoformat(), tx_hash, condition_id,
             condition_id, f"%{condition_id[-20:]}%")
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[WARN] mark_redeemed: {e}")

def get_unredeemed_condition_ids() -> set:
    try:
        conn = _db_connect()
        rows = conn.execute(
            "SELECT DISTINCT condition_id FROM trades "
            "WHERE redeemed=1 AND condition_id IS NOT NULL"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows if r[0]}
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

# ─── FETCH REDEEMABLE ─────────────────────────────────────────────────────────
async def fetch_redeemable(session, wallet: str) -> list:
    try:
        url = f"{DATA_API}/positions"
        async with session.get(url,
            params={"user": wallet, "sizeThreshold": "0"},
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

    gas_price = suggest_gas_price(w3)

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
            print(f"[WARN] Receipt timeout for {title}: {e}")
            return True, tx_hex, nonce + 1

        ts = datetime.now().strftime("%H:%M:%S")
        icon = "✅" if cash_pnl >= 0 else "❌"
        print(f"[{ts}] 💰 Redeemed: {title} | P&L: ${cash_pnl:+.2f} | "
              f"gas: {gas_price/1e9:.0f}gwei | tx: {tx_hex[:20]}...")
        log_event("💰", f"{icon} Redeemed {title[:38]} | P&L: ${cash_pnl:+.2f}")

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
        bal = get_usdc_balance(w3)
        print(f"[Redeemer] Connected to Polygon | USDC balance: ${bal:.2f}")

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
                            already_done.add(pos.get("conditionId"))
                            success += 1
                        time.sleep(0.5)

                    if success > 0:
                        bal = get_usdc_balance(w3)
                        print(f"[{ts}] ✅ Redeemed {success}/{len(new_positions)} "
                              f"| New balance: ${bal:.2f}\n")
                else:
                    print(f"[{ts}] No redeemable positions "
                          f"| Next check in {POLL_INTERVAL}s")

            except Exception as e:
                print(f"[WARN] Redeemer loop error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[STOPPED] Redeemer exited.")
