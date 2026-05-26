"""
gas_watcher.py — Auto-execute balance migration when Polygon gas drops
=======================================================================
Polymarket V2 migration: $467.78 pUSD is in the old GnosisSafe proxy.
This script watches Polygon gas price and fires the migration the moment
gas drops to a target threshold, staying within the EOA's current POL budget.

Steps executed automatically when gas <= GAS_TARGET_GWEI:
  1. Safe execTransaction: transfer all pUSD from proxy → EOA
  2. EOA approve CTF V2 Exchange to spend pUSD (MAX)
  3. EOA approve NegRisk V2 Exchange to spend pUSD (MAX)
  4. CLOB /balance-allowance/update to sync balance

Run once and leave it running in background. It will self-terminate after success.
Typical wait: 1–4 hours (Polygon gas is usually 30–60 gwei overnight).
"""

import os, sys, time
from dotenv import load_dotenv

SD = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SD, ".env"), override=True)

PRIVATE_KEY  = os.getenv("PRIVATE_KEY", "")
EOA          = os.getenv("POLYMARKET_PROXY_ADDRESS", "")   # your EOA wallet
OLD_PROXY    = "0x89D37809EDc9e8718819B62F07DD10A27a442bA9"

PUSD_ADDR    = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"   # pUSD (V2 collateral)
CTF_V2       = "0xE111180000d2663C0091e4f400237545B87B996B"   # V2 CTF Exchange
NEG_RISK_V2  = "0xe2222d279d744050d28e00520010520000310F59"   # V2 NegRisk Exchange
ZERO_ADDR    = "0x0000000000000000000000000000000000000000"
MAX_UINT     = 2**256 - 1

# Fire when gas <= this (gwei). At 55 gwei the full sequence costs ~0.0104 POL.
# With 0.013744 POL in EOA, we can fire at anything <= 57 gwei.
GAS_TARGET_GWEI = 57
CHECK_INTERVAL  = 30   # seconds between gas checks

RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
]

