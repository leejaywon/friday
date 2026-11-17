"""Tools Codex calls while answering: reminders, tasks, calendar, memory, files, coding jobs,
proposals, and browser logins and approvals.

Codex starts this as a stdio MCP server for each run. It writes to the same SQLite database the
bot reads, and anything meant for the user's phone goes through the outbox table.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp.server import MCPServer

from . import browser, gcal
from . import timeutil as tu
from .config import ensure_dirs, load_config
from .db import Store
from .jobs import REPO_ARG, github_slug, short
from .secrets_guard import REFUSAL, looks_secret
from .vault import Vault, check, domain_allowed

cfg = load_config()
ensure_dirs(cfg)
store = Store(cfg.db_path)

server = MCPServer(
    "friday",
    instructions=(
        "Personal assistant tools. Times are ISO 8601; a time without an offset is read as the "
        "user's local timezone (see current_time)."
    ),
)


def _tz() -> str:
    return store.tz(cfg.default_tz)


@server.tool()
def current_time() -> str:
    """The current date, time and the user's timezone. Call this before working out relative times."""
    tz = _tz()
    return f"{tu.local_str(tu.now_utc(), tz)} ({tz})"


@server.tool()
def set_timezone(timezone: str) -> str:
    """Change the user's timezone, e.g. 'Asia/Seoul' or 'Europe/Berlin', when they say they moved."""
    ZoneInfo(timezone)
    store.set("tz", timezone)
    return f"Timezone set to {timezone}. Existing reminders keep the clock time they were set for."


@server.tool()
def add_reminder(text: str, when: str, repeat: str = "", skip_dates: str = "") -> str:
    """Schedule a Telegram reminder: a message at a time, optionally repeating.

    For something the user must get done by a deadline, use add_task instead.

    text: what to remind the user about, written as the message they will receive.
    when: ISO 8601 date-time the series starts, e.g. '2026-11-17T09:00'. For a repeating
        reminder the first alert is the first time at or after this that matches the rule.
    repeat: optional iCalendar RRULE, e.g. 'FREQ=DAILY', 'FREQ=WEEKLY;INTERVAL=2;BYDAY=TU',
        'FREQ=MONTHLY;BYDAY=FR;BYSETPOS=-1' (last Friday), 'FREQ=DAILY;INTERVAL=3;UNTIL=20270228T235959'.
        Don't put several times of day in one rule (BYHOUR/BYMINUTE produce every combination);
        add one reminder per time of day instead. Empty for a one-off.
        Lunar dates and public holidays are not supported: tell the user, and offer solar dates.
    skip_dates: optional comma-separated local dates to skip, e.g. '2026-12-25,2027-01-01'.

    The result lists the next alerts. Show them to the user so they can catch a misunderstanding.
    """
    if looks_secret(text):
        return REFUSAL
    tz = _tz()
    dt = tu.parse_when(when, tz)
    if not repeat and dt < tu.now_utc() - timedelta(minutes=1):
        return f"Not scheduled: {tu.local_str(dt, tz)} is in the past."
    try:
        r = store.add_reminder(text, dt, tz, repeat or None, tu.parse_dates(skip_dates))
    except ValueError as e:
        return f"Not scheduled: {e}"
    return f"Scheduled #{r.id} '{r.text}'. Next alerts: {'; '.join(r.preview(tz))}"


@server.tool()
def list_reminders() -> str:
    """All upcoming reminders with their ids and next alerts."""
    tz = _tz()
    lines = []
    for r in store.active_reminders():
        nxt = f"  next: {'; '.join(r.preview(tz))}" if r.rrule else ""
        lines.append(r.describe(tz) + nxt)
    return "\n".join(lines) or "No upcoming reminders."


@server.tool()
def cancel_reminder(reminder_id: int) -> str:
    """Cancel a reminder (all future repeats too) by id."""
    return "Cancelled." if store.cancel_reminder(reminder_id) else "No active reminder with that id."


@server.tool()
def reschedule_reminder(reminder_id: int, when: str) -> str:
    """Move a reminder to a new ISO 8601 time. For a repeating reminder this also moves the series."""
    tz = _tz()
    ok = store.reschedule_reminder(reminder_id, tu.parse_when(when, tz))
    return "Rescheduled." if ok else "No active reminder with that id."


