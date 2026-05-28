# GHOST PREDATOR — Learnings (toward live)

Running log of what makes this strategy work, built from live paper data. Goal:
collect across regimes, fine-tune, decide if/when to fund real money.

## Config tripwire (AUTHORIZED baseline)
`.env` mtime baselines (separate files per machine): **VPS = 2026-05-25 03:06:00 UTC** (BASELINE
arm, T-12s), **Mac = 2026-05-25 12:39:48 (Mac local)** (EXPERIMENT arm, T-30s — intentionally
diverges from VPS + the share folder; week-long live A/B vs VPS T-12s started 2026-05-25). They must change ONLY on an authorized edit logged here —
if either changes with no entry below, that's a stealth change → investigate. NOTE: the tripwire
watches `.env` mtime only, NOT `ghost_predator.py` — a teammate's *code* change wouldn't trip it
(see 2026-05-24 teammate "Option 3" deploy that hit a DIFFERENT VPS, not ours).
Current live config: USE_WSS_BOOK=true, BOOK_POLL_MS=10, **WALK_FILL=true**, DYNAMIC_SIZE=true
(superseded by WALK_FILL), MIN_STAKE=1.0, MIN_MOVE_BPS=1.0, **MIN_ASK=0.65**, MAX_ASK=0.85,
BASE_SIZE=30, DURATIONS=5m,15m.
Authorized config changes:
- 2026-05-23 05:43Z — MIN_ASK 0.50→0.65 (cut the coinflip band: 0.50-0.65 won only 55%,
  needs ~57% to break even, net -$0.40/trade. Keep 0.65-0.78 @80% & 0.78-0.86 @95%/+$3.80/trade).
- 2026-05-25 03:06Z — **WALK_FILL=true added; strategy shifted to consistent $30 fills**
  (owner directive, both Mac+VPS). Bot now WALKS the ask ladder to fill the full BASE_SIZE ($30)
  within MAX_ASK at a blended avg entry, and fires ONLY if the in-band book supplies the full $30
  ("$30 or skip"). Rationale (owner): (1) it's a PvP market, not us-vs-house — paper "zero-miss"
  evidence is blind to races since paper never submits a real order; (2) the tiny thin-book wins
  (e.g. +$1.70) are NOT real — live slippage+fees eat them; the durable edge is decisive-move $30
  fills; (3) at ~84% WR a $30 fill EVs ~+$4.4/trade vs the old +$0.33. SUPERSEDES the earlier
  "keep a clean depth-matched baseline / don't ship walk-up" stance. New `walk_book_fill()` helper
  + snipe_loop branch + `fire()` records the blended avg as entry_price so paper PnL mirrors live.
  Backup of pre-change bot: `ghost_predator.py.bak_20260525` on VPS. Verified: walk logic unit-tested
  ($11.60 top-of-book → $30 @ blended 0.583; thin <$30 in-band → skip).
- 2026-05-25 12:30 — **SNIPE_WINDOW experiment: Mac 5m window 12→60 (T-60s), VPS stays 12 (T-12s)**
  (owner approved). Testing a teammate's 30-day/23k-market backtest: per-fire ROI is best at T-12s
  (+16.6%) but total $ peaks ~T-60s because asks are available ~6× more often (avail 5.6% @T-12s vs
  ~33% @T-60s) at only slightly lower WR (~97%→92.5%). Mac = experiment arm, VPS = baseline (separate
  DBs = clean-ish A/B; Mac differs only in window). MY CAVEATS on the teammate's numbers: (a) the
  availability/$ projections come from 18 markets on ONE Saturday evening (dead tape) — could be ±50%
  off and unrepresentative of weekday/peak; (b) availability ≠ LIVE fillability, and it's WORSE earlier
  (less-settled market = more makers/takers contesting the ask) — the backtest can't see races, so the
  wider window's paper number flatters it most; (c) earlier = deeper/more-contested books = the adverse-
  selected $30 fills our loss analysis flagged. So: real hypothesis, worth testing in paper, NOT a
  config flip on one Saturday. CAVEAT on the Mac arm: laptop sleeps → data gaps; a VPS twin would give
  cleaner 24/7 A/B (offered). Revert Mac to 12 to rejoin baseline. Also re-running our own
  snipe_backtest.py (3-day lag_data.db) across the same windows as an independent persistence-curve check.