ERC20_ABI = [
    {"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"s","type":"address"},{"name":"a","type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"type":"function"},
    {"inputs":[{"name":"r","type":"address"},{"name":"a","type":"uint256"}],"name":"transfer","outputs":[{"type":"bool"}],"type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"type":"uint256"}],"type":"function"},
]
SAFE_ABI = [
    {"inputs":[],"name":"nonce","outputs":[{"type":"uint256"}],"type":"function"},
    {"inputs":[
        {"name":"to","type":"address"},{"name":"value","type":"uint256"},
        {"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},
        {"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},
        {"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},
        {"name":"refundReceiver","type":"address"},{"name":"_nonce","type":"uint256"}
    ],"name":"getTransactionHash","outputs":[{"name":"","type":"bytes32"}],"type":"function"},
    {"inputs":[
        {"name":"to","type":"address"},{"name":"value","type":"uint256"},
        {"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},
        {"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},
        {"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},
        {"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}
    ],"name":"execTransaction","outputs":[{"name":"success","type":"bool"}],"type":"function"},
]


def connect():
    from web3 import Web3
    for rpc in RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    raise RuntimeError("No Polygon RPC available")


def sign_safe_tx(w3, safe_contract, to, data, nonce_val):
    from eth_account.messages import encode_defunct
    from eth_account import Account
    from web3 import Web3
    zero = Web3.to_checksum_address(ZERO_ADDR)
    tx_hash_bytes = safe_contract.functions.getTransactionHash(
        Web3.to_checksum_address(to), 0, data, 0,
        0, 0, 0, zero, zero, nonce_val
    ).call()
    msg    = encode_defunct(primitive=tx_hash_bytes)
    signed = Account.sign_message(msg, private_key=PRIVATE_KEY)
    r, s, v = signed.r, signed.s, signed.v
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v + 4])


def exec_safe_transfer(w3, safe, pusd, account, proxy_bal, gas_price):
    """Transfer all pUSD from proxy to EOA via Safe execTransaction."""
    from web3 import Web3
    zero = Web3.to_checksum_address(ZERO_ADDR)
    data = pusd.encode_abi("transfer", args=[Web3.to_checksum_address(EOA), proxy_bal])
    nonce = safe.functions.nonce().call()
    sig   = sign_safe_tx(w3, safe, PUSD_ADDR, data, nonce)

    tx = safe.functions.execTransaction(
        Web3.to_checksum_address(PUSD_ADDR), 0, data, 0,
        0, 0, 0, zero, zero, sig
    ).build_transaction({
        "from":     account.address,
        "nonce":    w3.eth.get_transaction_count(account.address, "pending"),
        "gas":      110_000,
        "gasPrice": gas_price,
    })
    raw = getattr(w3.eth.account.sign_transaction(tx, PRIVATE_KEY),
                  "raw_transaction", None) or w3.eth.account.sign_transaction(tx, PRIVATE_KEY).rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    print(f"  Transfer TX: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    ok = receipt.status == 1
    print(f"  Status: {'✅ SUCCESS' if ok else '❌ REVERTED'}")
    return ok


def exec_eoa_approve(w3, token_addr, spender, account, gas_price):
    """Approve spender from EOA."""
    from web3 import Web3
    token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    tx = token.functions.approve(
        Web3.to_checksum_address(spender), MAX_UINT
    ).build_transaction({
        "from":     account.address,
        "nonce":    w3.eth.get_transaction_count(account.address, "pending"),
        "gas":      60_000,
        "gasPrice": gas_price,
    })
    raw = getattr(w3.eth.account.sign_transaction(tx, PRIVATE_KEY),
                  "raw_transaction", None) or w3.eth.account.sign_transaction(tx, PRIVATE_KEY).rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    print(f"  Approve TX:  {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    ok = receipt.status == 1
    print(f"  Status: {'✅ SUCCESS' if ok else '❌ REVERTED'}")
    return ok


def sync_clob():
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
        api_key  = os.getenv("API_KEY",    os.getenv("CLOB_API_KEY", ""))
        api_sec  = os.getenv("API_SECRET", os.getenv("CLOB_SECRET", ""))
        api_pass = os.getenv("API_PASSPHRASE", os.getenv("CLOB_PASSPHRASE", ""))
        creds  = ApiCreds(api_key=api_key, api_secret=api_sec, api_passphrase=api_pass)
        client = ClobClient(host="https://clob.polymarket.com", chain_id=137,
                            key=PRIVATE_KEY, creds=creds, signature_type=0)
        client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        time.sleep(3)
        result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        bal = float(result.get("balance", 0)) if isinstance(result, dict) else 0
        print(f"  CLOB balance after sync: ${bal:.2f}")
        return bal
    except Exception as e:
        print(f"  CLOB sync: {e}")
        return 0.0


def run_migration(w3, gas_price):
    from web3 import Web3
    from eth_account import Account

    account = w3.eth.account.from_key(PRIVATE_KEY)
    pusd    = w3.eth.contract(address=Web3.to_checksum_address(PUSD_ADDR), abi=ERC20_ABI)
    safe    = w3.eth.contract(address=Web3.to_checksum_address(OLD_PROXY), abi=SAFE_ABI)

    proxy_bal = pusd.functions.balanceOf(Web3.to_checksum_address(OLD_PROXY)).call()
    eoa_bal   = pusd.functions.balanceOf(Web3.to_checksum_address(EOA)).call()
    pol_bal   = w3.eth.get_balance(account.address)

    print(f"  pUSD in proxy: ${proxy_bal/1e6:.2f}")
    print(f"  pUSD in EOA:   ${eoa_bal/1e6:.2f}")
    print(f"  POL available: {pol_bal/1e18:.6f}")

    # Step 1: transfer pUSD from proxy to EOA
    if proxy_bal > 0:
        print("\n[1/3] Transferring pUSD from proxy → EOA...")
        ok = exec_safe_transfer(w3, safe, pusd, account, proxy_bal, gas_price)
        if not ok:
            print("  Transfer failed — aborting.")
            return False
        time.sleep(4)
    else:
        print("[1/3] No pUSD in proxy (already moved).")

    # Step 2: approve CTF V2
    print("\n[2/3] Approving CTF V2 Exchange...")
    exec_eoa_approve(w3, PUSD_ADDR, CTF_V2, account, gas_price)
    time.sleep(2)

    # Step 3: approve NegRisk V2
    print("\n[3/3] Approving NegRisk V2 Exchange...")
    exec_eoa_approve(w3, PUSD_ADDR, NEG_RISK_V2, account, gas_price)
    time.sleep(2)

    # Step 4: verify and sync CLOB
    eoa_bal2 = pusd.functions.balanceOf(Web3.to_checksum_address(EOA)).call()
    print(f"\nOn-chain EOA pUSD: ${eoa_bal2/1e6:.2f}")
    print("\nSyncing CLOB balance...")
    clob_bal = sync_clob()

    print("\n" + "=" * 55)
    if eoa_bal2 > 0:
        print(f"✅ Migration complete! ${eoa_bal2/1e6:.2f} pUSD in your EOA.")
        print(f"   CLOB balance: ${clob_bal:.2f}")
        print("   Restart the trading bot to pick up the new balance.")
    else:
        print("⚠  Something went wrong. Check TX hashes above.")
    print("=" * 55)
    return eoa_bal2 > 0


def main():
    from web3 import Web3
    print("=" * 55)
    print(" gas_watcher.py — Auto balance migration")
    print("=" * 55)
    print(f" Target: gas ≤ {GAS_TARGET_GWEI} gwei")
    print(f" EOA:    {EOA}")
    print(f" Checking every {CHECK_INTERVAL}s. Leave running.\n")

    if not PRIVATE_KEY:
        print("ERROR: PRIVATE_KEY not in .env"); sys.exit(1)

    attempt = 0
    while True:
        try:
            w3 = connect()
            gp = w3.eth.gas_price
            gwei = gp / 1e9
            pol_bal = w3.eth.get_balance(Web3.to_checksum_address(EOA))

            # Estimate total cost at current gas
            est_cost = 230_000 * gp / 1e18   # 110k safe + 60k + 60k gas
            affordable = pol_bal / 1e18 >= est_cost * 1.05

            attempt += 1
            ts = time.strftime("%H:%M:%S")
            status = "✅ FIRE" if (gwei <= GAS_TARGET_GWEI and affordable) else "⏳ wait"
            print(f"[{ts}] Gas: {gwei:.1f} gwei | POL: {pol_bal/1e18:.5f} | "
                  f"Est cost: {est_cost:.5f} POL | {status}")

            if gwei <= GAS_TARGET_GWEI and affordable:
                print(f"\n🚀 Gas dropped to {gwei:.1f} gwei — executing migration NOW!")
                success = run_migration(w3, gp)
                if success:
                    sys.exit(0)
                else:
                    print("Migration failed. Will retry next gas window...")

        except KeyboardInterrupt:
            print("\nStopped.")
            sys.exit(0)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
