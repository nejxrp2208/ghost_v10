# [K] Weekly Audit — Week 1 (2026-06-07 → 06-14)

*Ghost Predator · BTC/ETH up-down 5m & 15m latency-snipe · own-book, all numbers verified from the positions DB on 2026-06-14. signature_type=1.*

## TL;DR
First full operational week. Raw book: **499 resolved, 70.7% WR, net −$41.84** — but that headline misleads. **Almost the entire dollar loss is the pre-test big-stake era**, peaking Wed 06-10 (−$45.87) on a $5–15 ladder fired into chop with no daily stop. Since the **$1-flat survival test** went live **06-11 05:03Z**, daily P&L is ~breakeven (Thu −$2.03, Fri −$2.01, Sat +$1.56, Sun −$0.85), and the **current live filter stack converts the book to 75.3% WR / +$104.87** (n=231). The strategy is +EV; the week's red is retired sizing.

## Performance (verified, own-book)
| Day (ET) | W–L | Net |
|---|---|---|
| Sun 06-07 | 9–2 | +$1.38 |
| Mon 06-08 | 84–28 | +$19.01 |
| Tue 06-09 | 95–36 | −$13.03 |
| **Wed 06-10** | 75–35 | **−$45.87** ← big-stake ladder into chop (the whole week's loss) |
| Thu 06-11 | 36–18 | −$2.03 ← $1-flat test live |
| Fri 06-12 | 18–10 | −$2.01 |
| Sat 06-13 | 21–10 | +$1.56 |
| Sun 06-14 | 15–7 | −$0.85 |

- **15m is the gem: 81.4% WR, +$101.51 (n=70). 5m carries the entire bleed: 69.0% WR, −$143.35 (n=429).**
- BTC 71.6% / +$10.87 (n=275) vs ETH 69.6% / −$52.71 (n=224).
- **Live-config-passing subset: 75.3% WR, +$104.87 (n=231)** — the go-forward book.

## Config-disclosure timeline (segment pooled analyses here)
**Key boundary: `06-11 05:03Z` = $1-flat test.** Do NOT pool dollar magnitudes across it — everything before is $5–15 stakes, everything after is $1 flat.

| When (UTC) | Change |
|---|---|
| 06-11 05:03 | **$1-flat survival test**: band 0.55–0.78 (skip 0.60–0.65), `MIN_BOOK_DEPTH=100`, flat $1, `MAX_LOSS_STREAK=5` |
| 06-11 14:46 | fire window `SNIPE_WINDOW 30→14, MIN_FILL 2→7` (7–14s, copied from B) |
| 06-11 16:40 | `MOVE_EXCLUDE=2.5-4` added; `BLOCKED_HOURS` emptied (trade all hours) |
| 06-12 01:05 | **fire window REVERTED to 30/2** (per J's per-book inversion warning) |
| 06-12 02:28–02:35 | `MIN_BOOK_DEPTH→300`, `MOVE_EXCLUDE→2.5-6`, `ETH_5M_BLOCKED_HOURS=16-23` |
| 06-12 13:53 | **bps OPEN** (`MOVE_EXCLUDE` off), `MIN_BOOK_DEPTH→100`, `BLOCKED_HOURS` restored = "clock+depth" config |
| 06-14 (code) | weekend min-move boundary moved Thu-23:00 → **Fri-22:00 ET**; **`HUNT_LOG`** added (1/sec snipe-window leader snapshots → fire-timing replay) |

**Current live:** bps open · depth ≥$100 · dead hours blocked ET {2,3,11,12,13,19,20,21} · ETH-5m blocked ET 16–23 · band 0.55–0.78 skip 0.60–0.65 · fire ~29s · $1 flat · 5-loss halt · weekend min-move 2.5 from Fri 22:00 ET.

## Key findings (own-book evidence)
1. **Loss anatomy (Binance ground-truth, n=145 losses):** ~46% are **razor coin-flips** (<2bps final margin = irreducible basis noise — accept the floor); ~54% are genuine reversals concentrated in **ETH-5m + thin books**. A bigger lead at fire-time gives **zero** protection (≥4bps fires reversed 55% — same as a coin flip). Oracle/Chainlink disagreement <2% — a non-issue, don't build for it.
2. **The toxic super-cell:** `5m × fired-early(25-31s) × move 2.5-4bps` = **59.3% WR, −$149, 24% of volume / 34.5% of all loss-$**. It's **multiplicative, not additive** — each leg alone bumps base loss-rate only 1.06–1.13×; stacked, 1.40×. Single-dimension cuts structurally cannot see it.
3. **A blunt `MOVE_EXCLUDE` is the WRONG fix.** 2.5-4bps is harmless-to-good outside that conjunction (84.6% WR at <10s; 88.9% on 15m). The residual bleed is a surgical cell — **BTC + entry≥0.70 + move 2.5-4bps** (BTC 56% WR vs ETH 82% in the *same* cell) — modeled +$56 over live by removing fewer winners than a band exclude. Matches J's "flat exclude may cut winners" caveat.
4. **Fire-timing is per-book and a FILL lever, not a WIN lever** — confirms J's inversion. We copied B's 7–14s blind (couldn't validate; our fires had no secs_left spread), J flagged 7–14 as *his* worst, we reverted to ~29s. Ground-truth: early vs late fires reverse at the same rate; "25-31s = 73% of loss-$" is **volume, not mechanism**. **Deployed hunt-logging** (1/sec leader snapshots: secs_left, move, best_ask, depth) to do a proper own-book replay — settles the window question in ~2 days. *Recommend every member bucket their own secs_left before touching their window.*
5. **Depth gate — 3-book agreement** that sub-$100 in-band books bleed (gate-worthy everywhere). Our full-book ledger argues a **$300** threshold (our $100–300 band = −$0.52/t, opposite of J's +$0.79) — threshold is per-book, the floor is structural.
6. **Exhaustion-ratio** (`|pre_window_move|/|move|`): fresh-vs-stale signal — a 2bp move wins 77% when fresh, 66% when it's the tail of a spent pre-window run. Real but **minor** on the live $1 book (+$2 swing); the big numbers were legacy-era inflation. Worth a look on other books.

## Operational scars (for the team)
1. **STALE-DASH BALANCE (verify-from-DB, learned the hard way):** the web dash froze on a stale cash value (showed $97) while the real on-chain balance was **$9.56** — that false all-clear let the ladder quietly drain the account overnight. **Never trust a dashboard banner for balance; verify from chain/DB.**
2. **A flat move-band exclude can STARVE the book.** `MOVE_EXCLUDE=2.5-6` + the 0.78 ask cap left only 1.5–2.5bps leads → ~1 fire / 10 hours; the profitable morning never traded. Gate on *conjunctions*, not blunt move-bands.
3. **Windows auto-pull gotcha:** a scheduled `git pull` task was silently failing — Windows refuses the trigger on battery ("only on AC power" default → error 0x800710E0), so the team-sync never ran unplugged. If any teammate runs a Windows auto-pull on a laptop: uncheck the battery condition, and launch via a hidden VBS wrapper so it doesn't flash a console every run.

## Open / next
- **Hunt-log replay (~06-16)** → set the snipe window on data, not the confounded observational buckets.
- **BTC entry×move intersection** = next deploy candidate (+$56 modeled).
- The "winning then reverses at the close" pattern is fundamentally an **exit** problem (settlement on the closing print), not entry timing — watching for B's exit kill-switch (June 18).
