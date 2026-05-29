# -*- coding: utf-8 -*-
"""
Terminal Dashboard — Polymarket Paper Trading Bot
==================================================
Run:   python tui.py
Quit:  q  or  Ctrl-C
"""

import time
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.layout import Layout
from rich.rule import Rule
from rich.style import Style
from rich.spinner import Spinner
from rich.progress_bar import ProgressBar
from rich import box

import database as db
import config

console = Console()

REFRESH_SECS = 5

# ── Palette ───────────────────────────────────────────────────────────────────
C_GOLD      = "bold yellow"
C_GREEN     = "bold bright_green"
C_RED       = "bold bright_red"
C_CYAN      = "bold bright_cyan"
C_BLUE      = "bold bright_blue"
C_MAGENTA   = "bold bright_magenta"
C_DIM       = "dim white"
C_WHITE     = "bold white"

BORDER_HEADER   = "bright_blue"
BORDER_POS      = "cyan"
BORDER_TRADES   = "magenta"
BORDER_SIGNALS  = "green"
BORDER_LOGS     = "grey42"
BORDER_FOOTER   = "grey30"

# Tier badge colours
TIER_STYLE = {
    "T0": ("black", "bright_cyan"),
    "T1": ("black", "bright_green"),
    "T2": ("black", "yellow"),
    "T3": ("black", "orange3"),
}

# ── Colour helpers ────────────────────────────────────────────────────────────

def _pnl_style(val: float) -> str:
    if val > 0:  return C_GREEN
    if val < 0:  return C_RED
    return C_DIM

def _conf_style(c: float) -> str:
    if c >= 0.85: return "bold bright_green"
    if c >= 0.75: return "bold green"
    if c >= 0.65: return "bold yellow"
    return "white"

def _tier_badge(tier: str) -> str:
    key = tier[:2]
    fg, bg = TIER_STYLE.get(key, ("white", "grey42"))
    return f"[{fg} on {bg}] {tier} [/]"

def _arrow(cur: float, entry: float) -> str:
    if cur > entry:   return "[bright_green]▲[/]"
    if cur < entry:   return "[bright_red]▼[/]"
    return "[dim]=[/]"

def _held_style(hours: float) -> str:
    if hours >= 20:  return "bold bright_red"
    if hours >= 12:  return "yellow"
    return "dim white"

def _bar(value: float, total: float, width: int = 8) -> str:
    """Mini ASCII progress bar."""
    if total <= 0:
        return "[dim]--------[/]"
    pct = min(1.0, value / total)
    filled = round(pct * width)
    bar    = "#" * filled + "-" * (width - filled)
    col    = "bright_green" if pct >= 0.5 else "yellow"
    return f"[{col}]{bar}[/]"


# ── Header ────────────────────────────────────────────────────────────────────

def _make_header(cash: float, start: float, open_cnt: int, closed_cnt: int,
                 total_pnl: float, win_rate: float, wins: int, losses: int) -> Panel:

    now   = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    pnl_s = _pnl_style(total_pnl)

    title_line = Text()
    title_line.append("  POLYMARKET PAPER BOT  ", style="bold white on dark_blue")
    title_line.append("  Option A  ", style="bold black on bright_cyan")
    title_line.append(f"  entry 0.82–0.88  WIN @ 0.90  ", style="dim")
    title_line.append(f"  {now}  ", style="dim cyan")

    stats = Text()
    stats.append("\n  ")
    stats.append("CASH  ", style="dim")
    stats.append(f"${cash:,.2f}", style=C_GOLD)
    stats.append("   |   ", style="dim")
    stats.append("START  ", style="dim")
    stats.append(f"${start:,.0f}", style="dim yellow")
    stats.append("   |   ", style="dim")
    stats.append("OPEN  ", style="dim")
    stats.append(f"{open_cnt}", style=C_CYAN)
    stats.append("   |   ", style="dim")
    stats.append("CLOSED  ", style="dim")
    stats.append(f"{closed_cnt}", style="white")
    stats.append("   |   ", style="dim")
    stats.append("TOTAL P&L  ", style="dim")
    stats.append(f"${total_pnl:+,.2f}", style=pnl_s)
    stats.append("   |   ", style="dim")
    stats.append("WIN RATE  ", style="dim")
    if closed_cnt:
        stats.append(f"{win_rate:.0%}", style=C_GREEN if win_rate >= 0.7 else "yellow")
        stats.append(f"  ({wins}W / {losses}L)", style="dim")
    else:
        stats.append("—", style="dim")

    body = Text()
    body.append_text(title_line)
    body.append_text(stats)

    return Panel(body, border_style=BORDER_HEADER, padding=(0, 0))


