# Ghost Predator Team Repo

Shared brain for the 4 of us. No keys, no wallets, no server IPs in here — ever.

## Setup (each partner, once, ~5 minutes)
1. Install Claude Code (claude.com/claude-code) and git.
2. Clone this repo: `git clone <repo-url>` (or GitHub Desktop).
3. Copy `my-bot.example.md` to `my-bot.md` and fill in YOUR bot's details
   (it's gitignored — your server info never leaves your machine).
4. Open a terminal IN this folder and run `claude`. Your Claude now knows the
   whole team playbook + your bot. Try `/botcheck`.

## Daily habit
- `git pull` before a session (or tell Claude "pull the team repo").
- When your Claude learns something the team should know, it appends to
  CLAUDE.md LESSONS -> `git commit` -> `git push`.

## What lives here
- `CLAUDE.md` — team rules + verified findings + lessons (the compounding file)
- `.claude/commands/` — shared slash commands (work for everyone via my-bot.md)
- `docs/` — research PDFs (depth gate, instant ladder, exit logger)
- `analysis/queries.sql` — backtest queries to tune thresholds on YOUR book
