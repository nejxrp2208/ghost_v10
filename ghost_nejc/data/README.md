# ghost-predator-data

Single source of truth for the team's **Ghost Predator** strategy configs and trade data.
Built 2026-06-18 to end the "what are X's exact settings?" screenshot hunt.

## What's here
- `configs/` — one file per person (`branden.yaml`, `marcos.yaml`, …). Each owner **corrects their
  own file**; git history is the audit trail of every change. To check someone's exact settings, read
  their file — never a screenshot.
- `configs/_schema.yaml` — the canonical field list: every parameter, its meaning, units, and range.
  **Everyone uses these exact keys.** This is what stops "3.0 vs 1.5 vs 5.8" drift.
- `configs/golden-hour.yaml` — the agreed shared golden-hour profile under test.
- `data/<person>/fills.csv` — exported trade fills (analysis columns only). See `data/SCHEMA.md`.
- `tools/` — `check_no_secrets.sh` (run before every commit) and `validate_config.py`.
- `reports/` — analysis outputs (e.g. golden-hour studies).

## 🔒 HARD RULE — never commit secrets
**No private keys, wallet addresses, API keys/secrets/passphrases, relayer keys, builder codes,
telegram tokens, IPs, tx hashes, or account balances.** Strategy parameters only.
Run `tools/check_no_secrets.sh` before every commit (a `.gitignore` also blocks `.env`, `*.key`, etc.).
Account balances/deposits are personal — they do **not** belong in shared configs.

## Workflow
1. Edit your own `configs/<you>.yaml`. Update `last_verified_by` / `last_verified_date`.
2. Flag anything you're unsure of with `# UNVERIFIED`.
3. `bash tools/check_no_secrets.sh && python3 tools/validate_config.py configs/<you>.yaml`
4. Commit with a message saying what changed and why. The diff is the double-check.

## Status of configs (2026-06-18)
- `branden.yaml` — VERIFIED (pulled from live VPS .env, secrets stripped).
- `golden-hour.yaml` — DRAFT (3 values pending team confirm: max_ask, paroli_ladder; move_bps set to 3.0 per Julian).
- `marcos.yaml`, `julian.yaml`, `kso.yaml`, `blicky.yaml` — TEMPLATES, owners must fill/verify.
