#!/usr/bin/env python3
"""
GHOST PREDATOR — live terminal dashboard (per-coin hunt cards).

HUNT shows one card per coin with a predator status progression
(DORMANT → DRIFTING → STALKING → CLOSING IN → ◆ STRIKE), a readiness score,
live price/leader, and metric bars (move, time-left, ask, edge). Plus your
real stake and the profit if a snipe wins. Vibrant + colorblind-safe: every
status carries a text/symbol label, not just color.

Run in its own terminal:  python3 dashboard.py
"""
import os, json, time, sqlite3
from datetime import datetime, timezone, timedelta

_DUR_S={"5m":300,"15m":900,"1h":3600}
def slug_window(slug):
    """Market window in ET from slug, e.g. '6:40-6:45'."""
    try:
        p=(slug or "").split("-"); start=int(p[-1]); dur=_DUR_S.get(p[-2],300)
        s=datetime.fromtimestamp(start,timezone.utc)-timedelta(hours=4)
        e=datetime.fromtimestamp(start+dur,timezone.utc)-timedelta(hours=4)
        return f"{s.strftime('%-I:%M')}-{e.strftime('%-I:%M')}"
    except Exception: return ""
from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.live import Live
from rich.layout import Layout
from rich.text import Text
from rich.align import Align

SD = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(SD, "ghost_predator.db")
STATE = os.path.join(SD, "state.json")
console = Console()

BORDER="bright_cyan"; TITLE="bold bright_cyan"
LBL="bright_cyan"; VAL="bold white"; DIM="grey58"
WIN="bold green3"; LOSS="bold red1"; UP="bold green3"; DOWN="bold orange1"
ARM="bold magenta"; HOT="bold yellow"; ACCENT="bold deep_sky_blue1"; BARC="cyan"
COIN_CLR={"BTC":"bold yellow","ETH":"bold deep_sky_blue1","SOL":"bold magenta",
          "XRP":"bold bright_cyan","BNB":"bold orange1"}

def coin_txt(c): return Text(c, style=COIN_CLR.get(c, VAL))
def title(s):    return Text(s, style=TITLE)
def money(x):    return Text(f"${x:+,.2f}", style=WIN if x>0 else (LOSS if x<0 else VAL))

def bar(frac, width=10, style=BARC):
    frac=max(0.0,min(1.0,frac)); fill=int(round(frac*width))
    return Text("█"*fill, style=style)+Text("░"*(width-fill), style=DIM)

def load_state():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception: return None
def q(sql,args=()):
    try:
        c=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,timeout=3)
        r=c.execute(sql,args).fetchall(); c.close(); return r
    except Exception: return []

# win-prob proxy from backtested leader-persistence by time-left (when decisive)
def win_prob(secs, decisive):
    if not decisive: return 0.55
    for lim,p in [(5,0.97),(10,0.95),(20,0.93),(30,0.92),(60,0.90)]:
        if secs<=lim: return p
    return 0.88

# ── header ───────────────────────────────────────────────────────────────────
def header(st):
    mode=st.get("mode","?") if st else "?"
    live=(st and time.time()-st.get("ts",0)<5)
    halt=(st or {}).get("halt")
    regime=(st or {}).get("regime",{})
    gate=regime.get("gate",True)
    enabled=regime.get("enabled",False)
    edge=regime.get("edge")
    paper_n=regime.get("paper_n",0)
    window=regime.get("window",15)
    t=Text()
    t.append("  GHOST PREDATOR ",style="bold bright_white")
    t.append("// LATENCY SNIPE // ",style=ACCENT)
    t.append(f"{mode}",style=HOT if mode=="LIVE" else WIN)
    t.append("      "); t.append("● FEED LIVE" if live else "● FEED STALE",
                                  style=WIN if live else LOSS)
    # edge gate indicator
    if enabled:
        if gate:
            e_str = f"{edge:+.1%}" if edge is not None else "—"
            t.append(f"      🟢 EDGE ON {e_str}",style="bold green3")
        else:
            e_str = f"{edge:+.1%}" if edge is not None else "—"
            t.append(f"      🔴 NO EDGE · PAPER-WATCH {e_str}  paper {paper_n}/{window}",style="bold red1")
    if halt:
        t.append(f"      ⛔ HALTED: {halt}",style="bold white on red3")
    elif gate or not enabled:
        t.append("      ▶ TRADING",style=WIN)
    t.append(f"   {datetime.now(timezone.utc):%H:%M:%S} UTC",style="bright_cyan")
    border = LOSS if (halt or (enabled and not gate)) else BORDER
    return Panel(Align.center(t),style="on grey15",border_style=border,height=3)

