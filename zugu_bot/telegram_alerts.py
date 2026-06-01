"""
ZUGU v3.5 — Telegram alerts module.

Branded with 🔮 ZUGU prefix to distinguish from other bots (e.g. PREDATOR uses 🎯).
Supports two call styles:

  - async send_alert(msg)  — used by resolver.py (matches existing contract)
  - sync  tg(msg)          — fire-and-forget for scanner.py async tasks

Plus pre-built ZUGU-branded helpers:
  - alert_entry(...)     fire on order placement
  - alert_halt(reason)   guardrail trip
  - alert_heartbeat(...) periodic health summary

All functions are SAFE NO-OPs when TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is
empty in .env, so missing creds NEVER break the bot.

Stdlib only. No new pip deps.
"""
from __future__ import annotations

import asyncio
import os
import urllib.parse
import urllib.request


TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TIMEOUT  = 8

# Brand prefix — all ZUGU messages start with this so they're easy to filter.
_ZUGU = "🔮 <b>ZUGU</b>"


def _send_sync(msg: str) -> None:
    """Blocking POST to Telegram. Always swallows errors."""
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":                  TG_CHAT,
            "text":                     msg,
            "parse_mode":               "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=TIMEOUT).read()
    except Exception:
        # never raise — telegram failure must not break the bot
        pass


async def send_alert(msg: str) -> None:
    """Async helper — used by resolver. Runs blocking HTTP in a thread so the
    asyncio loop is never stalled by network IO."""
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        await asyncio.to_thread(_send_sync, msg)
    except Exception:
        pass


def tg(msg: str) -> None:
    """Sync fire-and-forget wrapper. Schedules send in the running event loop
    if available, falls back to a blocking send otherwise."""
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_send_sync, msg))
    except RuntimeError:
        # no running loop — direct sync send (used in __main__ scripts)
        _send_sync(msg)


# ── Branded helpers — ZUGU-specific ─────────────────────────────────────────

def alert_entry(coin: str, side: str, ask: float, shares: float, size_usdc: float,
                bias_bps: float, lead_pct: float, secs_left: int) -> None:
    """Sent on every fire if FL_TG_ENTRY_ALERT=true. Use carefully — zugu fires
    ~50/day so this can spam unless filtered."""
    icon = "🟢" if side.upper() == "UP" else "🔴"
    tg(f"{_ZUGU} <b>FIRE</b>\n"
       f"{icon} {coin} {side} @ <code>${ask:.4f}</code>\n"
       f"💰 {shares:.2f} shares · risk <code>${size_usdc:.2f}</code>\n"
       f"📊 bias <code>{bias_bps:+.1f}bps</code> · lead <code>{lead_pct*100:.3f}%</code>\n"
       f"⏰ {secs_left}s left")


def alert_halt(reason: str) -> None:
    """Sent ONCE when guardrail trips (the supervisor de-duplicates re-fires)."""
    tg(f"{_ZUGU} ⛔ <b>HALTED</b>\n{reason}")


def alert_heartbeat(open_n: int, today_pnl: float,
                    total_w: int, total_l: int, total_pnl: float) -> None:
    """Periodic health summary — sent every FL_TG_HEARTBEAT_MIN minutes if > 0."""
    n  = total_w + total_l
    wr = (total_w / n * 100) if n else 0.0
    tg(f"{_ZUGU} heartbeat\n"
       f"open: {open_n} · today: <code>${today_pnl:+.2f}</code>\n"
       f"lifetime: {total_w}W/{total_l}L ({wr:.1f}%) · <code>${total_pnl:+.2f}</code>")
