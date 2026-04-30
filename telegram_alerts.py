"""
Telegram alerts — fire-and-forget Telegram notifications.

Set in your .env:
    TELEGRAM_BOT_TOKEN=<your bot token>     (or TELEGRAM_TOKEN)
    TELEGRAM_CHAT_ID=<your chat id>

If either is missing, send_alert() is a silent no-op so the bot
keeps running with no Telegram setup at all.

SSL: on Windows machines with antivirus HTTPS scanning, the system
cert store has the AV root injected but Python's bundled certifi
roots do not. We use `truststore` (Windows cert store) when available,
falling back to certifi.
"""

import os
import ssl
import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# Accept either env var name so existing .env files just work.
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def _make_ssl_context():
    """Return an SSL context that trusts the OS cert store (handles AV MITM)."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    return ssl.create_default_context()


async def send_alert(msg: str) -> None:
    """Send a Telegram message. No-op if creds are missing."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        ssl_ctx = _make_ssl_context()
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            await session.post(
                url,
                json={
                    "chat_id":    TELEGRAM_CHAT_ID,
                    "text":       msg,
                    "parse_mode": "HTML",
                },
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        # Never let alerting break the bot.
        pass