# ── Open Positions ────────────────────────────────────────────────────────────

def _make_positions(positions: list) -> Panel:
    t = Table(
        box=box.SIMPLE,
        show_footer=False,
        header_style="bold cyan",
        row_styles=["", "dim"],   # alternating row tint
        expand=True,
        show_edge=False,
    )
    t.add_column("#",         width=4,  justify="right",  style="dim")
    t.add_column("TIER",      width=14)
    t.add_column("OUTCOME",   width=7)
    t.add_column("ENTRY",     width=8,  justify="right")
    t.add_column("NOW",       width=8,  justify="right")
    t.add_column("",          width=2,  justify="center")   # arrow
    t.add_column("SHARES",    width=8,  justify="right",  style="dim")
    t.add_column("COST",      width=9,  justify="right",  style="dim")
    t.add_column("UNREAL P&L",width=12, justify="right")
    t.add_column("HELD",      width=7,  justify="right")
    t.add_column("QUESTION",  min_width=28, no_wrap=True, style="dim white")

    if not positions:
        t.add_row("", "[dim]No open positions yet[/]", *[""] * 9)
    else:
        for p in positions[:18]:
            side    = p.get("side", "")
            parts   = side.split()
            tier    = parts[0] if parts else "?"
            outcome = parts[1] if len(parts) > 1 else "?"
            entry   = p.get("entry_price", 0)
            cur     = p.get("current_price") or entry
            shares  = p.get("size", 0)
            cost    = round(entry * shares, 2)
            upnl    = p.get("unrealized_pnl") or 0

            held_h   = 0.0
            held_str = "—"
            opened_s = p.get("opened_at", "")
            if opened_s:
                try:
                    ot     = datetime.fromisoformat(opened_s).replace(tzinfo=timezone.utc)
                    held_h = (datetime.now(timezone.utc) - ot).total_seconds() / 3600
                    held_str = f"{held_h:.1f}h"
                except Exception:
                    pass

            outcome_col = "bright_cyan" if outcome.upper() == "NO" else "bright_green"

            t.add_row(
                str(p["id"]),
                _tier_badge(tier),
                f"[{outcome_col}]{outcome}[/]",
                f"[dim]{entry:.4f}[/]",
                f"[{'bright_green' if cur >= entry else 'bright_red'}]{cur:.4f}[/]",
                _arrow(cur, entry),
                f"{shares:.2f}",
                f"${cost:.2f}",
                f"[{_pnl_style(upnl)}]{upnl:+.2f}[/]",
                f"[{_held_style(held_h)}]{held_str}[/]",
                p.get("question", "")[:50],
            )

    return Panel(
        t,
        title=f"[{C_CYAN}] Open Positions [/]",
        border_style=BORDER_POS,
        padding=(0, 0),
    )


# ── Signals ───────────────────────────────────────────────────────────────────

