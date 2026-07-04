# GHOST PREDATOR — Claude Code Guide (main reference for this folder)

Last-window latency snipe on Polymarket BTC/ETH/SOL/XRP up-down markets (5m + 15m).
In the final seconds the underlying has usually decided the outcome; we compute the
leader from our own Binance feed and snipe PM's stale ask before it reprices.

Root repo guide: `../CLAUDE.md` (PM2 names, VPS deploy, ops commands). This file is
the authoritative config + data reference for the predator itself.

## Processes (PM2 on VPS)

| Process | File | Role |
|---|---|---|
| `ghost-predator` | `ghost_predator.py` | main snipe engine (Binance WSS + PM book WSS + snipe loop) |
| `ghost-predator-resolver` | `resolver.py` | on-chain token-level settlement (coin-agnostic) |
| `ghost-predator-alarm` | `alarmghost.py` | Telegram health alarm |
| `ghost-predator-radar` | `divergence_scan.py` | read-only PM-vs-spot divergence collector |
| `ghost-predator-firstmover` | `firstmover.py` | GO/WAIT/OUT edge signal from radar DB |
| `ghost-predator-shadow` | `ghost_predator_shadow.py` | paper-only collector, BTC/ETH/XRP/SOL → `shadow_predator.db` |

## Config model — everything is env-driven, NOTHING per-coin is hardcoded

`COINS=BTC,ETH,SOL,XRP` — every coin listed gets its own `{COIN}_*` override keys.
**Inheritance rule: absent or EMPTY per-coin key = inherit the global value; a SET
per-coin key fully REPLACES the global one for that coin.**

Per-coin keys (see `.env.example` for data-driven presets):

| Key | Meaning |
|---|---|
| `{C}_MIN_MOVE_BPS` | lower band edge — leader must move at least this |
| `{C}_MAX_MOVE_BPS` | upper band edge — skip giant moves PM already repriced (0 = off) |
| `{C}_SKIP_5M` / `{C}_SKIP_15M` | duration kill-switch per coin |
| `{C}_GOLDEN_HOURS` | UTC hours that trade the GOLDEN (biggest) ladder |
| `{C}_BLOCKED_HOURS` | UTC hours that trade the BLOCKED (minimal) ladder |
| `{C}_ALLOWED_HOURS` | HARD whitelist — if set, coin trades ONLY these hours |
| `{C}_BASE_SIZE` / `{C}_MAX_PER_MARKET` | stake/exposure when LADDER_ENABLED=false |
| `{C}_5M_LADDER_STEPS` / `{C}_15M_LADDER_STEPS` | classic ladder (normal hours) |
| `{C}_5M_GOLDEN_LADDER_STEPS` / `{C}_15M_GOLDEN_LADDER_STEPS` | golden-hour ladder |
| `{C}_5M_BLOCKED_LADDER_STEPS` / `{C}_15M_BLOCKED_LADDER_STEPS` | blocked-hour ladder |

### Hour tiers (UTC) — three classes, each with its own ladder

- **GOLDEN** → biggest ladder. Fallback chain: `{C}_{DUR}_GOLDEN_LADDER_STEPS` → `GOLDEN_LADDER_STEPS` → classic ladder.
- **normal** (not golden, not blocked) → classic ladder.
- **BLOCKED** → minimal ladder, default `1,2,3` — still trades (cheap data collection),
  it does NOT skip. Fallback: `{C}_{DUR}_BLOCKED_LADDER_STEPS` → `BLOCKED_LADDER_STEPS` → `1,2,3`.
  Only when `LADDER_ENABLED=false` do blocked hours hard-skip (no ladder to shrink).
- GOLDEN wins if an hour appears in both golden and blocked.
- `ALLOWED_HOURS` is separate and HARD: outside it the coin never fires at all.
- Ladder win/loss step position is SHARED across tiers (per coin×duration, from DB
  history): crossing into another tier changes the sizes, not the step index.

## Data findings (shadow DB 2026-06-05..25, n=5201 resolved; edge = WR − avg entry)

**Per coin (would_fill=1):** BTC 5m **+4.0%** (n=436), BTC 15m **+16.3%** (n=45);
XRP 5m **+2.6%** (n=515), XRP 15m **+2.4%** (n=68); ETH 5m +1.6%, 15m −0.9%;
SOL 5m **−1.4%** (n=570) — SOL is only +EV inside golden hours / golden band.

**Golden hours (UTC):** BTC 8,11,13-16,20 (16h = 95.6% WR) · ETH 4,8,11,17 ·
SOL 4,5,6,14,21 · XRP 10,11,13,15,18,20,23 (20h edge +17%).
**Toxic hours:** BTC 0,7,17,19,22,23 · ETH 1,12,18,20,23 · SOL 1,3,7,12,15,17 ·
XRP 0,1,5,8,12,22. Everything is worse on weekends (BTC weekday +3.4% vs weekend −0.3%).

**Golden bps bands (|move| at fire, 5m):** BTC 1.0–2.5 golden, **6+ toxic (−17%)** →
`BTC_MAX_MOVE_BPS=6`. SOL 1.0–1.5 and 3–4 golden, **4–6 toxic (−14.3%)** →
`SOL_MAX_MOVE_BPS=4`. XRP 1.5–2.0 and **4–6 golden (+12.5%)** → no cap for XRP.
ETH has no clean band.

Caveats: shadow config was ask 0.65–0.78, $20 flat, window 28s/20s; per-hour n≈35–75
(trust threshold n≥40); re-validate after every ~2 weeks of new data.

## Rules

- **Paper mode is sacred** — never flip `PAPER_TRADE` without explicit owner instruction.
- Deploy via GitHub only (commit+push local → `git pull` on VPS → `pm2 restart ghost-predator`).
- `.env` lives only on the VPS (gitignored); processes read it at start — restart after edits.
- Never call a change done without proof: local paper run or `pm2 logs` after restart.
