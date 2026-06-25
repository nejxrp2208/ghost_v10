# ghost-pool — one-time hosting setup (Ghinsinc org)

This repo holds **wallet-equivalent** data. Under an org, the danger is access
*creep* — an org default or a new member silently granting read to the wider 26.
Do these in order; the hardening is the whole point of the org route.

## 1. Create the empty repo (you, on github.com)
- New repository → Owner: **Ghinsinc** → Name: **ghost-pool** → **Private** →
  do NOT add README/.gitignore (this folder already has them).

## 2. Lock access to exactly the 4 of you (do BEFORE the first push)
- Org → Settings → Member privileges → **Base permissions = No permission**
  (so org membership alone grants ZERO access to this repo).
- Repo → Settings → Collaborators and teams → add B, K, M individually as
  **Write**. No teams (teams invite creep). You are admin.

## 3. Branch protection (prevents the force-push that already bit team/ once)
- Repo → Settings → Branches → Add rule for `main`:
  - Require linear history ✓
  - Do not allow force pushes ✓ / Do not allow deletions ✓

## 4. First push (J seeds it once you share access)
J already has the full scaffold + a 507-trade backfill committed locally with the
remote pre-set to `https://github.com/Ghinsinc/ghost-pool.git`. Once the empty
repo exists and J is added as a collaborator, J runs
`git push -u origin main` to seed the contract, validator, pooled SQL, and J's
data. (Prefer to seed it yourself? J can hand over a zip of the committed tree.)

## 5. Each member then builds their exporter
- Clone, make `data/<your initial>/`, write your exporter NATIVELY (CONTRACT.md
  is the spec; `reference_exporter_J.py` is reference-only — don't run it
  verbatim, team rule), run `validate_export.py`, commit + push. ~½ day each.
- Monthly: re-check the collaborator list (`Settings → Collaborators`) — an org
  has more ways to leak than a personal repo, so audit access, not just code.

## What NEVER goes in here
Keys, seeds, wallet addresses, server IPs, my-bot files, raw .db.
`tools/validate_export.py` leak-scans every export; run it before every push.
