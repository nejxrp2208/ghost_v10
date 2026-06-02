#!/usr/bin/env python3
"""
ZUGU v3.5 — live terminal dashboard (predator-style Rich TUI).

Reads state.json (atomic 1Hz dump from scanner.py) + trades DB (read-only).
NEVER mutates anything — pure display.

Run in its own terminal:  python3 crypto_ghost_dashboard.py

Layout
──────
  ┌── header ───────────────────────────────────────────────┐
  │  🔮 ZUGU v3.5 // FOLLOW LEADER // PAPER  ● FEED ▶ TRADING │
  ├── mid ──────────────────────────────────────────────────┤
  │ WALLET   │ FEED + GUARDS │ OPEN TRADES                  │
  ├── low ──────────────────────────────────────────────────┤
  │ TRACK RECORD               │ PER COIN                   │
  └──────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from rich.align   import Align
from rich.console import Console
from rich.layout  import Layout
from rich.live    import Live
from rich.panel   import Panel
from rich.table   import Table
from rich.text    import Text


# ── Paths ────────────────────────────────────────────────────────────────────
SD     = os.path.dirname(os.path.abspath(__file__))
STATE  = os.path.join(SD, "state.json")
_PAPER = os.getenv("PAPER_TRADE", "true").strip().lower() in ("true", "1", "yes")
DB     = os.path.join(SD, "crypto_ghost_PAPER.db" if _PAPER else "crypto_ghost.db")

console = Console()


# ── Palette (ZUGU brand = magenta to distinguish from predator's cyan) ──────
BORDER = "bright_magenta"; TITLE  = "bold bright_magenta"
LBL    = "bright_cyan";    VAL    = "bold white";   DIM = "grey58"
WIN    = "bold green3";    LOSS   = "bold red1"
UP     = "bold green3";    DOWN   = "bold orange1"
HOT    = "bold yellow";    ACCENT = "bold deep_sky_blue1"
ZUGU   = "bold magenta"

COIN_CLR = {"BTC": "bold yellow", "ETH": "bold deep_sky_blue1"}


def coin_txt(c: str) -> Text:
    return Text(c, style=COIN_CLR.get(c, VAL))


def money(x) -> Text:
    if x is None:
        return Text("—", style=DIM)
    try:
        xv = float(x)
    except (TypeError, ValueError):
        return Text("—", style=DIM)
    s = WIN if xv > 0 else (LOSS if xv < 0 else VAL)
    return Text(f"${xv:+,.2f}", style=s)


# ── State loaders ────────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def q(sql: str, args=()) -> list:
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
        rows = c.execute(sql, args).fetchall()
        c.close()
        return rows
    except Exception:
        return []


# ── HEADER ───────────────────────────────────────────────────────────────────
def header(st: dict) -> Panel:
    mode = st.get("mode", "?")
    live = bool(st) and (time.time() - float(st.get("ts", 0)) < 5)
    halt = st.get("halt")

    t = Text()
    t.append("  🔮 ZUGU ", style="bold bright_white")
    t.append(f"v{st.get('version', '3.5')} ", style=ZUGU)
    t.append(f"// {str(st.get('strategy', 'FOLLOW_LEADER')).replace('_',' ')} // ",
             style=ACCENT)
    t.append(mode, style=HOT if mode == "LIVE" else WIN)
    t.append("      ")
    t.append("● FEED LIVE" if live else "● FEED STALE",
             style=WIN if live else LOSS)
    if halt:
        t.append(f"      ⛔ HALTED: {halt}", style="bold white on red3")
    else:
        t.append("      ▶ TRADING", style=WIN)
    t.append(f"   {datetime.now(timezone.utc):%H:%M:%S} UTC", style="bright_cyan")
    return Panel(Align.center(t), style="on grey15",
                 border_style=(LOSS if halt else BORDER), height=3)


# ── WALLET ───────────────────────────────────────────────────────────────────
def wallet_panel(st: dict) -> Panel:
    stats = st.get("stats", {}) or {}
    today = float(stats.get("today_pnl") or 0)
    pnl   = float(stats.get("lifetime_pnl") or 0)
    won   = int(stats.get("lifetime_won") or 0)
    lost  = int(stats.get("lifetime_lost") or 0)

    portfolio = float(os.getenv("PORTFOLIO_SIZE", "200"))
    balance   = portfolio + pnl
    n         = won + lost
    wr        = (won / n * 100) if n else 0.0
    cost_row  = q("SELECT COALESCE(SUM(size_usdc),0) FROM trades "
                  "WHERE status IN('won','lost')")
    cost      = float(cost_row[0][0]) if cost_row else 0.0
    roi       = (pnl / cost * 100) if cost > 0 else 0.0

    t = Table.grid(padding=(0, 1))
    t.add_column(style=LBL, width=10)
    t.add_column(width=18)
    t.add_row("Balance",  money(balance))
    t.add_row("Today",    money(today))
    t.add_row("All-time", money(pnl))
    t.add_row("Record",   Text(f"{won}W / {lost}L  ({n})", style=VAL))
    t.add_row("Win rate", Text(f"{wr:.1f}%", style=ACCENT))
    t.add_row("ROI",      Text(f"{roi:+.1f}%",
                               style=WIN if roi > 0 else (LOSS if roi < 0 else VAL)))
    t.add_row("", "")
    t.add_row("Open", Text(str(stats.get("open_trades", 0)), style=HOT))
    return Panel(t, title=Text("WALLET", style=TITLE), border_style=BORDER)


# ── FEED + GUARDRAILS ────────────────────────────────────────────────────────
def feed_panel(st: dict) -> Panel:
    prices = st.get("prices", {}) or {}
    cfg    = st.get("config", {}) or {}
    bias   = st.get("bias", {}) or {}
    stats  = st.get("stats", {}) or {}

    t = Table.grid(padding=(0, 1))
    t.add_column(style=LBL, width=12)
    t.add_column(width=20)

    for c in ("BTC", "ETH"):
        if c in prices:
            t.add_row(coin_txt(c), Text(f"${float(prices[c]):,.2f}", style=VAL))
    t.add_row("", "")
    t.add_row("sizing",   Text(str(cfg.get("sizing", "—")), style=HOT))
    t.add_row("ask band",
              Text(f"{cfg.get('min_entry_ask','?')}–{cfg.get('max_entry_ask','?')}",
                   style=VAL))
    t.add_row("window",
              Text(f"{cfg.get('min_secs_left','?')}–{cfg.get('max_secs_left','?')}s",
                   style=VAL))
    t.add_row("max open", Text(str(cfg.get("max_open_trades", "?")), style=VAL))
    skip_h = cfg.get("skip_hours_utc") or []
    if skip_h:
        t.add_row("skip hr", Text(",".join(str(h) for h in skip_h), style=DIM))
    if cfg.get("skip_weekends"):
        t.add_row("weekends", Text("skipped", style=DIM))

    # ── Guardrails ──
    t.add_row("", "")
    dll = float(os.getenv("FL_DAILY_LOSS_LIMIT", "0"))
    if dll > 0:
        d = float(stats.get("today_pnl") or 0)
        s = LOSS if d <= -dll * 0.7 else (HOT if d < 0 else WIN)
        t.add_row("day stop", Text(f"${d:+.2f} / -${dll:.0f}", style=s))
    r24 = float(os.getenv("FL_ROLLING_24H_LIMIT", "0"))
    if r24 > 0:
        t.add_row("24h cap", Text(f"-${r24:.0f}", style=DIM))
    streak = int(os.getenv("FL_MAX_LOSS_STREAK", "0"))
    if streak > 0:
        t.add_row("streak cap", Text(f"{streak} losses", style=DIM))

    # ── Bias tracker (the key ZUGU-specific metric) ──
    if bias:
        t.add_row("", "")
        for asset in ("BTC", "ETH"):
            info = bias.get(asset) or bias.get(asset.lower()) or {}
            val_bps = float(info.get("value") or 0) * 10000
            n_samp  = int(info.get("samples") or 0)
            t.add_row(f"bias {asset.lower()}",
                      Text(f"{val_bps:+.1f}bps (n={n_samp})", style=ACCENT))

    return Panel(t, title=Text("FEED + GUARDRAILS", style=TITLE), border_style=BORDER)


# ── OPEN TRADES ──────────────────────────────────────────────────────────────
def open_trades_panel() -> Panel:
    rows = q("SELECT id, coin, outcome, entry_price, size_usdc, ts "
             "FROM trades WHERE status='open' ORDER BY id DESC LIMIT 8")
    if not rows:
        return Panel(Align.center(Text("no open trades — flat", style=DIM)),
                     title=Text("OPEN TRADES", style=TITLE), border_style=BORDER)
    t = Table(expand=True, box=None, pad_edge=False)
    for c, j in [("#", "right"), ("coin", "left"), ("side", "center"),
                 ("entry", "right"), ("risk", "right"), ("opened", "right")]:
        t.add_column(Text(c, style=LBL), justify=j)
    for trade_id, coin, side, entry, size, ts in rows:
        side_t = (Text("▲ UP", style=UP) if side == "UP"
                  else Text("▼ DOWN", style=DOWN))
        t.add_row(Text(f"#{trade_id}", style=ACCENT),
                  coin_txt(coin), side_t,
                  Text(f"{float(entry):.3f}", style=VAL),
                  Text(f"${float(size):.2f}", style=HOT),
                  Text((ts or "")[-8:], style=DIM))
    return Panel(t, title=Text("OPEN TRADES", style=TITLE), border_style=BORDER)


# ── TRACK RECORD ─────────────────────────────────────────────────────────────
def record_panel() -> Panel:
    rows = q("SELECT substr(ts,12,8), coin, outcome, entry_price, size_usdc, pnl, status "
             "FROM trades WHERE status IN('won','lost') ORDER BY id DESC LIMIT 12")
    if not rows:
        return Panel(Align.center(Text("no resolved trades yet", style=DIM)),
                     title=Text("TRACK RECORD (resolved)", style=TITLE),
                     border_style=BORDER)
    t = Table(expand=True, box=None, pad_edge=False)
    for c, j in [("time", "left"), ("coin", "left"), ("side", "center"),
                 ("entry", "right"), ("risk", "right"),
                 ("result", "center"), ("pnl", "right")]:
        t.add_column(Text(c, style=LBL), justify=j)
    for tm, coin, side, entry, size, pnl, status in rows:
        side_t = (Text("▲ UP", style=UP) if side == "UP"
                  else Text("▼ DOWN", style=DOWN))
        res = (Text("✔ WIN", style=WIN) if status == "won"
               else Text("✘ LOSS", style=LOSS))
        t.add_row(Text(tm or "", style=DIM),
                  coin_txt(coin), side_t,
                  Text(f"{float(entry):.3f}", style=VAL),
                  Text(f"${float(size):.2f}", style=VAL),
                  res, money(pnl))
    return Panel(t, title=Text("TRACK RECORD  (resolved)", style=TITLE),
                 border_style=BORDER)


# ── PER COIN ─────────────────────────────────────────────────────────────────
def percoin_panel() -> Panel:
    rows = q("SELECT coin, COUNT(*), "
             "COALESCE(SUM(status='won'),0), "
             "COALESCE(SUM(pnl),0) "
             "FROM trades WHERE status IN('won','lost') GROUP BY coin")
    t = Table.grid(padding=(0, 2))
    for _ in range(4):
        t.add_column()
    t.add_row(Text("coin", style=LBL), Text("n", style=LBL),
              Text("WR", style=LBL),   Text("pnl", style=LBL))
    if not rows:
        t.add_row(Text("—", style=DIM), "", "", "")
    for coin, n, won, pnl in rows:
        wr = (won / n * 100) if n else 0.0
        t.add_row(coin_txt(coin),
                  Text(str(n), style=VAL),
                  Text(f"{wr:.0f}%", style=ACCENT),
                  money(pnl))
    return Panel(t, title=Text("PER COIN", style=TITLE), border_style=BORDER)


# ── RENDER ───────────────────────────────────────────────────────────────────
def render() -> Layout:
    st  = load_state()
    lay = Layout()
    lay.split_column(
        Layout(header(st), size=3),
        Layout(name="mid", size=18),
        Layout(name="low"),
    )
    lay["mid"].split_row(
        Layout(wallet_panel(st),    ratio=2),
        Layout(feed_panel(st),      ratio=2),
        Layout(open_trades_panel(), ratio=5),
    )
    lay["low"].split_row(
        Layout(record_panel(),  ratio=3),
        Layout(percoin_panel(), ratio=1),
    )
    return lay


def main() -> None:
    with Live(render(), console=console, refresh_per_second=2, screen=True) as live:
        while True:
            time.sleep(0.5)
            live.update(render())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bright_magenta]dashboard closed[/]")