@server.tool()
def calendar_events(start: str, end: str) -> str:
    """Events on the user's Google Calendar between two ISO 8601 times."""
    tz = _tz()
    events = gcal.list_events(cfg.google_token, tu.parse_when(start, tz), tu.parse_when(end, tz), ZoneInfo(tz))
    return "\n".join(e.describe() for e in events) or "No events."


@server.tool()
def calendar_create_event(title: str, start: str, end: str, description: str = "", location: str = "") -> str:
    """Add an event to the user's Google Calendar. Confirm details with the user first if anything is unclear."""
    tz = _tz()
    ev = gcal.create_event(
        cfg.google_token, title, tu.parse_when(start, tz), tu.parse_when(end, tz), description, location
    )
    return f"Created '{ev.get('summary')}' [id={ev.get('id')}] {ev.get('htmlLink', '')}"


@server.tool()
def calendar_delete_event(event_id: str) -> str:
    """Delete a Google Calendar event by id. Only do this when the user clearly asked for it."""
    gcal.delete_event(cfg.google_token, event_id)
    return "Deleted."


@server.tool()
def add_task(title: str, due: str = "", remind: str = "", notes: str = "") -> str:
    """Add a to-do: something the user has to get done, optionally by a deadline ("까지", "by").

    title: short description, e.g. '보고서 마무리'.
    due: ISO 8601 deadline, e.g. '2026-11-18T15:00'. Empty if there is no deadline.
        Use the vague-time defaults from your instructions for words like 저녁까지 or 오늘 중.
    remind: ISO 8601 time for a heads-up before the deadline. Leave empty for the default
        (09:00 on the due day, or 1 hour before); 'none' for no heads-up.
    notes: optional details.

    Friday also messages the user when the deadline arrives if the task is still open.
    """
    if looks_secret(f"{title} {notes}"):
        return REFUSAL
    tz, now = _tz(), tu.now_utc()
    due_dt = tu.parse_when(due, tz) if due else None
    if remind.strip().lower() == "none":
        remind_dt = None
    elif remind:
        remind_dt = tu.parse_when(remind, tz)
    else:
        remind_dt = tu.default_task_reminder(due_dt, tz, now) if due_dt else None
    t = store.add_task(title, due_dt, remind_dt, notes)
    return f"Added task {t.describe(tz)}"


@server.tool()
def list_tasks(include_done: bool = False) -> str:
    """Open to-dos, earliest deadline first, with ids. include_done adds the 10 most recently finished."""
    tz, now = _tz(), tu.now_utc()
    lines = [("OVERDUE " if t.due_at and t.due_at < now else "") + t.describe(tz) for t in store.open_tasks()]
    if include_done:
        lines += ["Done: " + t.describe(tz) for t in store.recent_done_tasks()]
    return "\n".join(lines) or "No open tasks."


@server.tool()
def complete_task(task_id: int) -> str:
    """Mark a to-do as done (when the user says they finished it). Find the id with list_tasks."""
    return "Marked done." if store.complete_task(task_id) else "No open task with that id."


@server.tool()
def update_task(task_id: int, title: str = "", due: str = "", remind: str = "", notes: str = "") -> str:
    """Change an open to-do. Only the fields you pass change; remind='none' removes the heads-up."""
    if looks_secret(f"{title} {notes}"):
        return REFUSAL
    tz = _tz()
    t = store.update_task(
        task_id,
        title=title or None,
        notes=notes or None,
        due=tu.parse_when(due, tz) if due else None,
        remind=tu.parse_when(remind, tz) if remind and remind.lower() != "none" else None,
        clear_remind=remind.lower() == "none",
    )
    return f"Updated {t.describe(tz)}" if t else "No open task with that id."


@server.tool()
def cancel_task(task_id: int) -> str:
    """Drop a to-do the user no longer needs to do."""
    return "Cancelled." if store.cancel_task(task_id) else "No open task with that id."


@server.tool()
def remember(fact: str) -> str:
    """Save a lasting fact about the user (preferences, people, routines) for future conversations.

    Call recall first: if a memory on the same subject exists, use update_memory instead so old
    and new facts don't contradict each other. Never use this for passwords, keys, card or ID numbers.
    """
    if looks_secret(fact):
        return REFUSAL
    mid, created = store.remember(fact)
    return f"Saved memory #{mid}." if created else f"Already saved as #{mid}."


