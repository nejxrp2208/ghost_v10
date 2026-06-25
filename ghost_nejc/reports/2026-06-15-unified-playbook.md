# Unified Team Playbook — cross-analysis of all 4 audits (B / J / K / M)

*Compiled 2026-06-15 by B's session. Method: each member's audit extracted independently, then 10 strategy dimensions cross-tabulated across the four books and **adversarially verified** — every dimension stress-tested for the team's own failure modes (mixed-regime contamination, in-sample/single-week, small-n, per-book confounds masquerading as consensus). Sources: this repo's `CLAUDE.md` LESSONS, `docs/Weekly_Audit_J_2026-06-15.md`, `reports/2026-06-14-K-week1-audit.md`, and B's weekly audit. This is a synthesis for discussion, not a directive — per iron rule 2 every per-book value is a candidate to confirm on your own book.*

## Bottom line

Only a handful of findings are genuinely **team-wide verified**. Most apparent "consensus" is a per-book **shape** with a per-book **value** — and copying another seat's number is the exact failure we keep hitting (fire-timing inverts B↔J; depth $100–300 flips sign; entry-band threshold differs B 0.60 vs J 0.65). The dominant risk across all four audits is the one B already named: **single-week, in-sample, mixed-regime data** — almost every dollar figure spans a config change. The real arbiter for everything contested is the **2026-06-18 frozen-config pooled (ghost-pool) week** — the first clean cross-book sample. Freeze and feed it; do not converge early.

---

## A. Adopt team-wide NOW (verified / discipline — no more data needed)