# ── wallet (now with deployed + potential) ───────────────────────────────────
def wallet_panel(st):
    tot,won,lost,opn,pnl=(q("SELECT COUNT(*),COALESCE(SUM(status='won'),0),"
        "COALESCE(SUM(status='lost'),0),COALESCE(SUM(status='open'),0),"
        "COALESCE(SUM(pnl),0) FROM positions") or [(0,0,0,0,0)])[0]
    bankroll=st.get("bankroll",1000) if st else 1000
    cost=(q("SELECT COALESCE(SUM(size_usdc),0) FROM positions WHERE status IN('won','lost')") or [(0,)])[0][0]
    dep,potwin=(q("SELECT COALESCE(SUM(size_usdc),0),COALESCE(SUM(shares-size_usdc),0) "
                  "FROM positions WHERE status='open'") or [(0,0)])[0]
    roi=(pnl/cost*100) if cost else 0; wr=(won/(won+lost)*100) if (won+lost) else 0
    today=(q("SELECT COALESCE(SUM(pnl),0) FROM positions WHERE substr(ts,1,10)=?",
             (datetime.now(timezone.utc).strftime("%Y-%m-%d"),)) or [(0,)])[0][0]
    t=Table.grid(padding=(0,1)); t.add_column(style=LBL,width=10); t.add_column(width=16)
    t.add_row("Balance",money(bankroll+pnl)); t.add_row("Today",money(today))
    t.add_row("All-time",money(pnl))
    t.add_row("Record",Text(f"{won}W / {lost}L  ({tot})",style=VAL))
    t.add_row("Win rate",Text(f"{wr:.0f}%",style=ACCENT))
    t.add_row("ROI",Text(f"{roi:+.0f}%",style=WIN if roi>0 else (LOSS if roi<0 else VAL)))
    t.add_row("","");
    t.add_row("Deployed",Text(f"${dep:,.2f}",style=HOT if dep else VAL))
    t.add_row("If all win",Text(f"+${potwin:,.2f}",style=WIN if potwin else VAL))
    return Panel(t,title=title("WALLET"),border_style=BORDER)

def feed_panel(st):
    cfg=(st or {}).get("cfg",{}); px=(st or {}).get("prices",{})
    t=Table.grid(padding=(0,1)); t.add_column(style=LBL,width=10); t.add_column(width=14)
    for c in ("BTC","ETH","SOL","XRP","BNB"):
        if c in px: t.add_row(coin_txt(c),Text(f"${px[c]:,.2f}",style=VAL))
    t.add_row("","")
    t.add_row("stake",Text(f"${cfg.get('base_size','?')}/snipe",style=HOT))
    t.add_row("min move",Text(f"{cfg.get('min_move','?')}bps",style=VAL))
    t.add_row("max ask",Text(f"{cfg.get('max_ask','?')}",style=VAL))
    t.add_row("poll",Text(f"{cfg.get('book_poll','?')}ms",style=HOT))
    _bms=cfg.get('book_ms')
    t.add_row("latency",Text(f"{_bms}ms" if _bms is not None else "—",style=VAL))
    # guardrails — today's PnL toward the daily loss limit
    dll=cfg.get('daily_loss_limit');
    today=(q("SELECT COALESCE(SUM(pnl),0) FROM positions WHERE substr(ts,1,10)=?",
             (datetime.now(timezone.utc).strftime("%Y-%m-%d"),)) or [(0,)])[0][0]
    t.add_row("","")
    if dll:
        used=Text(f"${today:+.0f} / -${dll:.0f}",
                  style=LOSS if today<=-dll*0.7 else (HOT if today<0 else WIN))
        t.add_row("day stop",used)
    t.add_row("streak cap",Text(f"{cfg.get('max_loss_streak','?')} losses",style=VAL))
    # edge gate / regime
    regime=(st or {}).get("regime",{})
    if regime.get("enabled"):
        t.add_row("","")
        gate=regime.get("gate",True)
        edge=regime.get("edge")
        pn=regime.get("paper_n",0); pw=regime.get("window",15)
        pe=regime.get("paper_edge")
        e_txt=Text(f"{'ON' if gate else 'OFF'} {edge:+.1%}" if edge is not None else f"{'ON' if gate else 'OFF'} —",
                   style=WIN if gate else LOSS)
        t.add_row("edge gate",e_txt)
        t.add_row("paper",Text(f"{pn}/{pw}" + (f"  {pe:+.1%}" if pe is not None else ""),
                               style=WIN if (pe is not None and pe>=0) else (HOT if pn>0 else DIM)))
    return Panel(t,title=title("FEED + GUARDRAILS"),border_style=BORDER)