@server.tool()
def update_memory(memory_id: int, fact: str) -> str:
    """Replace a saved memory's text, e.g. when the user moved, changed jobs or corrected you."""
    if looks_secret(fact):
        return REFUSAL
    return "Updated." if store.update_memory(memory_id, fact) else "No memory with that id."


@server.tool()
def recall(query: str = "") -> str:
    """Search saved memories. Returns up to 20 best keyword matches; an empty query returns the
    20 newest. Try a second query with other words (e.g. 엄마 / 어머니 / mom) if the first finds nothing."""
    rows = store.memories(query)
    total = store.memory_count()
    if not rows:
        return f"Nothing found ({total} memories saved)."
    body = "\n".join(f"#{r['id']} {r['text']}" for r in rows)
    return body + (f"\n({len(rows)} of {total} memories shown)" if total > len(rows) else "")


@server.tool()
def forget(memory_id: int) -> str:
    """Delete a saved memory by id."""
    return "Forgotten." if store.forget(memory_id) else "No memory with that id."


@server.tool()
def start_coding_job(repo: str, task: str, engine: str = "") -> str:
    """Start a coding job: a coding agent changes one of the user's projects on this server.
    Only when the user's own message asks for a code change or a new project.

    repo: a project folder name in ~/code on the server (e.g. 'friday', 'myapp'), or 'owner/name'
        on GitHub, which is cloned on first use. A name that doesn't exist yet becomes a new
        local project. GitHub repos get a pull request; local projects are changed in place and
        committed, with an Undo button.
    task: the complete instruction for the coding agent. It can't see this chat or ask questions,
        so write everything it needs in full: what to change, where if known, and any details,
        names or wording the user gave. Same language as the user.
    engine: 'claude' (Claude Code) or 'codex'. Empty uses the default; only set it when the user
        names one.

    The job runs in the background, usually for several minutes. Friday messages the result
    (with a pull request for GitHub repos); the user merges, closes or undoes it from that
    message, or replies to it to ask for more changes.
    """
    repo, task, engine = repo.strip(), task.strip(), (engine.strip().lower() or cfg.code_engine)
    if not REPO_ARG.match(repo):
        return f"Not queued: '{repo}' isn't a repo name. Use a folder name in ~/code or owner/name."
    if engine not in ("claude", "codex"):
        return f"Not queued: engine must be 'claude' or 'codex', not '{engine}'."
    if not task:
        return "Not queued: the task is empty."
    if looks_secret(task):
        return REFUSAL
    jid = store.add_job(repo, task, engine)
    new = "" if "/" in repo or (cfg.code_dir / repo).exists() else f" It's a new project: ~/code/{repo}."
    return f"Queued coding job #{jid} on {repo} ({engine}).{new} Friday will message the result when it's done."


@server.tool()
def list_coding_jobs() -> str:
    """The 10 most recent coding jobs: id, status, repo, engine, task and pull request link.

    Statuses: queued, running, review (pull request open), done (finished; local projects are
    committed in place), merged, closed, undone, failed, cancelled.
    """
    lines = []
    for j in store.recent_jobs():
        line = f"#{j['id']} [{j['status']}] {j['repo']} ({j['engine']}): {short(j['task'])}"
        if j["pr_url"]:
            line += f" {j['pr_url']}"
        if j["status"] == "failed" and j["error"]:
            line += f" - {short(j['error'], 120)}"
        lines.append(line)
    return "\n".join(lines) or "No coding jobs yet."


@server.tool()
def list_projects() -> str:
    """The user's projects on this server (folders in ~/code): name, GitHub repo or 'local', and
    the last commit. Use the name with start_coding_job."""
    import subprocess

    def git(path: Path, *args: str) -> str:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""

    lines = []
    for d in sorted(p for p in cfg.code_dir.iterdir() if p.is_dir() and not p.name.startswith(".")) \
            if cfg.code_dir.is_dir() else []:
        remote = github_slug(git(d, "remote", "get-url", "origin")) or "local"
        last = git(d, "log", "-1", "--format=%cs %s") or "no commits"
        lines.append(f"{d.name} ({remote}): {short(last, 80)}")
    return "\n".join(lines) or f"No projects in {cfg.code_dir} yet."