| Rule | Why it holds |
|---|---|
| **Min-lead / min-move bps FLOOR is REFUTED** — nobody raises a bps bar | Strongest finding in the corpus. Multi-book, multi-method: B (509t + 60-loss anatomy: losers' leads ≈ winners'), K (≥4bps reversed 55%), J (per-coin \|move\| can't separate W/L). A floor cuts the profitable 1.5–2.5 engine and keeps the toxic middle. |
| **Sub-$100 in-band depth FLOOR** (skip phantom books) + re-evaluate depth **per-tick inside the window** | 3-book directional agreement (B/J/K). A thin in-band quote is what the FOK craters into mid-flight. B's first catch: a $4 book that refilled to $201 one second later. Threshold *above* $100 is per-book. |
| **Post-fill SLIP-SIGN detector** — trigger on `(fill − quoted price)`, NOT a price floor | Cleanest team-wide replication: 3 books, **no sign-flip** (B breach record; J >5c down-collapse = his entire weekday bleed; K's 14 of 19 deep breaches filled *inside* the normal 0.55–0.70 band, invisible to any price floor). Build the detector on all 4 books. |
| **Ops rulebook** (mandatory hygiene, not edge) | Verify from DB/chain, never a banner/dash/state.json; every display calls the *same* `eff_` function the entry check uses; compute time-varying config (weekend bar) IN the entry check at fire-time, never via cron/.env (J's cron flip silently failed, −$50); pull+diff the *running* file before patching; restart *outside* the snipe window; plan-mode for any live-money change; segment by config-era timestamp before pooling. |
| **Weekday vs weekend split** in every analysis | Weekend bleed replicates (B −$148; J −$112 vs +$100 weekday). Never trust a pooled weekend number. (Bar *direction* is contested — section C.) |
| **Ladder = stake-sizing ONLY**; walk resolved rows only; size DOWN a bleeder | Iron rule 4. Walk resolved rows only (exclude open + scratched, or stakes climb in losing streaks). The ladder amplifies EV, never creates it — a bleeding book sizes down, never climbs. |

---

## B. Per-book TUNE (shape agreed, value differs — run your own query, deploy ONE change, freeze)

| Lever | Where each seat lands / what to do |
|---|---|
| **Cheap-ask skip threshold** | B `ask<0.60` · J `skip<0.65` (worst band 0.60–0.65) · K keeps a **conjunction** (`move≥2 AND entry<0.72`) · M re-run on a clean sample. Test **jointly with your depth gate** (ask~depth corr −0.53 on J = double-gate risk) and re-validate under current sizing (B found it goes p≈0.42 once you control for ladder rung). |
| **Depth threshold above the $100 floor** | J $100 · B/K's tables argue $300 but on stake-contaminated dollars — re-cut on clean single-regime data before moving off $100. |
| **Move-band bleeder** | J/K = 2.5–4 bps · B = 3–5 (non-monotonic, leaves the band alone). Prefer the **surgical cell** (K's `BTC × entry≥0.70 × 2.5–4`) over a blunt `MOVE_EXCLUDE`, which cuts the 15m 2.5–4 *gem* (~89–92% WR) and starved K's book to ~1 fire/10h. |
| **5m ladder cap** | B caps 5m at $15 · K flat $1 · M runs the full ladder profitably (+$337 all-time), should still watch his $33+ rungs (−$39 in audit). |
| **Coin / duration gates** | No team-wide BTC/ETH-skip or 15m-only rule: K sees a 15m gem / 5m bleed, B's live window makes 5m the engine; J/K call ETH the red coin, M's book has BTC bleeding. The durable driver is the per-book move-band, not the label. |

---

## C. CONTESTED / OPEN — freeze, don't converge, settle on the 06-18 pooled data

| Item | Why it's open | Do before 06-18 |
|---|---|---|
| **Fire-timing window** | B's deployed 7–14s is literally J's *worst* bucket; **0 of 4 books agree** on any window value; seat-coordination confound unresolved. | All 4 deploy 1 Hz HUNT_LOG (B & M can't even bucket their own `secs_left` yet). Hold windows frozen. Don't copy B. Tune `(timing × move-band)` cells, not a 1-D window. |
| **Hours / session** | Powered on **one** book (J: 08–11Z engine, p=0.0005); B's own study says his version was 88–93% dead-old-window contamination. | K runs J's 08–11Z × Mon–Thu query; B re-runs hours on NEW-window-only fills; no team-wide hour gate yet. |
| **Weekend bar DIRECTION** | Genuinely split: B *lowered* it (betting the late window rescues it), J + K *raised* it (J's thin-lead engine inverts negative on weekends). Zero clean replication of either direction. | Each holds current bar; post a clean segmented weekend A/B Monday. Lower only if you *also* moved to the late window *and* your own weekend book is +EV on it. |
| **Auto-scratch SELL (live)** | Executor has n=1 clean live unwind (#851); standing team freeze on auto-sell before 06-18. | Build + **shadow-log** now. Keep B's as the single-box pilot. No live rollout before the bid_log verdict + per-book backtest + sign-off. |

---

## D. The three highest-value actions for the unit (do these first)

1. **Singleton `fcntl.flock` lock on every bot — now.** Marc's verified fix for the double-order bug (~$90 self-inflicted; two processes fired the same window). **B's box has the same gap, confirmed.** J/K must grep their build for the watchdog/pgrep restart race. Zero cost, unbounded downside — the only item that silently double-fires real money.
2. **Give Marc (`marcdang3`) GitHub push access.** He's relaying every finding through B — a disclosure single-point-of-failure that means his book's segmentation can't be trusted into the 06-18 verdict.
3. **Stand up the `ghost-pool` repo** (base-permission NONE, 4 explicit collaborators, branch protection). Everything in section C waits on it; the hardening is the one thing between "4 of us" and "26 competitors see our fire-timing." J's side is built and ready to seed.

## E. Live disagreements that owe a decision
- **J's flat-1.5 weekend override** was deployed against his own data with no backtest — an iron-rule-2 violation he flagged at the time. Re-examine with Monday's weekend buckets.
- **Seat-coordination vs per-book windows** — J's hypothesis is single-sourced (K can't replicate it; his `shadow_positions` is empty) and partly falsified by the move-band sign-flip. Exploratory until the pooled data.

## F. Corrections the verification flagged in the committed playbook
- The `CLAUDE.md` line granting the ask-band **"VERIFIED status"** leans on Marc's self-admitted-**contaminated** n=109 — accurate scope is *cheap-ask-bleed SHAPE verified on 2 clean books + 1 conjunction (K); M pending clean rerun; threshold per-book; lead/bps floor refuted.*
- The **"958 pooled trades"** no-clustering stat is **not** cross-book (ghost-pool isn't live) — it's B's single-book autocorrelation (still a valid test, and J's per-execution replay is the independent 2nd book).
- The old **"entry band is per-book (downgraded 6/11)"** note: J withdrew only the *0.80+ high-end* claim (his 0.80+ band is now empty under MAX_ASK), **not** the per-book threshold — the cheap-bleed shape is real, the threshold is still per-book.

---

**One-unit summary:** lock down the structural fixes (flock lock, Marc's access, ghost-pool) and the verified rules (no bps floor, $100 depth floor, slip-sign detector, ops hygiene) now; tune the per-book values each on your own segmented book; and **freeze** every contested knob (fire window, hours, weekend bar, auto-sell) for the 06-18 frozen-config pooled verdict — the first clean cross-book sample and the only honest arbiter.
