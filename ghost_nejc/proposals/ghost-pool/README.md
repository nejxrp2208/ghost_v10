# Proposal: ghost-pool — a shared cross-book data exchange

**From:** J · **For:** B (org admin) + K + M · **Status:** built & ready, needs B
to create the repo · 2026-06-12

## What it is
A second private repo (`ghost-pool`) where each of us exports our bot's resolved
trades + sensor data daily into our own `data/<initial>/` directory. Git is the
transport. Pooled studies run with DuckDB over `data/*/...` — or by asking Claude.
**It is NOT a leaderboard and NOT a config-copying machine.** Pooled data answers
*mechanism* questions; deploying anything still needs your own-book evidence
(iron rule 2). The playbook (this repo) stays prose-only; raw rows live ONLY in
ghost-pool.

## Why now
We keep hand-pasting numbers into LESSONS and re-cloning each other's DBs. Three
things on our plate actually *need* pooled data:
1. **The 2026-06-18 exit-liquidity verdict** — it's defined as a pooled bid_log
   study across all 4 books. Right now there's no place to pool it.
2. **The 4-book ensemble/disagreement study** — "same window, who picked which
   side, who was right" becomes one SQL join instead of a 4-clone ritual.
3. **The window-collision test** — my kills vs your fires on the *same* market
   event (the clean version of the interdependence hypothesis I posted).

And the bigger idea: we take near-identical trades but each capture *different
sensors* (me: full depth ladders + bid_log + skips; B: hunt_log; K: tape). On a
shared event spine (join key = coin+duration+window_end) that redundancy becomes
**coverage** — a weekend window only one of us fired still gets instrumented.

## The one rule that makes this safe (B, this is on you as org admin)
Pooled raw trade rows are **wallet-equivalent**: public Polygon fills match our
rows in an afternoon, and we sit inside a 26-op group racing the same edge. If
ghost-pool ever inherits org-wide read, 22 competitors get our fire-timing,
ask-band, and kill weak-spots. So: **separate repo, base-permission = none, the
4 of us as explicit collaborators, branch protection on.** Full steps in
`SETUP-org-admin.md` in this folder. (J's note: a personal repo with 3 invited
collaborators removes the org-default failure mode entirely — but the team chose
the org route, so the hardening matters.)

## How each member sets up their side (~½ day, once)
1. After B creates + shares the repo, clone it; make `data/<your initial>/`.
2. Write YOUR exporter **natively** against your own DB — do NOT run J's verbatim
   (team rule: builds differ). `reference_exporter_J.py` here is a READ-only
   reference for the shape; `CONTRACT.md` is the spec. ~80 lines: SELECT real
   fills → map to the canonical columns → one CSV per closed UTC day.
3. Fill `config_eras.csv` from your CONFIG DISCLOSURE history and `meta.yml`
   (series start dates, quirks). Eras are what keep pooled stats from mixing a
   pre/post-change book — critical given how often we each toggle.
4. `python validate_export.py data/<X>` (leak-scans for keys/IPs/wallets, checks
   the contract) → fix until OK → commit + push.
5. Automate daily (cron / Task Scheduler) on your LOCAL machine — never put git
   creds on the VPS; reuse the read-only snapshot pull you already do.

## What's already done (J)
- Contract v1, the leak-scanning validator, two pooled DuckDB studies
  (`scoreboard.sql`, `ensemble_disagreement.sql`), and J's full backfill
  (507 real fills, 9 days, 6 eras) are built and committed.
- The scoreboard already runs and reproduces my known weekend-bleed era from the
  pooled file, so the pipeline is proven end-to-end on one book.

## Files in this folder
- `SETUP-org-admin.md` — B's exact GitHub steps (create + harden + add us).
- `CONTRACT.md` — the export contract v1 (canonical columns, rules, event spine).
- `validate_export.py` — run before every push; fails on any leak or schema break.
- `scoreboard.sql` / `ensemble_disagreement.sql` — the first pooled studies.
- `reference_exporter_J.py` — J's exporter, for reference only (write your own).

**B — when you've created and hardened the repo, ping the channel and we each
build our exporter. Target: first validated 4-book pool before 2026-06-15, so
there's slack before the 06-18 bid_log verdict.**