PROPOSAL_KINDS = {"task": "due", "reminder": "when", "event": "start"}  # kind -> its main time field


def proposal_items(raw: str, tz: str) -> list[dict]:
    """Check propose_items' JSON and normalize it: times become local ISO with an offset.
    Raises ValueError with a message the model can act on."""
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"items_json isn't valid JSON ({e})") from None
    if not isinstance(items, list) or not items:
        raise ValueError("items_json must be a non-empty JSON array")
    if len(items) > 30:
        raise ValueError("at most 30 items per proposal")
    now, zone, out = tu.now_utc(), ZoneInfo(tz), []

    def when(value, label: str):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be an ISO 8601 time")
        return tu.parse_when(value, tz)

    for n, item in enumerate(items, 1):
        try:
            if not isinstance(item, dict):
                raise ValueError("must be an object")
            kind, title = item.get("kind"), str(item.get("title") or "").strip()
            if kind not in PROPOSAL_KINDS:
                raise ValueError("kind must be task, reminder or event")
            if not title:
                raise ValueError("title is empty")
            clean: dict = {"kind": kind, "title": title}
            key = PROPOSAL_KINDS[kind]
            value = item.get(key) or item.get("due") or item.get("when") or item.get("start")
            if value or kind != "task":
                dt = when(value, key)
                if dt < now - timedelta(minutes=1) and not (kind == "reminder" and item.get("repeat")):
                    raise ValueError(f"{tu.local_str(dt, tz)} is in the past; leave it out")
                clean[key] = dt.astimezone(zone).isoformat(timespec="minutes")
            if kind == "event":
                end = when(item["end"], "end") if item.get("end") else dt + timedelta(hours=1)
                if end <= dt:
                    raise ValueError("end must be after start")
                clean["end"] = end.astimezone(zone).isoformat(timespec="minutes")
            if kind == "task" and str(item.get("remind") or "").strip():
                remind = str(item["remind"]).strip()
                clean["remind"] = "none" if remind.lower() == "none" else (
                    when(remind, "remind").astimezone(zone).isoformat(timespec="minutes"))
            if kind == "reminder" and item.get("repeat"):
                rule = tu.normalize_rrule(str(item["repeat"]))
                tu.validate_rrule(rule, dt.astimezone(zone).replace(tzinfo=None))
                clean["repeat"] = rule
            for k in ("notes", "location"):
                if str(item.get(k) or "").strip():
                    clean[k] = str(item[k]).strip()
            sub = item.get("submission")
            if sub:
                if not isinstance(sub, dict) or not str(sub.get("site") or "").strip():
                    raise ValueError('submission must be {"site": ..., "what": ...}')
                clean["submission"] = {"site": str(sub["site"]).strip(), "what": str(sub.get("what") or "").strip()}
        except (ValueError, TypeError) as e:
            raise ValueError(f"item {n}: {e}") from None
        out.append(clean)
    return out


@server.tool()
def propose_items(items_json: str, note: str = "") -> str:
    """Propose to-dos, reminders and events for the user to confirm with one tap.

    Use this instead of add_task / add_reminder / calendar_create_event whenever the items come
    from an image, file, document or web page and the user hasn't asked you to add them. Friday
    sends the user a numbered list with "add all" / "ignore" buttons; nothing is added until
    they tap. Don't repeat the list in your reply: say in a sentence what you found.

    items_json: a JSON array. Each item is an object:
        {"kind": "task", "title": "...", "due": "2026-11-20T23:59", "remind": optional ISO or "none"}
        {"kind": "reminder", "title": "message text", "when": "2026-11-19T09:00", "repeat": optional RRULE}
        {"kind": "event", "title": "...", "start": "2026-11-21T14:00", "end": optional (default +1h),
         "location": optional}
      Any item may add "notes" and, for something the user must hand in online (an assignment on
      an e-campus site, a form), "submission": {"site": "lms.example.edu", "what": "report PDF"};
      Friday then asks how to handle the submission.
      Times are ISO 8601 in the user's local time. Leave out items whose time has passed.
    note: optional short context shown under the list, e.g. the course name or source.
    """
    tz = _tz()
    if looks_secret(f"{items_json} {note}"):
        return REFUSAL
    try:
        items = proposal_items(items_json, tz)
    except ValueError as e:
        return f"Not proposed: {e}"
    pid = store.add_proposal(items, note.strip())
    store.queue("proposal", text=str(pid))
    return f"Proposed {len(items)} item(s) as #P{pid}; the user will tap to confirm. Nothing is added yet."