# ── HUNT cards (one per coin, video style) ───────────────────────────────────
def status_of(h, cfg):
    left=h["secs_left"]; dec=h["decisive"]; inw=h["in_window"]
    ask=h.get("ask"); maxask=cfg.get("max_ask",0.9)
    if inw and dec and ask is not None and ask<=maxask: return "◆ STRIKE", ARM
    if inw and dec:                                      return "◆ STRIKE (no ask)", ARM
    if inw:                                              return "HOLDING (flat)", HOT
    if left<=30:                                         return "CLOSING IN", HOT
    if left<=60:                                         return "STALKING", ACCENT
    if left<=120:                                        return "DRIFTING", BARC
    return "DORMANT", DIM

def coin_card(coin, h, cfg, regime=None):
    if h is None:
        g=Table.grid(); g.add_column()
        g.add_row(Text("no active market",style=DIM))
        return Panel(g,title=Text(f"◆ {coin}",style=COIN_CLR.get(coin,VAL)),
                     subtitle=Text("DORMANT",style=DIM),border_style=DIM,height=13)
    dur=h.get("dur","5m")
    left=h["secs_left"]; dec=h["decisive"]; mv=h["move_bps"]; ask=h.get("ask")
    price=h.get("price"); lead=h["leader"]; maxask=cfg.get("max_ask",0.9)
    minmv=cfg.get("min_move",3); base=cfg.get("base_size",5)
    stat,stat_style=status_of(h,cfg)
    wp=win_prob(left,dec)
    roi=(wp/ask-1) if ask else None
    profit=base*(1/ask-1) if ask else None
    # readiness score /100
    ts=max(0,min(55,(330-left)/330*55)); ds=25 if dec else min(25,abs(mv)/max(minmv,1)*25)
    asc=0
    if ask is not None: asc=20*max(0,(maxask-ask))/maxask if ask<=maxask else 5
    score=int(min(100,ts+ds+asc))

    g=Table.grid(padding=(0,1)); g.add_column(width=7,style=LBL); g.add_column(width=11)
    g.add_column(width=11)
    # row: readiness score bar
    g.add_row("ready", bar(score/100,10,stat_style.split()[-1] if False else BARC),
              Text(f"{score}/100",style=VAL))
    # price + leader + pct
    lt=Text(f"▲ {lead}",style=UP) if lead=="UP" else (Text(f"▼ {lead}",style=DOWN) if lead=="DOWN" else Text(lead,style=DIM))
    pr=Text(f"${price:,.2f}",style=VAL) if price else Text("—",style=DIM)
    g.add_row("price", pr, lt)
    g.add_row("", Text(f"{h.get('pct',0):+.3f}%",style=VAL), Text(f"{left}s left",style=HOT if left<=cfg.get('window',12) else VAL))
    g.add_row(Text("─"*6,style=DIM),Text("─"*11,style=DIM),Text("─"*11,style=DIM))
    # metric bars
    g.add_row("move", bar(min(1,abs(mv)/8.0),10, VAL), Text(f"{mv:+.1f}bps",style=VAL))
    if ask is not None:
        g.add_row("ask", bar(max(0,(1-ask)),10, WIN if ask<=maxask else DIM),
                  Text(f"{ask:.3f}",style=WIN if ask<=maxask else DIM))
        g.add_row("edge", bar(min(1,(roi or 0)),10, WIN if (roi or 0)>0 else DIM),
                  Text(f"{roi*100:+.0f}% ROI",style=WIN if roi>0 else DIM))
    else:
        g.add_row("ask", bar(0,10,DIM), Text("— (await)",style=DIM))
        g.add_row("edge", bar(0,10,DIM), Text("—",style=DIM))
    g.add_row(Text("─"*6,style=DIM),Text("─"*11,style=DIM),Text("─"*11,style=DIM))
    armed = stat.startswith("◆")
    gate_on = (regime or {}).get("gate", True) or not (regime or {}).get("enabled", False)
    if armed and ask is not None:
        if gate_on:
            g.add_row(Text("ENTER",style=ARM), Text(f"${base:.0f}",style=HOT),
                      Text(f"WIN +${profit:.2f}",style=WIN))
        else:
            g.add_row(Text("PAPER",style="bold yellow"), Text(f"${base:.0f}",style=DIM),
                      Text(f"~+${profit:.2f} shadow",style=DIM))
    elif ask is not None:
        g.add_row("stake", Text(f"${base:.0f}",style=DIM),
                  Text(f"~+${profit:.2f} if it arms",style=DIM))
    else:
        g.add_row("stake", Text(f"${base:.0f}",style=DIM), Text("(no ask yet)",style=DIM))
    _win=h.get("window","")
    return Panel(g, title=Text(f"◆ {coin} · {dur}"+(f" · {_win}" if _win else ""),style=COIN_CLR.get(coin,VAL)),
                 subtitle=Text(stat,style=stat_style),
                 border_style=stat_style if stat.startswith("◆") else BORDER, height=13)

