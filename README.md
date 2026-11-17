# Friday

A personal assistant that lives on your VPS and talks to you over Telegram (iPhone and Mac).

- **Chat** in Korean or English. Codex answers using your ChatGPT plan (`codex exec`).
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
- **Coding jobs**: ask for a change to a project on the server; Claude Code or Codex does it in a
  sandbox. GitHub repos get a pull request you merge or close from Telegram; local projects are
  changed in place, with an Undo button.
- **Coding with Claude**: Claude Code's Remote Control runs on the same server. Open it from
  the Claude iPhone app.

Everything (code, SQLite database, files) stays on your server. The bot polls Telegram, so no
inbound ports are open apart from SSH.

```
iPhone / Mac ── Telegram ──► friday bot (systemd) ──► codex exec ──► friday MCP tools ──► SQLite
                                     │                     │                             ▲
                                     │                     └──► Playwright MCP ──► Chromium (systemd)
                                     └── reminder / calendar / brief / browser-run loops ┘
Claude iPhone app ──► claude remote-control (tmux, systemd)
```

## Quick start

You need:

- A fresh **Ubuntu 24.04** VPS (4 vCPU, 8 GB is plenty) that you can reach as root over SSH.
- A Telegram account, a ChatGPT plan (for Codex) and, for coding jobs and Remote Control, a
  Claude plan.
