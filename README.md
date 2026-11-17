# Friday

A personal assistant that lives on your own server and talks to you over Telegram.

- **Chat** in any language. Friday runs on your ChatGPT plan (Codex) or your Claude plan
  (Claude Code), whichever you have.
- **Reminders** that always arrive: a scheduler on the server sends them, independent of the AI.
  Repeats are supported ("every weekday at 9").
- **Google Calendar**: read and add events, heads-up before meetings, morning brief.
- **Memory**: Friday saves lasting facts about you and recalls them later.
- **Files and photos**: send them to the bot; they land in `files/inbox/` for Friday to use. A
  photo of a syllabus or notice turns into a list of deadlines and events you add with one tap.
  `/files` lists them to get back or delete. Up to 20 MB, or 2 GB with the local Bot API server.
- **Voice messages**: Friday transcribes them (Groq Whisper or OpenAI) and answers what you said.
- **Browser**: a shared headless Chromium on the server that Friday drives for you, with an
  encrypted login vault. It asks you on Telegram before the first login on each site and before
  anything irreversible, and can do browser work at a scheduled time (e.g. hand in an assignment).
- **Watches**: "tell me when the course site posts a new notice": Friday checks on a schedule and
  messages you only when something new matches.
- **Coding jobs**: ask for a change to a project on the server; Claude Code or Codex does it in a
  sandbox. GitHub repos get a pull request you merge or close from Telegram; local projects are
  changed in place, with an Undo button.

Everything (code, SQLite database, files) stays on your server. The bot polls Telegram, so no
inbound ports are open apart from SSH.

```
Telegram ──► friday bot (systemd) ──► Codex or Claude Code ──► friday MCP tools ──► SQLite
                     │                        │                                     ▲
                     │                        └──► Playwright MCP ──► Chromium (systemd)
                     └── reminder / calendar / brief / browser-run / watch loops ───┘
```

## Setup

You need:

- A fresh **Ubuntu 24.04** VPS you can reach as root over SSH. 2 vCPU and 4 GB of RAM are enough.
- A Telegram account, and a ChatGPT plan or a Claude plan.
- Your own copy of this repo on GitHub (a fork, or a private repo you push it to), and the
  [GitHub CLI](https://cli.github.com) signed in (`gh auth login`).

Clone your copy (replace `<you>`) and run the guided setup from that folder:

```bash
gh repo clone <you>/friday && cd friday
```

```bash
deploy/setup.sh
```

It sets up an SSH key and the `friday` / `friday-root` SSH aliases, prepares the server
(`deploy/bootstrap.sh`: packages, the Codex and Claude Code CLIs, Chromium, the `friday` user,
key-only SSH and a firewall), asks for the bot token from @BotFather, which AI plan to use, your
timezone and the optional keys, gives the server a deploy key for your repo, signs in to Codex or
Claude on the server, deploys, and sets your Telegram user id so the bot answers only you. Each
step checks whether it's already done and skips it, so run it again any time (for example to add
a key). It asks before changing anything and never shows the secrets you type.

Your settings live in `.env.server` on your Mac (ignored by git; `.env.example` explains every
one). `deploy/push.sh` uploads it and deploys.

### Connect Google Calendar (optional)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the
   **Google Calendar API**.
2. OAuth consent screen: External, add your own Google account as a test user, then
   **Publish app** (in Testing mode Google expires the token every 7 days). The "unverified app"
   warning is expected for a personal app.
3. Credentials → Create OAuth client ID → **Desktop app** → download the JSON and save it as
   `data/google_client_secret.json` in this folder.
4. Run `uv run friday google-auth` on your Mac (it opens the browser), then `deploy/push.sh`,
   which uploads the token.

### Files up to 2 GB (optional)