def price_chart(coin, cd):
    """Live price-action chart: price line + 'beat' (open) reference + countdown."""
    dur=(cd or {}).get("dur","5m"); _win=(cd or {}).get("window","")
    ttl=Text(f"{coin} · {dur}"+(f" · {_win}" if _win else "")+" — price action",style=COIN_CLR.get(coin,VAL))
    if not cd or not cd.get("hist") or cd.get("open") is None or len(cd["hist"])<2:
        return Panel(Align.center(Text("gathering price action…",style=DIM)),
                     title=ttl,border_style=BORDER,height=13)
    hist=cd["hist"][-54:]; open_px=cd["open"]; now=cd.get("now") or hist[-1]
    secs=max(0,cd.get("secs_left",0))
    lo=min(min(hist),open_px); hi=max(max(hist),open_px)
    if hi<=lo: hi=lo+max(1.0,abs(lo)*1e-4)
    H,W=9,len(hist)
    def row_of(p): return max(0,min(H-1,int(round((hi-p)/(hi-lo)*(H-1)))))
    tr=row_of(open_px); pr=[row_of(p) for p in hist]
    up = now>=open_px; lstyle = UP if up else DOWN
    mm,ss=divmod(int(secs),60)
    hdr=Text()
    hdr.append(f"beat ${open_px:,.2f}   ",style=DIM)
    hdr.append(f"now ${now:,.2f}   ",style=lstyle)
    hdr.append(f"⏱ {mm:02d}:{ss:02d}",style=HOT)
    lines=[hdr]
    for r in range(H):
        t=Text()
        for j in range(W):
            if pr[j]==r: t.append("●",style=lstyle)
            elif r==tr:  t.append("┄",style="grey50")
            else:        t.append(" ")
        # right-edge axis labels
        if r==0:        t.append(f"  ${hi:,.0f}",style=DIM)
        elif r==tr:     t.append("  ◄ beat",style="grey70")
        elif r==H-1:    t.append(f"  ${lo:,.0f}",style=DIM)
        lines.append(t)
    return Panel(Group(*lines),title=ttl,
                 border_style=lstyle if up else BORDER,height=13)

# ── open / record / per-coin ─────────────────────────────────────────────────
def slug_dur(slug):
    try: return (slug or "").split("-")[-2]
    except Exception: return "?"

def open_panel():
    rows=q("SELECT coin,outcome,entry_price,size_usdc,shares,move_bps,close_ts,slug "
           "FROM positions WHERE status='open' ORDER BY close_ts DESC LIMIT 8")
    if not rows:
        return Panel(Align.center(Text("no open snipes — flat",style=DIM)),
                     title=title("OPEN SNIPES"),border_style=BORDER)
    t=Table(expand=True,box=None,pad_edge=False)
    for c,j in [("coin","left"),("window","center"),("side","center"),("stake","right"),
                ("entry","right"),("pay","right"),("if win","right"),("resolves","right")]:
        t.add_column(Text(c,style=LBL),justify=j)
    nowt=time.time()
    for coin,side,entry,size,sh,mv,close_ts,slug in rows:
        left=int((close_ts or nowt)+120-nowt)
        ifwin=(sh-size) if (sh and size) else 0
        t.add_row(coin_txt(coin), Text(f"{slug_dur(slug)} {slug_window(slug)}".strip(),style=ACCENT),
                  Text(f"▲ {side}" if side=="Up" else f"▼ {side}",style=UP if side=="Up" else DOWN),
                  Text(f"${size:.2f}",style=HOT), Text(f"{entry:.3f}",style=VAL),
                  Text(f"{1/entry:.1f}x" if entry else "-",style=ACCENT),
                  Text(f"+${ifwin:.2f}",style=WIN),
                  Text(f"~{left}s" if left>0 else "settling",style=VAL))
    return Panel(t,title=title("OPEN SNIPES  (stake in / profit if it wins)"),border_style=BORDER)