def _make_signals(signals: list) -> Panel:
    t = Table(
        box=box.SIMPLE,
        show_footer=False,
        header_style="bold green",
        row_styles=["", "dim"],
        expand=True,
        show_edge=False,
    )
    t.add_column("TIER",    width=14)
    t.add_column("CONF",    width=7,  justify="right")
    t.add_column("PRICE",   width=7,  justify="right", style="dim")
    t.add_column("TRADED",  width=7,  justify="center")
    t.add_column("QUESTION", min_width=30, no_wrap=True, style="dim white")

    if not signals:
        t.add_row("[dim]No signals yet[/]", *[""] * 4)
    else:
        for s in signals[:14]:
            acted = "[bold bright_green]YES[/]" if s.get("acted_on") else "[dim]no[/]"
            tier  = s.get("signal", "?")
            conf  = s.get("confidence", 0)
            t.add_row(
                _tier_badge(tier),
                f"[{_conf_style(conf)}]{conf:.0%}[/]",
                f"{s.get('price', 0):.4f}",
                acted,
                s.get("question", "")[:55],
            )

    return Panel(
        t,
        title=f"[{C_GREEN}] Signals [/]",
        border_style=BORDER_SIGNALS,
        padding=(0, 0),
    )


# ── Closed Trades ─────────────────────────────────────────────────────────────

def _make_trades(trades: list) -> Panel:
    t = Table(
        box=box.SIMPLE,
        show_footer=False,
        header_style="bold magenta",
        row_styles=["", "dim"],
        expand=True,
        show_edge=False,
    )
    t.add_column("#",       width=4,  justify="right", style="dim")
    t.add_column("SIDE",    width=22)
    t.add_column("ENTRY",   width=7,  justify="right", style="dim")
    t.add_column("EXIT",    width=7,  justify="right")
    t.add_column("SHARES",  width=8,  justify="right", style="dim")
    t.add_column("P&L",     width=11, justify="right")
    t.add_column("RESULT",  width=6,  justify="center")
    t.add_column("CLOSED",  width=11, style="dim")
    t.add_column("QUESTION", min_width=25, no_wrap=True, style="dim white")

    if not trades:
        t.add_row("", "[dim]No closed trades yet[/]", *[""] * 7)
    else:
        for tr in trades[:10]:
            pnl    = tr.get("realized_pnl", 0)
            result = "[bold bright_green] WIN [/]" if pnl > 0 else "[bold bright_red] LOSS[/]"
            closed = (tr.get("closed_at") or "")[:16].replace("T", " ")
            side   = tr.get("side", "")
            parts  = side.split()
            tier   = parts[0] if parts else "?"

            t.add_row(
                str(tr["id"]),
                f"{_tier_badge(tier)} {' '.join(parts[1:])}"[:22],
                f"{tr.get('entry_price', 0):.4f}",
                f"[{'bright_green' if pnl >= 0 else 'bright_red'}]{tr.get('exit_price', 0):.4f}[/]",
                f"{tr.get('size', 0):.2f}",
                f"[{_pnl_style(pnl)}]{pnl:+.4f}[/]",
                result,
                closed,
                tr.get("question", "")[:35],
            )

    return Panel(
        t,
        title=f"[{C_MAGENTA}] Closed Trades [/]",
        border_style=BORDER_TRADES,
        padding=(0, 0),
    )


# ── Logs ──────────────────────────────────────────────────────────────────────

def _make_logs(logs: list) -> Panel:
    lines = []
    for lg in logs[:20]:
        lvl = lg.get("level", "INFO")
        msg = lg.get("message", "")[:115]
        ts  = (lg.get("created_at") or "")[-8:]

        if lvl == "ERROR":
            lvl_tag = "[bold white on red] ERR [/]"
            msg_col = "bright_red"
        elif lvl == "WARNING":
            lvl_tag = "[bold black on yellow] WRN [/]"
            msg_col = "yellow"
        else:
            lvl_tag = "[dim] INF [/]"
            msg_col = "dim white"

        lines.append(f"[dim]{ts}[/] {lvl_tag} [{msg_col}]{msg}[/]")

    body = "\n".join(lines) if lines else "[dim]Waiting for first scan...[/]"
    return Panel(
        body,
        title=f"[dim white] Bot Logs [/]",
        border_style=BORDER_LOGS,
        padding=(0, 1),
    )


