<img src="assets/banner.webp" alt="Friday" width="100%">

![Python 3.12](https://img.shields.io/badge/Python_3.12-3776AB?style=flat-square&logo=python&logoColor=white)
![uv](https://img.shields.io/badge/uv-DE5FE9?style=flat-square&logo=uv&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-26A5E4?style=flat-square&logo=telegram&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-000000?style=flat-square&logo=modelcontextprotocol&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white)
![Ubuntu 24.04](https://img.shields.io/badge/Ubuntu_24.04-E95420?style=flat-square&logo=ubuntu&logoColor=white)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-D22128?style=flat-square&logo=apache&logoColor=white)](LICENSE)

A personal assistant that lives on your own server and talks to you over Telegram.

- **Chat** in any language. Friday runs on your ChatGPT plan (Codex) or your Claude plan
  (Claude Code), whichever you have.
- **Reminders** that always arrive: a scheduler on the server sends them, independent of the AI.
  Repeats are supported ("every weekday at 9").
- **Google Calendar**: read and add events, heads-up before meetings, morning brief.
- **Memory**: Friday saves lasting facts about you and recalls them later.
- **Files and photos**: send them to the bot; they land in `files/inbox/` for Friday to use. A
  photo of a schedule, notice or invoice turns into a list of deadlines and events you add with one tap.
  `/files` lists them to get back or delete. Telegram's limits apply: 20 MB in, 50 MB out.
- **Voice messages**: Friday transcribes them (Groq Whisper or OpenAI) and answers what you said.
- **Browser**: shared headless Google Chrome on the server, with H.264/AAC playback and an
  encrypted login vault. It asks you on Telegram before the first login on each site and before
  anything irreversible, and can do browser work at a scheduled time (e.g. submit a form).
- **Browser audio**: ask Friday to record the browser, save the M4A, or transcribe and explain
  a lecture using your existing Groq/speech-to-text connection. Capture is on demand; recordings
  stay on the server until you request a file or transcription.
- **Watches**: "tell me when that website posts a new notice": Friday checks on a schedule and
  messages you only when something new matches.
- **Coding jobs**: ask for a change to a project on the server; Claude Code or Codex does it in a
  sandbox. GitHub repos get a pull request you merge or close from Telegram; local projects are
  changed in place, with an Undo button.

Everything (code, SQLite database, files) stays on your server. The bot polls Telegram, so no
inbound ports are open apart from SSH.

```
Telegram ──► friday bot (systemd) ──► Codex or Claude Code ──► friday MCP tools ──► SQLite
                     │                        │                                     ▲
                     │                        └──► Playwright MCP ──► Chrome (systemd)
                     └── reminder / calendar / brief / browser-run / watch loops ───┘
```

## Setup

You need:

- A fresh **Ubuntu 24.04 amd64 (x86_64)** VPS you can reach as root over SSH. Size it by what
  you'll use: the bot itself is light; the headless browser and coding agents take more memory.
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
(`deploy/bootstrap.sh`: packages, the Codex and Claude Code CLIs, Google Chrome, the `friday` user,
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
| `/login <name> <domain>` | save a website login through a form (see **Forms**) |
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

**Photos and documents**: send a photo of a schedule, notice or invoice without a caption and
Friday lists what it found (📋) instead of adding it. Nothing is added until you tap ✅ Add all.
Events go to Google Calendar, or become to-dos if it isn't connected. For something handed in
online, Friday then asks how: ⏰ reminder only, 🧭 prepare it and ask you before submitting, or
🤖 submit it on its own. The last two schedule a browser run 24 hours before the deadline.

**Browser**: ask for things like "open the shop's site and check my order". Friday drives the
shared browser through the Playwright MCP server. To log in it uses a saved login from the vault:
it types the username and password itself (the agent never sees them), only on the login's own
domains and only over https. The first use on each site asks you on Telegram (✅ / ❌). If no
login fits, Friday sends a 🔑 request (see **Forms** below). One-time codes (SMS, OTP) are asked
for the same way and aren't stored. Before
anything irreversible (submit, send, pay, delete) Friday sends a screenshot and waits for ✅.
"Tomorrow at 9, submit the report on the website" schedules a browser run: you approve the
plan and the logins it may use. The browser can't open `file://` pages, and its control port
listens on the server's localhost only. `FRIDAY_BROWSER=false` turns it off.

**Forms**: when Friday needs a login, a code, a key or a few choices, its request comes with a
**📝 Open form** button under the text field. It opens Friday's Mini App, a small page inside
Telegram, and what you type there is encrypted for your server (AES-256-GCM, the key wrapped with
your server's RSA key in `~/friday-data/vault/`), so Telegram carries only ciphertext. Friday
asks only for the fields that site or service needs: an email alone, a username and password, one
API key, a public and a secret key. Passwords, keys and other 🔒 fields are hidden from the AI;
an email or username is passed on so it knows which account. Logins and keys are kept in the
vault for later. If Friday asks for something the site doesn't use, tap Cancel and tell it what
the site needs. You can also reply to the request message with one value per line (`-` for a
field that isn't needed); Friday deletes your reply. The page (`site/app/`) is the same for
every Friday, so all installs use the copy this repo publishes at
https://leejaywon.github.io/friday/app/. To use your own copy, host `site/` on any https address
and set `FRIDAY_APP_URL` to its `app/` page; `FRIDAY_APP_URL=` (empty) turns the button off.

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

The shared browser is Google Chrome Stable, installed by `deploy/bootstrap.sh` with its
H.264/AAC codecs, Ubuntu AppArmor sandbox profile and root-owned Chrome policies. Chrome
updates through its APT repository. Normal deploys leave the running browser alone; after a
browser/unit change, stop `friday-browser.service`, back up `~/friday-data/browser` with its
permissions intact, and start it again. Preserve that profile to retain site logins. When
moving from a newer Chromium to Chrome Stable, verify a profile copy before switching.

To check actual video frames and decoded audio samples in the running browser:

```bash
ssh friday 'cd ~/friday && .venv/bin/python deploy/check-browser.py'
```

It opens and closes a test tab, serves generated samples on a temporary loopback port, and
returns a nonzero exit code if H.264, AAC or combined playback fails. It does not require a
speaker and does not read login cookies.

### Browser audio recording

`friday-audio.service` runs a private PulseAudio server with the `friday_browser` null sink.
Chrome alone is configured to send output there; FFmpeg records its monitor source. Microphone
input and other applications' default audio outputs are not captured. All audible Chrome tabs
share that sink, so pause unrelated tabs before recording. Muted/paused media produces silence.

Examples to send Friday:

- "Record this lecture for 50 minutes."
- "Stop the browser recording and send me the audio file."
- "Transcribe the recording and explain the main points in Korean."
- "What did the lecturer say about regularization?"

The tools are `start_browser_recording`, `stop_browser_recording`, `browser_recording_status`,
`list_browser_recordings`, `transcribe_browser_recording` and `read_browser_transcript`.
Recording defaults to 60 minutes with a hard maximum of 120, and runs in its own systemd user
unit so chat/MCP restarts don't interrupt it. No audio is recorded automatically. Recordings
are saved under `files/browser-audio/<id>/` in the workspace: `recording.m4a`, five-minute M4A
parts and (when requested) `transcript.txt`. `/files` and `send_file` can retrieve saved files.
Call status after an automatic stop to finalize the combined M4A.

Transcription uses the existing `TRANSCRIBE_*` configuration. It sends up to three five-minute
parts per tool call, caches successful parts for retries, and skips near-silent parts. Repeat
until `remaining_parts` is zero; the read tool pages through longer transcripts. Friday uses
the text as source material for explanations and identifies partial recordings. Saving a
recording alone does not trigger an upload or transcription. A recording/transcript remains
on disk until the owner removes it; no automatic cleanup or retention deadline is applied.

On older servers install `pulseaudio pulseaudio-utils ffmpeg` as root before deploying. Back up
the stopped browser profile, then restart `friday-browser.service` once to apply its PulseAudio
environment; normal deployments leave an already running browser alone. The existing Chrome
profile and logins are retained.

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
