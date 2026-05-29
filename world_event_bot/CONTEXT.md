# Politicas — Workspace Router (L1)

> Layer 1 of the ICM hierarchy. Read on entry. Answers: **"Where do I go?"**

This file tells you which stage to enter and what to load. It does **not** contain the work itself — that lives in each stage's `CONTEXT.md`.

---

## Stage progression

| # | Stage | When to enter | Output |
|---|-------|---------------|--------|
| 01 | `audit` | "Check the bot" / "is anything broken" / weekly health check | Findings list in `stages/01-audit/output/` |
| 02 | `investigate` | A finding needs deeper proof (data, repro, scope) | Investigation note in `stages/02-investigate/output/` |
| 03 | `propose` | Investigation done, ready to recommend a change | Proposal with WHY in `stages/03-propose/output/` |
| 04 | `implement` | User approved a proposal | Code diff + summary in `stages/04-implement/output/` |
| 05 | `verify` | Implementation finished, prove it works | Verification log in `stages/05-verify/output/` |
| 06 | `extract-rules` | New lessons added to `skills/lesson-plans/` and marked `reviewed` | Rule-extraction report in `stages/06-extract-rules/output/` — feeds stage 03 |

**One stage per invocation.** Don't combine audit with implement, or investigate with propose. If the work crosses stages, finish stage N's output file first, then enter stage N+1.

---

## What to load (global)

| Resource | When | Why |
|----------|------|-----|
| `CLAUDE.md` (L0) | Always | Workspace identity + workflow rules |
| `stages/<current>/CONTEXT.md` | Always at stage entry | The contract for what you're doing |
| `_config/signal-doctrine.md` | Any change to [signal_engine.py](signal_engine.py) or thresholds | The "why" behind every tier |
| `_config/api-contracts.md` | Any change to [gamma_client.py](gamma_client.py) or [clob_client.py](clob_client.py) | Endpoint + response shapes |
| `_config/db-schema.md` | Any change to [database.py](database.py) or new query | Table columns + invariants |
| `_config/filters.md` | Changing target/exclude keyword logic | Why each keyword is on the list |
| `shared/run-instructions.md` | Entering `05-verify` | How to actually launch the bot |
| `skills/lesson-plans/INDEX.md` | Entering `06-extract-rules` | Know which lessons are ready to distill |
| `skills/lesson-plans/NN-*.md` | Entering `06-extract-rules` | The Concrete Rules section is the actual input |
| `tasks/todo.md` | Session start | Current plan, in-flight items |
| `tasks/lessons.md` | Session start | Past corrections — read before repeating mistakes |

## What NOT to load

| Resource | Why |
|----------|-----|
| All `.py` files at once | Read only the files the stage names. Selective routing is the point of ICM. |
| `polymarket_event_bot.db` | Query through [database.py](database.py); never crack the SQLite file open directly. |
| `.venv/` | Build artifact, never relevant to a task. |
| Old `stages/*/output/` from prior runs | Unless the current stage explicitly inherits a prior artifact, treat past outputs as archive. |

---

## Quick-reference: which file controls what

| If you're touching… | The contract lives in… |
|---------------------|------------------------|
| Trading thresholds (midpoint, spread, quality) | [_config/signal-doctrine.md](_config/signal-doctrine.md) |
| Polymarket API calls | [_config/api-contracts.md](_config/api-contracts.md) |
| SQLite schema or queries | [_config/db-schema.md](_config/db-schema.md) |
| Target / exclude keywords | [_config/filters.md](_config/filters.md) |
| Streamlit UI | [app.py](app.py) directly — no shared rules yet |
| Running / debugging the bot | [shared/run-instructions.md](shared/run-instructions.md) |
