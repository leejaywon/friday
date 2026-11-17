# Friday

Personal Telegram agent (Python 3.12, uv). README.md explains what it does and how it's run.

## On the VPS

- `~/code/friday`: this repo. Make code changes here.
- `~/friday`: the running copy. Never edit it: every deploy resets it to GitHub's `main`.
- `~/friday-data`: database, files, Google token, `mcp.toml`, the browser profile and the login
  vault. Not in git; never delete it.
- `~/friday/.env`: secrets. Never print, copy or commit them.

## Workflow

1. `git pull` before starting (the owner may push to the same repo from another machine).
2. Change code, then run `uv run pytest -q`.
3. Commit with a clear message and `git push` (to this repo's GitHub `origin`, branch `main`).
4. Deploy only when the user asks or agrees: `bash ~/friday/deploy/deploy.sh`.
   It deploys GitHub's `main`, restarts the bot and prints its status. Check the status shows
   `active (running)`; if not, read `journalctl --user -u friday -n 50`.

To undo a bad deploy, `git revert` the commit, push, and deploy again.

If Claude here has the `friday` MCP server (`claude mcp add friday --scope user -- ~/friday/.venv/bin/friday mcp`),
the Friday tools (reminders, tasks, calendar, memory, send a file to Telegram) write to the same
database the bot uses.