def record_panel():
    rows=q("SELECT substr(ts,12,8),coin,outcome,entry_price,size_usdc,pnl,status,slug "
           "FROM positions WHERE status IN('won','lost') ORDER BY id DESC LIMIT 12")
    if not rows:
        return Panel(Align.center(Text("no resolved snipes yet — first results coming…",style=DIM)),
                     title=title("TRACK RECORD"),border_style=BORDER)
    t=Table(expand=True,box=None,pad_edge=False)
    for c,j in [("time","left"),("coin","left"),("window","center"),("side","center"),("stake","right"),
                ("entry","right"),("result","center"),("pnl","right")]:
        t.add_column(Text(c,style=LBL),justify=j)
    for tm,coin,side,entry,size,pnl,status,slug in rows:
        res=Text("✔ WIN",style=WIN) if status=="won" else Text("✘ LOSS",style=LOSS)
        t.add_row(Text(tm,style=DIM),coin_txt(coin),Text(f"{slug_dur(slug)} {slug_window(slug)}".strip(),style=ACCENT),
                  Text("▲ "+side if side=="Up" else "▼ "+side,style=UP if side=="Up" else DOWN),
                  Text(f"${size:.2f}",style=VAL),Text(f"{entry:.3f}",style=VAL),res,money(pnl))
    return Panel(t,title=title("TRACK RECORD  (resolved)"),border_style=BORDER)

def percoin_panel():
    rows=q("SELECT coin,COUNT(*),COALESCE(SUM(status='won'),0),COALESCE(SUM(pnl),0) "
           "FROM positions WHERE status IN('won','lost') GROUP BY coin")
    t=Table.grid(padding=(0,2))
    for _ in range(4): t.add_column()
    t.add_row(Text("coin",style=LBL),Text("snipes",style=LBL),Text("win%",style=LBL),Text("pnl",style=LBL))
    if not rows: t.add_row(Text("—",style=DIM),"","","")
    for coin,n,won,pnl in rows:
        t.add_row(coin_txt(coin),Text(str(n),style=VAL),
                  Text(f"{(won/n*100) if n else 0:.0f}%",style=ACCENT),money(pnl))
    return Panel(t,title=title("PER-COIN"),border_style=BORDER)

def render():
    st=load_state(); lay=Layout()
    cfg=(st or {}).get("cfg",{}); hunt=(st or {}).get("hunt",[]); charts=(st or {}).get("charts",{})
    regime=(st or {}).get("regime",{})
    # one ROW per (coin, duration) present — so you see both 5m and 15m timers
    order=[]
    for dl in ("5m","15m","1h"):
        for c in ("BTC","ETH","SOL","XRP","BNB"):
            key=f"{c}_{dl}"
            if key in charts: order.append((c,dl,key))
    if not order: order=[("BTC","5m",None),("ETH","5m",None)]
    order=order[:4]
    # build a [card | chart] unit per (coin,dur)
    units=[]
    for c,dl,key in order:
        cand=[h for h in hunt if h["coin"]==c and h.get("dur")==dl]
        h=min(cand,key=lambda x:(x["secs_left"], x.get("ask") is None)) if cand else None
        u=Layout(name=f"u_{c}_{dl}")
        u.split_row(Layout(coin_card(c,h,cfg,regime),ratio=2),
                    Layout(price_chart(c,charts.get(key)),ratio=3))
        units.append(u)
    # arrange units in a 2-per-row grid (max 2 rows tall, so it always fits)
    grid_rows=[]
    for i in range(0,len(units),2):
        pair=units[i:i+2]
        gr=Layout(name=f"grow{i}")
        if len(pair)==1: gr.update(pair[0])
        else: gr.split_row(*pair)
        grid_rows.append(gr)
    nrows=max(1,len(grid_rows))
    lay.split_column(Layout(header(st),size=3),Layout(name="mid",size=11),
                     Layout(name="hunt",size=13*nrows),Layout(name="low"))
    lay["mid"].split_row(Layout(wallet_panel(st),ratio=2),Layout(feed_panel(st),ratio=2),
                         Layout(open_panel(),ratio=5))
    lay["hunt"].split_column(*grid_rows)
    lay["low"].split_row(Layout(record_panel(),ratio=3),Layout(percoin_panel(),ratio=1))
    return lay

def main():
    with Live(render(),console=console,refresh_per_second=2,screen=True) as live:
        while True:
            time.sleep(0.5); live.update(render())

if __name__=="__main__":
    try: main()
    except KeyboardInterrupt: console.print("\n[bright_cyan]dashboard closed[/]")