# --- browser ----------------------------------------------------------------

APPROVAL_TIMEOUT = 300  # seconds to wait for a tap; agent.py gives these tools a longer Codex timeout
RUN_ID = int(os.environ.get("FRIDAY_RUN_ID") or 0)  # set by Friday only for a scheduled browser run


async def _ask(kind: str, text: str, screenshot: Path | None = None) -> str:
    """Send a yes/no question with buttons (the bot renders outbox kind 'approval') and wait for
    the tap. Returns approved | denied | expired."""
    aid = store.add_approval(kind, text)
    store.queue("approval", text=str(aid), path=str(screenshot) if screenshot else None)
    deadline = time.monotonic() + APPROVAL_TIMEOUT
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        if (status := store.approval(aid)["status"]) != "pending":
            return status
    store.decide_approval(aid, "expired")
    return store.approval(aid)["status"]  # a tap may have landed just before


NOT_APPROVED = {"denied": "Denied", "expired": "Not approved: no answer within 5 minutes."}


@server.tool()
def list_logins() -> str:
    """Saved website logins: names and domains only (passwords are never shown). Use a name with fill_login."""
    names = Vault(cfg.vault_dir).names()
    return "\n".join(f"{n}: {', '.join(d)}" for n, d in names) or "No saved logins."


@server.tool()
async def fill_login(name: str) -> str:
    """Log in with a saved login: types its username and password into the login form open in the
    browser and submits it. Open the site's login page first. The user approves the first use on
    each site on Telegram; after that it fills there without asking (logins of an approved
    scheduled run are already approved). You never see the password. Returns 'Filled and
    submitted…', 'Denied' or 'Wrong site: <host>'."""
    try:
        login = Vault(cfg.vault_dir).get(name)
    except Exception as e:  # e.g. a missing or wrong key; the message never holds a secret
        return f"The login vault can't be read ({type(e).__name__})."
    if not login:
        return f"No saved login named '{name}'. list_logins shows them."
    try:
        ok, host = await browser.check_site(login.domains)
    except Exception as e:
        return f"The browser isn't reachable ({type(e).__name__})."
    if not ok:
        return f"Wrong site: {host} (open the login form on https://{login.domains[0]} first)"
    # Trusted on this site (approved here once, or typed in for it), or pre-approved for this run.
    if not (domain_allowed(host, login.trusted) or (RUN_ID and store.run_allows_login(RUN_ID, name))):
        status = await _ask("login", f"🔑 '{name}' 로그인 정보를 {host} 에 입력할까요?\n"
                                     "(허용하면 이 사이트에서는 다음부터 묻지 않아요. /logins 에서 되돌릴 수 있어요)")
        if status != "approved":
            return NOT_APPROVED[status]
        try:
            Vault(cfg.vault_dir).trust(name, host)
        except Exception:  # not remembered: it just asks again next time
            pass
    try:
        return await browser.fill_login(login)
    except Exception as e:  # only the type: Playwright's messages could echo what was typed
        return f"Couldn't fill the form ({type(e).__name__})."


@server.tool()
async def request_login(name: str, domain: str, reason: str = "") -> str:
    """Ask the user on Telegram for a website login when no saved login fits (see list_logins).
    They reply with the username and password; Friday deletes that message and stores the login
    in the vault, so you never see it. Waits up to 5 minutes. Returns 'Saved login <name> for
    <domain>' (then call fill_login with that name: it won't ask again on that site),
    'Cancelled' or 'Timed out'.

    name: short name for the login, e.g. 'school' (letters, digits, '.', '_', '-').
    domain: the site it is for, e.g. 'lms.example.edu' (no https://, no path; subdomains included).
    reason: optional few words in the user's language, e.g. '과제 제출'.
    """
    try:
        domain = check(name, [domain])[0]
    except ValueError as e:
        return f"Not requested: {e}"
    rid = store.add_login_request(name, domain, " ".join(reason.split())[:200])
    store.queue("login_request", text=str(rid))
    deadline = time.monotonic() + APPROVAL_TIMEOUT
    while time.monotonic() < deadline and store.login_request(rid)["status"] == "pending":
        await asyncio.sleep(2)
    store.finish_login_request(rid, "expired")  # no-op unless still pending
    status = store.login_request(rid)["status"]
    return {"saved": f"Saved login {name} for {domain}", "cancelled": "Cancelled"}.get(status, "Timed out")