# ── Footer ────────────────────────────────────────────────────────────────────

def _make_footer() -> Panel:
    cap  = "Unlimited" if config.MAX_OPEN_POSITIONS == 0 else str(config.MAX_OPEN_POSITIONS)
    text = Text(justify="center")
    text.append("  [Q] Quit  ", style="bold white")
    text.append("  |  ", style="dim")
    text.append(f"Refresh: {REFRESH_SECS}s  ", style="dim")
    text.append("  |  ", style="dim")
    text.append(f"Balance: ${config.STARTING_BALANCE:,.0f}  ", style="dim yellow")
    text.append("  |  ", style="dim")
    text.append(f"Max Trade: ${config.MAX_TRADE_SIZE:.0f}  ", style="dim")
    text.append("  |  ", style="dim")
    text.append(f"Max Positions: {cap}  ", style="dim")
    text.append("  |  ", style="dim")
    text.append(f"Spread: {config.MAX_SPREAD}  ", style="dim")
    text.append("  |  ", style="dim")
    text.append(f"Vol Filter: ${config.MIN_VOLUME_24HR:,.0f}  ", style="dim")
    return Panel(text, border_style=BORDER_FOOTER, padding=(0, 0))


# ── Layout assembly ───────────────────────────────────────────────────────────

def _build() -> Layout:
    cash       = db.get_cash_balance()
    positions  = db.get_open_positions()
    trades     = db.get_closed_trades()
    signals    = db.get_signals(limit=14)
    logs       = db.get_logs(limit=20)

    open_cnt   = len(positions)
    closed_cnt = len(trades)
    total_upnl = sum(p.get("unrealized_pnl") or 0 for p in positions)
    total_rpnl = sum(t.get("realized_pnl")   or 0 for t in trades)
    total_pnl  = total_upnl + total_rpnl
    wins       = sum(1 for t in trades if (t.get("realized_pnl") or 0) > 0)
    losses     = closed_cnt - wins
    win_rate   = (wins / closed_cnt) if closed_cnt else 0.0

    layout = Layout()
    layout.split_column(
        Layout(name="header",   size=5),
        Layout(name="body"),
        Layout(name="footer",   size=3),
    )
    layout["body"].split_row(
        Layout(name="left",  ratio=3),
        Layout(name="right", ratio=2),
    )
    layout["left"].split_column(
        Layout(name="positions", ratio=3),
        Layout(name="trades",    ratio=2),
    )
    layout["right"].split_column(
        Layout(name="signals", ratio=2),
        Layout(name="logs",    ratio=3),
    )

    layout["header"].update(
        _make_header(cash, config.STARTING_BALANCE, open_cnt, closed_cnt,
                     total_pnl, win_rate, wins, losses)
    )
    layout["positions"].update(_make_positions(positions))
    layout["trades"].update(_make_trades(trades))
    layout["signals"].update(_make_signals(signals))
    layout["logs"].update(_make_logs(logs))
    layout["footer"].update(_make_footer())

    return layout


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    db.init_db()

    try:
        import msvcrt
        def _key_pressed() -> bool:
            return msvcrt.kbhit() and msvcrt.getch().lower() == b'q'
    except ImportError:
        def _key_pressed() -> bool:
            return False

    with Live(_build(), refresh_per_second=2, screen=True, console=console) as live:
        while True:
            for _ in range(REFRESH_SECS * 4):
                if _key_pressed():
                    console.clear()
                    console.print("[bold yellow]Dashboard closed.[/]")
                    return
                time.sleep(0.25)
            live.update(_build())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.clear()
        console.print("[bold yellow]Dashboard closed.[/]")
        sys.exit(0)