- 2026-05-25 ~12:45 — **RESULT: teammate's window HEADLINE REFUTED (independent backtest, n=2,416, GROSS).**
  Persistence SHAPE confirmed (94.9% @T-12s -> 53% @T-300s = coinflip at open). But under our LIVE band
  (0.65-0.85), widening does NOT pay: fires rise only ~1.7x (T-12s 433 -> T-60s 720), NOT the 6x claimed;
  total gross profit is ~flat T-12s->T-30s (+$111 / +$116) then declines (T-60s +$99, T-180s +$32). After
  fees (which hit wider/lower-ask windows harder) T-12s ~= T-30s and T-60s is clearly worse. The "T-60s
  peak / 6x fires / much more $" was an artifact of their 18-market Saturday availability sample —
  persistence real, conclusion not. VERDICT: keep T-12s baseline; T-30s at most a wash; T-60s+ worse.
  ACTION: recommend switching Mac experiment T-60->T-30 (only window w/ marginal support) or revert to T-12.
- 2026-05-25 12:39 — owner chose the live check: **Mac switched T-60->T-30s**, 1-week live A/B vs VPS T-12s.
  Bar to beat: T-30 must out-net T-12 AFTER fees AND after live fill-races (which hit the wider window
  harder) — backtest says it's ~a wash gross, so the realistic expectation is it confirms T-12s. Compare
  fires/day, WR, per-fire, and total on each box's separate DB at end of week.

## Core findings (as of 2026-05-23 ~04:30 UTC)
1. **Edge scales with move decisiveness** (all-trades, fee-adj, dyn-size):
   coinflip <2bps +$0.31/trade · decisive 2-3bps +$1.29 · strong 3+bps +$2.12. ~7×.
2. **Dead market = 60% coinflips** (median move 1.6bps). This is the WORST regime for
   the strategy. Active/peak hours should shift the mix toward decisive moves.