@server.tool()
async def request_approval(summary: str) -> str:
    """Ask the user before an irreversible step (submit, send, post, pay, delete). Friday sends a
    screenshot of the current page with your summary and approve/deny buttons, and waits up to
    5 minutes. Go on only if this returns 'approved'.

    summary: what exactly you're about to do, in the user's language, e.g.
        '자료구조 과제 3 제출: report.pdf 첨부 후 제출 버튼 누름'.
    """
    shot = cfg.files_dir / "browser" / f"approval-{tu.now_utc():%Y%m%d-%H%M%S}.png"
    try:
        url = await browser.screenshot(shot)
    except Exception:
        url = None
    status = await _ask("action", f"✋ {summary}" + (f"\n{url}" if url else ""), shot if url else None)
    return {"approved": "approved", "denied": "denied: don't do it", "expired": "timed out: don't do it"}[status]


@server.tool()
async def schedule_browser_run(when: str, instructions: str, mode: str, logins: str = "") -> str:
    """Schedule browser work for later, only when the user's own message asks for it (e.g.
    "내일 9시에 이캠퍼스에서 과제 제출해줘"). The user approves the plan on Telegram first; that
    also approves the listed logins for that run. At the time, a fresh agent does the work and
    Friday reports the result.

    when: ISO 8601 time to start, e.g. '2026-11-18T09:00'.
    instructions: the complete task for that agent, which can't see this chat: site, what to do,
        which file (workspace path), and what counts as done. Same language as the user.
    mode: 'approve' (prepare everything, then ask the user before the final submit) or 'auto'
        (submit without asking; stops and reports if anything looks unexpected). Ask the user if
        they didn't say.
    logins: comma-separated names from list_logins the run may use, or empty.
    """
    tz = _tz()
    dt = tu.parse_when(when, tz)
    if dt < tu.now_utc() - timedelta(minutes=1):
        return f"Not scheduled: {tu.local_str(dt, tz)} is in the past."
    if mode not in ("approve", "auto"):
        return "Not scheduled: mode must be 'approve' or 'auto'."
    if not instructions.strip():
        return "Not scheduled: the instructions are empty."
    if looks_secret(instructions):
        return REFUSAL
    names = [n.strip() for n in logins.split(",") if n.strip()]
    known = {n for n, _ in Vault(cfg.vault_dir).names()}
    if unknown := [n for n in names if n not in known]:
        return f"Not scheduled: no saved login named {', '.join(unknown)}. list_logins shows them."
    how = {"approve": "준비 후 제출 전에 확인 요청", "auto": "확인 없이 자동 제출"}[mode]
    status = await _ask("schedule", (
        f"🗓 {tu.local_str(dt, tz)}에 브라우저 작업을 예약할까요?\n\n{instructions.strip()}\n\n"
        f"로그인: {', '.join(names) or '없음'}\n방식: {how}"
    ))
    if status != "approved":
        return NOT_APPROVED[status]
    rid = store.add_scheduled_run(dt, instructions.strip(), mode, names)
    return f"Scheduled browser run #{rid} for {tu.local_str(dt, tz)}. The user can see or cancel it with /runs."


@server.tool()
def send_file(path: str, caption: str = "") -> str:
    """Send a file from the workspace to the user's Telegram chat. Path is relative to the workspace."""
    p = (cfg.workspace / path).resolve()
    if not p.is_relative_to(cfg.workspace.resolve()):
        return "Refused: the file must be inside the workspace."
    if not p.is_file():
        return f"No such file: {path}"
    store.queue("file", text=caption or None, path=str(p))
    return f"Queued {p.name} for sending."


def run() -> None:
    server.run("stdio")


if __name__ == "__main__":
    run()