- Your own copy of this repo on GitHub (a fork, or a private repo you push it to), and the
  [GitHub CLI](https://cli.github.com) signed in (`gh auth login`).

Clone your copy (replace `<you>`):

```bash
gh repo clone <you>/friday && cd friday
```

Then run the guided setup from that folder:

```bash
deploy/setup.sh
```

It walks through the steps below: SSH key and aliases, preparing the server, the bot's settings,
GitHub access for the server, signing in to Codex and Claude, the first deploy, and your
Telegram user id. Each step checks whether it's already done and skips it, so you can run it
again at any time (for example to add an optional key). It asks before changing anything and
never shows the secrets you type.

## Setup, step by step

The same steps by hand, for reference. Replace `SERVER_IP` with your server's address and
`<you>/friday` with your copy of the repo.

### 1. Order the server

Any VPS with Ubuntu 24.04 works, e.g. Contabo's Cloud VPS 4 (4 vCPU, 8 GB, 100 GB) in a region
near you, no add-ons.

Make an SSH key on your Mac and paste the **public** key into the order form's SSH key field:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/friday_ed25519 -C friday-vps
```

```bash
cat ~/.ssh/friday_ed25519.pub
```

If the form has no SSH key field, add it after the server is delivered (this asks for the root
password from your provider once):

```bash
ssh-copy-id -i ~/.ssh/friday_ed25519.pub root@SERVER_IP
```

Then add this to `~/.ssh/config` on your Mac (replace `SERVER_IP`):

```
Host friday
  HostName SERVER_IP
  User friday
  IdentityFile ~/.ssh/friday_ed25519
  IdentitiesOnly yes

Host friday-root
  HostName SERVER_IP
  User root
  IdentityFile ~/.ssh/friday_ed25519
  IdentitiesOnly yes
```

### 2. Prepare the server

Installs packages, Node + Codex CLI, Claude Code, uv and Chromium, creates the `friday` user,
turns off password login and enables the firewall.

```bash
ssh friday-root 'bash -s' < deploy/bootstrap.sh
```

### 3. Sign in to Codex and Claude on the server

```bash
ssh -t friday 'codex login --device-auth'
```

```bash
ssh -t friday 'cd ~/code && ~/.local/bin/claude'
```

In Claude, run `/login`, trust the folder when asked, then `/exit`.

### 4. Server access to GitHub

The server deploys your repo's `main` from GitHub, so it needs read-write access to that one repo
through a deploy key (not your personal account). The repo is the one `git remote get-url origin`
shows on your Mac; below it's `<you>/friday`.

On the server, make the key and point an SSH alias at it:

```bash
ssh friday 'ssh-keygen -q -t ed25519 -N "" -C "friday-vps deploy key" -f ~/.ssh/github_friday && printf "Host github-friday\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/github_friday\n  IdentitiesOnly yes\n" >> ~/.ssh/config'
```

Register it on the repo with write access (from the Mac, signed in with `gh`):

```bash
ssh friday 'cat ~/.ssh/github_friday.pub' > /tmp/github_friday.pub && gh repo deploy-key add /tmp/github_friday.pub --repo <you>/friday --allow-write --title "friday-vps"
```

Then trust GitHub's host key (check the fingerprint is `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`),
and clone the repo twice: `~/code/friday` is where you edit, `~/friday` is what runs.

```bash
ssh friday 'ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts && ssh-keygen -lf ~/.ssh/known_hosts'
```

```bash
ssh friday 'git clone git@github-friday:<you>/friday.git ~/code/friday && git clone git@github-friday:<you>/friday.git ~/friday'
```

### 5. Create the Telegram bot

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. `cp .env.example .env.server` and put the token in `TELEGRAM_BOT_TOKEN`. Set `FRIDAY_TZ` to
   your timezone. `.env.server` is ignored by git; never commit it.
3. Deploy:

```bash
deploy/push.sh
```

4. Message your bot. It replies with your Telegram user id. Put that id in
   `TELEGRAM_ALLOWED_USER_IDS` in `.env.server` and deploy again. From then on the bot answers
   only you.

### 6. Connect Google Calendar (optional)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the
   **Google Calendar API**.
2. OAuth consent screen: External, add your own Google account as a test user, then
   **Publish app** (in Testing mode Google expires the token every 7 days). The "unverified app"
   warning is expected for a personal app.
3. Credentials → Create OAuth client ID → **Desktop app** → download the JSON and save it as
   `data/google_client_secret.json` in this folder.
4. On your Mac (opens the browser):

```bash
uv run friday google-auth
```

5. Deploy again. The token goes to the server:

```bash
deploy/push.sh
```

### 7. Claude Code from your iPhone

Give Claude on the server the Friday tools (reminders, tasks, calendar, memory, send a file to
Telegram). They use the same database as the bot:

```bash
ssh friday '~/.local/bin/claude mcp add friday --scope user -- ~/friday/.venv/bin/friday mcp'
```

Start Remote Control:

```bash
ssh friday 'systemctl --user enable --now claude-rc'
```

Open the Claude iPhone app and look in its Code section for the session named **vps**. Projects
go in `~/code` on the server. To see the Remote Control console, run `ssh -t friday tmux attach -t claude-rc`
(detach with Ctrl-b d).

### 8. Coding jobs (optional)

Friday pushes branches and opens pull requests with its own write token. Create a fine-grained
token at https://github.com/settings/personal-access-tokens with:

- Repository access: **Only select repositories**, just the repos Friday may work on.
- Permissions: **Contents: Read and write**, **Pull requests: Read and write** (Metadata: Read is added automatically).

Put it in `GITHUB_CODE_TOKEN` in `.env.server` and deploy:

```bash
deploy/push.sh
```

This token is separate from the read-only `GITHUB_TOKEN` the chat uses. Friday removes it from
its environment at startup, so neither the chat agent nor the coding agents get it; only Friday's
own git and GitHub calls use it. The agents run as the same user, though, and the token is still
in `~/friday/.env`, so give it only the repos you'd let an agent push branches to. Without it,
jobs still run but stop at a local commit. `CODE_ENGINE` (claude or codex) picks the default agent.

### 9. Voice messages (optional)

Codex can't listen to audio, so Friday turns voice messages into text first. Create a key at
https://console.groq.com/keys, put it in `TRANSCRIBE_API_KEY` in `.env.server` and deploy.
For OpenAI instead, also set `TRANSCRIBE_PROVIDER=openai` and use an OpenAI API key
(`TRANSCRIBE_PROVIDER` is `groq` or `openai`; `groq` when unset). `TRANSCRIBE_MODEL` overrides
the provider's default model and `TRANSCRIBE_LANGUAGE` (e.g. `ko`, `en`) skips language detection.
Like `GITHUB_CODE_TOKEN`, the key is removed from Friday's environment at startup, so the agents
don't get it in theirs (it's still in `~/friday/.env`; a spending limit on the key is a good idea).
Audio files (not voice messages) get a `.txt` transcript saved next to them. `/status` shows
whether transcription is on.

### 10. Browser logins (optional)

The shared browser needs no setup: `bootstrap.sh` installs Chromium and each deploy starts
`friday-browser.service` (set `FRIDAY_BROWSER=false` in `.env.server` to turn it off). Its
profile in `~/friday-data/browser` keeps sites logged in across restarts.

Logins are usually added from Telegram (see **Browser** below: `/login`, or Friday asks when it
needs one). You can also add one on the server; this asks for the username and the password (not shown):

```bash
ssh -t friday 'cd ~/friday && .venv/bin/friday vault add school --domain lms.example.edu'
```

`school` is a short name of your choice; `--domain` is the site it may be typed into, subdomains
included (repeat it for more). See saved logins (and where they're trusted) and delete them:

```bash
ssh friday 'cd ~/friday && .venv/bin/friday vault list'
```

```bash
ssh friday 'cd ~/friday && .venv/bin/friday vault remove school'
```

### 11. Secure form (optional)

When Friday needs structured input or a secret (a login, a one-time code, an API key, a few
choices), its request comes with a **📝 입력하기** button under the text field. It opens a small
form inside Telegram (a Mini App, `site/login/index.html`) with the fields the agent asked for:
text, masked passwords, codes, numbers, email, URL, longer text, dropdowns, choice buttons,
checkboxes, dates and times. The form encrypts what you type for your server (AES-256-GCM, the
key wrapped with the server's RSA-3072 key, kept in `~/friday-data/vault/form_key.pem`), so
Telegram only carries ciphertext. The form's settings travel in the URL's `#` part, which
browsers never send to the web host.

Sensitive fields (🔐; passwords always) go only to the vault and never to the agent, which gets
just "stored as <name>.<field>"; the other values are handed to the agent. The request message
and the form both say, in Friday's own words, which vault entry a 🔐 value goes to and which site
it is bound to (or that it isn't bound to one). If that doesn't match what you're doing, cancel.

The form is a static page published by GitHub Pages (`.github/workflows/pages.yml`). Turn Pages
on once, in the repo's Settings > Pages > Source: **GitHub Actions**, or:

```bash
gh api -X POST repos/<you>/friday/pages -f build_type=workflow
```

Then push (or run the "Pages" workflow) and it is served at `https://<you>.github.io/friday/login/`,
which Friday uses by default (worked out from the repo's GitHub `origin`). Free Pages needs a
**public** repo; with a private one, host `site/login/index.html` on any static https host and
set `FRIDAY_FORM_URL` in `.env.server` to its address. `FRIDAY_FORM_URL=` (empty) turns the form
off. Without the form, or if it doesn't open, reply to the request message instead: one value
per line (username, then password, then any extra fields it lists).

### 12. Files up to 2 GB (optional)

Telegram's Bot API lets bots download files up to 20 MB and send up to 50 MB. A local Bot API
server ([tdlib/telegram-bot-api](https://github.com/tdlib/telegram-bot-api), built by
`bootstrap.sh` into `/usr/local/bin`) raises both to 2 GB. It runs as `friday-botapi.service`,
connects out to Telegram and listens on 127.0.0.1:8081 only, so no port is opened.

1. Sign in at https://my.telegram.org with your phone number → **API development tools** →
   create an app (any name). Put its `api_id` and `api_hash` in `.env.server` as
   `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.
2. Deploy (`deploy/push.sh`). It starts `friday-botapi.service` and restarts Friday pointed at it.
3. Right after, log the bot out of Telegram's cloud Bot API, once (it asks to confirm):

   ```bash
   ssh -t friday 'cd ~/friday && .venv/bin/friday botapi switch'
   ```

   Without this Telegram doesn't guarantee the bot gets its messages through the local server.
   After it, the cloud API refuses the bot for about 10 minutes.
4. Check with `ssh friday 'cd ~/friday && .venv/bin/friday botapi status'` and send a big file.

Going back: remove `TELEGRAM_API_ID` from `.env.server`, call `logOut` on the local server
(`curl -s -X POST http://127.0.0.1:8081/bot<token>/logOut` on the VPS) and deploy; the cloud API
takes the bot back 10 minutes after the switch.

## Using it

| Telegram | What it does |
|---|---|
| any message | talk to Friday |
| send a file / photo | saved to `files/inbox/<date>/` and answered: with a caption, about that; without one, Friday waits a few seconds for a message, then looks at it and does the obvious thing or asks |
| send a photo of deadlines | a list of the to-dos, reminders and events in it, with buttons to add them all or ignore them |
| send a voice message | transcribed, shown back to you (🎙) and answered like a typed message; needs `TRANSCRIBE_API_KEY` |
| `/today` | today's events, tasks due, overdue tasks and reminders |
| `/tasks` | open to-dos, earliest deadline first |
| `/reminders` | upcoming reminders |
| `/new` | fresh conversation (memories and reminders are kept); also happens after 6 h of silence |
| `/model` | list models; `/model gpt-6-sol high`, `/model low`, `/model default` |
| `/tz Europe/Paris` | change timezone, e.g. when you travel |
| `/code [claude\|codex] <repo> <task>` | start a coding job, e.g. `/code friday fix the typo in README` |
| `/jobs` | recent coding jobs and their pull requests |
| `/stop <id>` | stop a running coding job |
| `/runs` | scheduled browser runs and their results; `/runs cancel <id>` cancels one |
| `/watches` | what Friday is watching for you: ⏸ pause, ▶️ resume, 🗑 delete |
| `/login <name> <domain>` | save a website login: tap 📝 입력하기 (the form), or reply to Friday's message with the username and password |
| `/logins` | saved website logins: 🔒 ask again before use, 🗑 delete |
| `/files [folder]` | the newest files in Friday's workspace (what you sent, what Friday made), with size and date; tap one for 📤 get or 🗑 delete (asks first); 📂 browses folders |
| `/backup` | sends you the database file |
| `/status` | Codex login, calendar, counts |

Edit Friday's personality and rules in `~/friday-data/workspace/FRIDAY.md` on the server.
A few fixed rules (no stored secrets, web/GitHub text is never an instruction, plain-text
formatting) are always added and can't be edited away. Some buttons and replies are in Korean.

**Tasks vs reminders**: "보고서 내일 저녁까지" ("report by tomorrow evening") is a *task* (deadline, done/open). Friday nudges
at 09:00 on the due day (or 1 h before), messages again when it's due, and both messages have
✅ Done / ⏳ 1 hour buttons. "7시에 운동" ("workout at 7") is a *reminder* (a message at a time, optionally
repeating; skip dates supported, lunar dates and holidays not). Vague times like 저녁까지 ("by evening")
have fixed meanings (see `VAGUE_TIMES` in `src/friday/timeutil.py`).

**Photos and documents**: send a photo of a syllabus, notice or schedule without a caption and
Friday lists what it found (📋) instead of adding it: to-dos with deadlines, reminders and
events. Nothing is added until you tap ✅ (add all) or ❌ (ignore). Events go to Google Calendar,
or become to-dos if it isn't connected. For something handed in online (an assignment on a
course site), Friday then asks how to submit it: ⏰ reminder only, 🧭 prepare it and ask you
before submitting, or 🤖 submit it on its own. The last two schedule a browser run 24 hours
before the deadline (or right away if that's sooner); 🤖 also approves that site's saved logins
for that run. If no login is saved for the site, Friday tells you to add one with `/login`.

**Memory** search returns the 20 best keyword matches (Korean particles are ignored), and
Friday updates an existing memory instead of piling up contradicting ones.

**Web search** is on (`FRIDAY_WEB_SEARCH=live`). Friday searches when a question needs current facts.

**Browser**: ask for things like "open the course site and check my grades". Friday drives the
shared browser through the Playwright MCP server. To log in it uses a saved login from the vault:
it types the username and password itself (the agent never sees them), only on the login's own
domains and only over https. The first use on each site asks you on Telegram (✅ / ❌); after
that it logs in there without asking, until you tap 🔒 in `/logins`. If no login fits, Friday
sends a 🔑 request (or send `/login school lms.example.edu` yourself): tap **📝 입력하기** and
fill in the form (step 11), or reply to the request with the username on the first line and the
password on the second. Friday can ask for extra fields a site needs (a student id, a PIN); they
are stored with the login and typed by Friday too. The bot stores them in the vault, deletes
your message from the chat (if that fails it tells you to delete it) and never passes the text
to the agent or the logs. A login saved this way is already approved for its site. For a
one-time code (SMS, OTP) Friday sends the same kind of request; the code goes to the agent for
that login only and isn't stored. `friday vault add` on the server still works. Before anything
irreversible (submit, send, pay, delete) it sends you a screenshot and waits for ✅; without an
answer within 5 minutes it doesn't go ahead. Ask for it later ("tomorrow at 9, submit the
report on the course site") and Friday schedules a browser run: you approve the plan and the
logins it may use, and pick 🧭 (ask before the final submit) or 🤖 (submit without asking). At
that time a fresh agent does the work and sends you the result and a screenshot of the last page.
`/runs` lists runs; `/runs cancel <id>` cancels a pending or running one. The browser can't open
`file://` pages, and its control port listens on the server's localhost only.

**Watches** ("이러면 알려줘"): say what to watch in plain words, e.g. "이캠퍼스에 새 공지 올라오면
알려줘", "friday 레포 PR에 리뷰 달리거나 CI 실패하면 알려줘" or "12월 20일 오전 서울→부산 KTX 좌석 나오면
알려줘". Friday sends a 🔔 approval with what it will check, how often (at least every 15 minutes,
to spare your ChatGPT plan; e.g. 30m, 2h, daily 09:00), until when, and which tools and saved
logins it may use. Once you tap ✅, a fresh agent checks right away and then on that interval, one
check at a time and never alongside a scheduled browser run. Checks only look: no submitting,
sending, buying or posting, GitHub is read-only, and the only logins are the ones you approved
for the watch (a GitHub-only watch doesn't use the browser). You get "🔔 title + what changed"
only when the condition is met with something new, with ✅ 그만 보기 / ⏸ 일시중지 buttons. After 3
failed checks in a row the watch pauses and Friday tells you once; a watch with an end date stops
with a short note. Up to 10 watches can be active; `/watches` lists them. Email isn't connected
yet, so for "교수님 메일 답장 없으면" Friday says so and offers what it can (e.g. a watch on your
webmail in the browser).

**GitHub**: create a fine-grained token at https://github.com/settings/personal-access-tokens
with access to the repos you want and read-only permissions (Contents, Issues, Pull requests,
Metadata). Put it in `GITHUB_TOKEN` in `.env.server` and deploy. `GITHUB_READONLY=true` also
asks GitHub's MCP server to hide all write tools.

**More MCP servers**: edit `~/friday-data/mcp.toml` on the server (examples inside). Each
`[servers.<name>]` table takes the same keys as Codex's MCP config. The file is read on every
message, so no restart is needed; `/status` shows what loaded and any mistakes. Tokens go in
`.env.server`, referenced with `bearer_token_env_var` or `env_from`. Friday can't edit this file
itself. From your iPhone, the easiest way to edit it is to ask Claude in the Remote Control
session.

```bash
ssh -t friday 'nano ~/friday-data/mcp.toml'
```

**Coding jobs**: send `/code friday fix the typo in README`, or just ask ("friday 레포에서 README
오타 고쳐줘"). `<repo>` is a folder in `~/code` on the server or `owner/name`, which is cloned to
`~/code/<name>` on first use. Claude Code is the default agent; `/code codex <repo> <task>` uses Codex.

**GitHub repos** (the folder's `origin` is on GitHub):

1. The agent works in its own git worktree under `~/code/.jobs`, on a new branch `friday/job-<id>`
   from the default branch. It edits and runs tests but doesn't commit or push.
2. Friday commits the changes, pushes the branch and opens a pull request, then messages you
   the agent's summary and the link, with buttons to merge or close it.
3. To ask for more changes, reply to that message. The agent continues the same session in the
   same worktree and the pull request gets a new commit.
4. Merging squash-merges the pull request and removes the branch and worktree; closing drops them.
   For Friday's own repo (the `origin` of `~/friday`) a 🚀 Deploy button follows the merge.

Only code you merge reaches the default branch.

**Local projects** (no GitHub remote): `/code myapp make a simple todo web page`. A name that
doesn't exist yet becomes a new folder `~/code/myapp`, and a folder without git gets `git init`
with a snapshot of what's there. The agent changes the folder itself; Friday first commits any
uncommitted work of yours separately, then commits the job's changes. The result message has an
↩️ Undo button that reverts the job's commits (as new commits, so nothing is lost). Replies work
the same way.

Jobs run one at a time, and each stops after `CODE_JOB_TIMEOUT_SECONDS` (1 hour); `/stop <id>` ends one
early. The agents run in a sandbox: they can write only in the project (and package caches), reach
only GitHub, PyPI and npm, and can't read `.env`, Friday's data, SSH keys or the agents' logins.

**Passwords**: Friday refuses to save passwords, keys, card or ID numbers, both by instruction
and by a check inside its tools. Website logins for the browser go only in the vault, added by
replying to a 🔑 login request (`/login`) or from the server's terminal (step 10). Keep everything else in your password manager.

## Operating

```bash
ssh friday 'journalctl --user -u friday -f'
```

```bash
ssh friday 'cd ~/friday && .venv/bin/friday doctor'
```

### Changing the code

The server runs GitHub's `main`; only pushed commits go live.

- **From the Mac**: commit, `git push`, then `deploy/push.sh` (it also uploads `.env.server`
  and the Google files).
- **From the iPhone**: ask Claude in the **vps** Remote Control session to work in
  `~/code/friday`. It commits, pushes, and deploys with `bash ~/friday/deploy/deploy.sh`
  (see `CLAUDE.md`). Run `git pull` on the Mac afterwards.

Never edit `~/friday` on the server: each deploy resets it to GitHub's `main`. To undo a
deploy, `git revert` the commit, push, and deploy again.

Optional: switch Codex to an OpenAI API key if you hit your ChatGPT plan's limits. Run this,
paste the key, press Enter, then Ctrl-D. Run `codex login --device-auth` to switch back.

```bash
ssh -t friday 'codex login --with-api-key'
```

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
