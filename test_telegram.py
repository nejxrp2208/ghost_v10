"""
Verbose Telegram tester — prints the actual API response so you can
see what's failing. Run with: python test_telegram.py

Uses the OS cert store (truststore) so it works on Windows machines
with antivirus HTTPS scanning. Falls back to certifi, then default.
"""
import os
import ssl
import asyncio
import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    print("WARN: python-dotenv not installed, falling back to OS env")

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

print(f"Token present:  {bool(TOKEN)}  ({TOKEN[:12]}... len={len(TOKEN)})")
print(f"ChatID present: {bool(CHAT_ID)} ({CHAT_ID})")
print()

if not TOKEN or not CHAT_ID:
    print("ERROR: Missing creds. Check .env has both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
    raise SystemExit(1)


def _make_ssl_context():
    """Trust the OS cert store first (handles AV MITM roots)."""
    try:
        import truststore
        print("SSL: using truststore (OS cert store)")
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        pass
    try:
        import certifi
        print("SSL: using certifi (no truststore installed)")
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    print("SSL: using Python default (truststore + certifi missing)")
    return ssl.create_default_context()


async def main():
    ssl_ctx = _make_ssl_context()
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as s:
        # Step 1 — verify bot token by hitting getMe
        url = f"https://api.telegram.org/bot{TOKEN}/getMe"
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            print(f"getMe -> HTTP {r.status}")
            print(f"        {data}")
            if not data.get("ok"):
                print("\nFAIL: Bot token is rejected. Get a fresh one from @BotFather.")
                return
            bot_username = data["result"]["username"]
            print(f"        Bot is @{bot_username}\n")

        # Step 2 — try to send the message
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        async with s.post(
            url,
            json={"chat_id": CHAT_ID, "text": "GhostScanner test message", "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            data = await r.json()
            print(f"sendMessage -> HTTP {r.status}")
            print(f"               {data}")
            if data.get("ok"):
                print("\nSUCCESS: Telegram is wired correctly.")
            else:
                desc = data.get("description", "")
                print(f"\nFAIL: {desc}")
                if "chat not found" in desc.lower():
                    print(f"\n>>> You need to message @{bot_username} first.")
                    print(f"    1. Open Telegram, search for @{bot_username}")
                    print(f"    2. Tap Start (or send /start)")
                    print(f"    3. Re-run this script")
                elif "bot was blocked" in desc.lower():
                    print(f"\n>>> You blocked @{bot_username}. Unblock it and re-run.")


if __name__ == "__main__":
    asyncio.run(main())