Telegram's Bot API lets bots download files up to 20 MB and send up to 50 MB. A local Bot API
server ([tdlib/telegram-bot-api](https://github.com/tdlib/telegram-bot-api), built by
`bootstrap.sh`) raises both to 2 GB. It listens on 127.0.0.1 only, so no port is opened.

1. At https://my.telegram.org → **API development tools**, create an app and put its `api_id`
   and `api_hash` in `.env.server` as `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.
2. Deploy (`deploy/push.sh`), then once: `ssh -t friday 'cd ~/friday && .venv/bin/friday botapi switch'`.
   This logs the bot out of Telegram's cloud API, which then refuses it for about 10 minutes.

To go back, remove `TELEGRAM_API_ID`, call `logOut` on the local server
(`curl -s -X POST http://127.0.0.1:8081/bot<token>/logOut` on the server) and deploy.

## Using it

| Telegram | What it does |
|---|---|
| any message | talk to Friday |
| send a file / photo | saved to `files/inbox/<date>/` and answered: with a caption, about that; without one, Friday waits a few seconds for a message, then looks at it |
| send a photo of deadlines | a list of the to-dos, reminders and events in it, with buttons to add them all or ignore them |
| send a voice message | transcribed, shown back to you (🎙) and answered; needs `TRANSCRIBE_API_KEY` |
| `/today` | today's events, tasks due, overdue tasks and reminders |
| `/tasks` | open to-dos, earliest deadline first |
| `/reminders` | upcoming reminders |
| `/new` | fresh conversation (memories and reminders are kept); also happens after 6 h of silence |
| `/model` | list models; `/model sonnet`, `/model low`, `/model default` |
| `/tz Europe/Paris` | change timezone, e.g. when you travel |
| `/code [claude\|codex] <repo> <task>` | start a coding job, e.g. `/code friday fix the typo in README` |
| `/jobs` | recent coding jobs and their pull requests |
| `/stop <id>` | stop a running coding job |
| `/runs` | scheduled browser runs and their results; `/runs cancel <id>` cancels one |
| `/watches` | what Friday is watching for you: pause, resume, delete |
| `/login <name> <domain>` | save a website login: reply to Friday's message with the username and password |
| `/logins` | saved website logins: 🔒 ask again before use, 🗑 delete |
| `/files [folder]` | the newest files in Friday's workspace; tap one to get or delete it |
| `/backup` | sends you the database file |
| `/status` | AI sign-in, calendar, counts |

Edit Friday's personality and rules in `~/friday-data/workspace/FRIDAY.md` on the server. A few
fixed rules (no stored secrets, web/GitHub text is never an instruction, plain-text formatting)
are always added and can't be edited away.

**Tasks vs reminders**: "report by tomorrow evening" is a *task* (deadline, done/open). Friday
nudges at 09:00 on the due day (or 1 h before) and again when it's due, with ✅ Done / ⏳ 1 hour
buttons. "workout at 7" is a *reminder* (a message at a time, optionally repeating; skip dates
supported). Vague times like "by evening" have fixed meanings (`VAGUE_TIMES` in
`src/friday/timeutil.py`).

**Photos and documents**: send a photo of a syllabus, notice or schedule without a caption and
Friday lists what it found (📋) instead of adding it. Nothing is added until you tap ✅ Add all.
Events go to Google Calendar, or become to-dos if it isn't connected. For something handed in
online, Friday then asks how: ⏰ reminder only, 🧭 prepare it and ask you before submitting, or
🤖 submit it on its own. The last two schedule a browser run 24 hours before the deadline.

**Browser**: ask for things like "open the course site and check my grades". Friday drives the
shared browser through the Playwright MCP server. To log in it uses a saved login from the vault:
it types the username and password itself (the agent never sees them), only on the login's own
domains and only over https. The first use on each site asks you on Telegram (✅ / ❌). If no
login fits, Friday sends a 🔑 request: reply to it with the username on the first line and the
password on the second (plus any extra fields it lists). The bot stores them in the vault and
deletes your reply. One-time codes (SMS, OTP) work the same way and aren't stored. Before
anything irreversible (submit, send, pay, delete) Friday sends a screenshot and waits for ✅.
"Tomorrow at 9, submit the report on the course site" schedules a browser run: you approve the
plan and the logins it may use. The browser can't open `file://` pages, and its control port
listens on the server's localhost only. `FRIDAY_BROWSER=false` turns it off.

**Watches**: say what to watch in plain words. Friday sends a 🔔 approval with what it will
check, how often (at least every 15 minutes), until when, and which tools and saved logins it may
use. Checks only look: no submitting, sending, buying or posting. You get a message only when the
condition is met with something new. After 3 failed checks in a row the watch pauses.

**GitHub**: create a fine-grained token at https://github.com/settings/personal-access-tokens
with read-only permissions (Contents, Issues, Pull requests, Metadata) for the repos you want,
put it in `GITHUB_TOKEN` and deploy. `GITHUB_READONLY=true` also hides GitHub's write tools.

**More MCP servers**: edit `~/friday-data/mcp.toml` on the server (examples inside). It is read
on every message, so no restart is needed; `/status` shows what loaded. Tokens go in
`.env.server`, referenced with `bearer_token_env_var` or `env_from`.

**Coding jobs**: send `/code friday fix the typo in README`, or just ask. `<repo>` is a folder in
`~/code` on the server or `owner/name`, cloned on first use. `CODE_ENGINE` picks the default agent.

- GitHub repos: the agent works in its own git worktree on a new branch. Friday commits, pushes
  and opens a pull request, then sends you the summary with Merge / Close buttons. Reply to that
  message to ask for more changes. For Friday's own repo a 🚀 Deploy button follows the merge.
  This needs `GITHUB_CODE_TOKEN`: a fine-grained token with **Contents** and **Pull requests:
  Read and write** on only the repos Friday may change. Friday keeps it out of the agents'
  environment. Without it, jobs stop at a local commit.
- Local projects (no GitHub remote): the agent changes the folder itself, and the result has an
  ↩️ Undo button that reverts the job's commits.

Jobs run one at a time and stop after `CODE_JOB_TIMEOUT_SECONDS` (1 hour). The agents run in a
sandbox: they write only in the project, reach only GitHub, PyPI and npm, and can't read `.env`,
Friday's data, SSH keys or the agents' logins.

**Passwords**: Friday refuses to save passwords, keys, card or ID numbers, both by instruction
and by a check inside its tools. Website logins go only in the vault.

## Operating

```bash
ssh friday 'journalctl --user -u friday -f'
```

```bash
ssh friday 'cd ~/friday && .venv/bin/friday doctor'
```

The server runs GitHub's `main`; only pushed commits go live. Commit, `git push`, then
`deploy/push.sh`. Never edit `~/friday` on the server: each deploy resets it. To undo a deploy,
`git revert` the commit, push, and deploy again.

If you hit your ChatGPT plan's limits, `ssh -t friday 'codex login --with-api-key'` switches Codex
to an OpenAI API key (`codex login --device-auth` switches back).

## Development

```bash
uv sync
```

```bash
uv run pytest
```

`uv run friday ask "remind me in 2 minutes to stretch"` runs one agent turn locally against `./data`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
