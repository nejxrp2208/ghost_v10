Read my-bot.md in this repo for my bot's SSH host, database path, and log path.
Then do a READ-ONLY health check: query the positions table for the last 8
trades, today's and rolling-24h P&L vs my caps, balance vs my floor (count
in-flight stakes), and current ladder streak; grep the live log's last ~60
lines for SNIPE/skip/err lines. Report a compact status. Never change anything.
Verify from the database, never from a status banner (CLAUDE.md rule 1).