3. **Polymarket settles on CHAINLINK** (ETH/USD & BTC/USD streams), not Binance.
   On near-flat closes (sub-2bps) the feeds can disagree → loss even when "right" on
   Binance (this was the #53 loss). Ties (≥open) resolve UP.
4. **Taker fee = shares × 0.07 × p × (1-p)** — small at favorites (our band), ~$0.3-0.8/trade.
5. **Do NOT raise MIN_MOVE_BPS** — verified: it strips profitable small-move wins while
   the (deep-book) losses sit at higher moves. Backfires.
6. **Dynamic sizing** (size to depth) → ~100% fill + flips the modeled fee-adj net
   positive (+5.2%) by capturing thin high-win books at small size. Live capture
   depends on winning the taker race for thin depth (→ speed: WSS + colocation).
7. **The 3 fillable losses are 3 different mechanisms** (late reversal #33, feed
   disagreement #53, whipsaw #56) — no single fixable flaw; coinflip-tail variance.
8. **Adverse selection in liquidity**: thin books correlate with HIGH win rate (book
   swept = outcome obvious); the deep books you can fill at size are the coinflips.

## Baseline (dead 4am tape, 16-trade fillable sample)
fillable net after fees: ~-$13 to -$24 | coinflip +$2.94/trade (n=10, 90%) |
decisive/strong buckets still tiny (n=2-4), each dominated by one -$30 loss = noise.
**Open question:** do peak hours grow the decisive/strong buckets to n≥10+ AND turn
their $/trade solidly positive after fees? That decides whether it's live-worthy.

## Infrastructure (24/7)
- **Watchdog** (VPS cron, every minute under flock): restarts ghost_predator.py /
  resolver.py / alarmghost.py ONLY if dead (counts real python procs; idempotent —
  cannot duplicate; tested kill→revive→repeat = stays 1). Pings Telegram on any restart.
  So a "Watchdog: ... restarted" Telegram = a process died and was auto-revived.
- **ledger_report.py** (VPS cron, daily 23:00 UTC): fee-adjusted regime ledger → Telegram.
- **Hourly monitor** (this session, cron :07): health + ledger + learnings.
- **Mac launchd KeepAlive** (local instance only, 2026-05-24): `com.ghostpredator.bot`
  + `com.ghostpredator.resolver` in `~/Library/LaunchAgents/` — auto-revive bot/resolver
  on exit incl. laptop wake-from-sleep (clean death w/ no traceback = macOS killed it on
  sleep, the observed failure mode), and auto-start on login/reboot. GOTCHA: launchd can't
  create log files inside `~/Desktop` (TCC-protected from background services → EX_CONFIG 78);
  logs go to `/tmp/gp_bot.log` & `/tmp/gp_resolver.log` (the bot itself reads/writes Desktop
  fine — only launchd's own log-file creation is blocked). Stop via `launchctl unload`.
  Mac is a SECONDARY viewer (laptop sleeps ≠ true 24/7; VPS is the canonical collector w/ its
  own dataset). LESSON: scope process kills to Predator's ABSOLUTE path
  (`/Desktop/ghost_predator/resolver.py`) — a bare `pkill -f resolver.py` also matches Ghost
  Lattice's `crypto_ghost_resolver.py` and must NEVER touch Lattice.

## Latency / execution (measured 2026-05-23)
- CLOB (clob.polymarket.com) is behind **Cloudflare** (anycast). Vultr-London VPS:
  **ping ~1.9ms** to the edge — already the network floor. **Colocation to AWS eu-west-2
  would save ~0ms** on the network *path* (same Cloudflare edge).
  **2026-05-25 owner reframe:** this is a **PvP** market and paper is BLIND to races — we never
  submit a real order, so we "win" every paper fill by definition; "zero-miss in the logs"
  proves nothing about live. So "network maxed" only bounds the *transport*, not whether we lose
  entry races live. Colocation + pre-signing are back ON the table as live race-win levers;
  the colocation migration is deferred until we go live small and actually MEASURE real misses.
- Order round-trip: **warm ~10-15ms** (reused TLS) vs **cold ~40ms** (~27ms is the TLS
  handshake). The ~10ms server-side floor (Cloudflare+matching) is fixed for everyone.
- **Only order-latency lever = keep the TLS connection WARM.** Implemented `prewarm_loop()`
  (LIVE only): pings the CLOB via the order client every 5s while any market is <75s from
  close, so live FOKs fire warm (~10ms) not cold (~40ms). Paper no-op. VERIFY
  get_server_time() method name when going live (wrapped safe).
- **Pre-signed fire path** — `presign_loop()`, LIVE only, built 2026-05-25: builds & SIGNS the
  $30 FOK (amount=BASE_SIZE, price=MAX_ASK — params CONSTANT per token, so the signature never
  goes stale on ask drift) during the snipe window; `fire()` then only POSTs it, shaving the
  ~30-50ms EIP-712 signing (the single slowest fire-path step) off the trigger. Paper no-op;
  `fire()` falls back to inline signing if no cached order → can never break a fire. VERIFY when
  live: create_market_order() returns a reusable signed order, and a few-sec-old FOK salt/nonce
  is still accepted by the CLOB. UNTESTED until live (paper never submits).
- No-fill depth: ~38-83% of books hold >=$30 in-band depending on band. Liquidity is INVERSELY
  related to certainty (deep books = more contested). Earlier call "dynamic sizing, not forcing
  $30" is **SUPERSEDED 2026-05-25 by WALK_FILL** (owner directive): force the full $30 via
  book-walk within MAX_ASK, skip if unfillable. Rationale: thin-book small wins (e.g. +$1.70)
  don't survive live slippage/fees, and at ~84% WR consistent $30 fills EV far higher (~+$4.4 vs
  +$0.33/trade). We'll measure whether the WR holds at $30 as the new fires accumulate.

## Hourly observations
- 2026-05-23 04:30Z — baseline set. Dead overnight tape. #61 (-7.2bps strong) WON +$1.50.
- 2026-05-23 04:36Z — watchdog deployed + tested (kill→revive→no-dup). 24/7 crash-resilience on.
- 2026-05-23 05:09Z — fillable net climbing -$24→-$8.61 as coinflip wins stack (+$2.96/trade,
  91%, n=11). decisive/strong STILL n=3/4 = noise (dead overnight tape, no decisive moves).
  #62 #63 won; #64 (BTC Down @0.51, -3.0bps — decisive move priced at the floor = textbook
  mispricing) pending. Network confirmed maxed; 1ms order placement impossible (Cloudflare+
  matching server-side ~15ms floor for everyone). Speed work DONE.
