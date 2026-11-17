"""The Telegram bot: chat with Codex, coding jobs, browser runs and approvals, and delivery of
reminders, task alerts, meeting heads-ups and the morning brief."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import mimetypes
import re
import shutil
import sqlite3
import tempfile
import time as clock
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import botapi, browser, gcal, mcp_config
from . import timeutil as tu
from . import workspace as wsfiles
from .agent import AgentResult, instructions, run_codex
from .codex_server import CodexServer, CodexServerError
from .config import PROJECT_ROOT, Config, ensure_dirs
from .db import Store, Task
from .forms import check_values, login_spec
from .jobs import JobError, Outcome, Runner, github_slug, parse_code_args, short
from .jobs import run as run_cmd
from .models import available_models, model_list
from .models import summary as models_summary
from .models import switch as switch_model
from .transcribe import TranscriptionError, model_for, transcribe
from .vault import Vault, check
from .watches import WatchBot

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000
FILES_PAGE = 8  # entries per /files page
IDLE_NEW_THREAD = timedelta(hours=6)  # start a fresh Codex thread after this much silence
# Also start one (carrying the last exchange) once the context is this big (AgentResult.input_tokens).
# Not after every browser turn: follow-ups ("거기 일요일엔?") need what the page said.
MAX_TURN_INPUT_TOKENS = 60_000
# A turn still running this long sends the agent's progress line (ProgressNote).
PROGRESS_AFTER = 8.0
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
FILES_ONLY = (
    "(The user sent only the files above, with no message. Look at them. If it's clear what they "
    "want done, do it. Otherwise say in a sentence or two what you see and what you think matters, "
    "and ask what they'd like you to do with them.)"
)
# Files answered on their own when at least one is an image (photo, screenshot).
IMAGES_ONLY = (
    "[The user sent the image(s) above without a message. Work out what it is. If it shows deadlines, "
    "assignments, exams, meetings or anything to remember, call propose_items with them (don't add them "
    "yourself); include submission for things handed in online. If it's unclear what they want, ask one "
    "short question. Otherwise describe it briefly.]"
)
# A browser approval stops counting after this: the tool waiting for it gives up at 5 minutes.
APPROVAL_EXPIRES = timedelta(minutes=5, seconds=10)
# A request the chat agent didn't wait for (mcp_server._follow_up) expires after this.
FOLLOWUP_EXPIRES = timedelta(minutes=30)
# Extra turns per message when the agent turns on a toolset (use_tools), so it gets the tools.
MAX_CONTINUATIONS = 2
# A one-line reply that only says a request was sent; the user sees the request itself.
REQUEST_ECHO = re.compile(r"보냈|보낼게|요청|입력|폼|양식|sent|request|form|fill in|enter", re.I)
# Before the message when a chat turn is retried with `codex exec` after the app-server died mid-turn.
RETRY_NOTE = ("[Your previous attempt at this message was cut off by an error after you had started using tools. "
              "Check what was already done (e.g. list_reminders, list_tasks) before doing anything again.]\n")
VOICE_NOTE = "[The user sent this as a voice message; it was transcribed automatically and may contain recognition errors.]\n"
HELP = """\
편하게 한국어나 영어로 말해주세요. 할 일과 마감, 리마인더, Google 캘린더 일정, 기억해 둘 것,
보내준 파일을 챙겨요. 음성 메시지도 받아 적어서 답해요.

/today - 오늘 일정, 할 일, 리마인더
/tasks - 남은 할 일과 마감
/reminders - 예정된 리마인더
/new - 새 대화 시작
/model [name] [effort] - Codex 모델 보기/바꾸기, 예: /model gpt-6-sol high
/code [claude|codex] <repo> <할 일> - 코딩 에이전트가 저장소를 고치고 풀 리퀘스트를 열어요.
  작업 메시지에 답장하면 추가 지시가 돼요.
/jobs - 최근 코딩 작업
/stop <번호> - 코딩 작업 중지
/runs - 예약된 브라우저 작업; /runs cancel <번호> 로 취소
/watches - things I check for you ("이러면 알려줘"): pause, resume or delete
/login <이름> <도메인> - 브라우저용 사이트 로그인 저장 (답장으로 보내면 메시지는 지워요)
/logins - 저장된 로그인: 다시 묻게 하거나 삭제
/tz [Area/City] - 시간대 보기/바꾸기
/files [폴더] - 파일 보기: 눌러서 받기·삭제
/backup - 데이터베이스 파일 받기
/status - 상태 확인"""

# The menu Telegram shows when you type "/".
COMMANDS = [
    ("today", "오늘 일정, 할 일, 리마인더"),
    ("tasks", "남은 할 일과 마감"),
    ("reminders", "예정된 리마인더"),
    ("model", "Codex 모델 보기/바꾸기"),
    ("code", "코딩 작업: /code [claude|codex] <repo> <할 일>"),
    ("jobs", "최근 코딩 작업"),
    ("stop", "코딩 작업 중지"),
    ("runs", "예약된 브라우저 작업"),
    ("watches", "Watches: what I check for you"),
    ("login", "사이트 로그인 저장: /login <이름> <도메인>"),
    ("logins", "저장된 로그인"),
    ("new", "새 대화 시작"),
    ("tz", "시간대 보기/바꾸기"),
    ("files", "파일 보기: 받기·삭제"),
    ("status", "상태 확인"),
    ("backup", "데이터베이스 파일 받기"),
    ("help", "Friday가 할 수 있는 것"),
]


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    chunks, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return chunks + ([rest] if rest else [])


MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def plain_text(text: str) -> str:
    """Telegram gets plain text: Markdown links become "text url" and **bold** loses its stars."""
    text = MD_LINK.sub(lambda m: m[2] if m[1].strip() == m[2] else f"{m[1]} {m[2]}", text)
    return MD_BOLD.sub(r"\1", text)


class ProgressNote:
    """The agent's "checking X" line from the middle of a turn, sent (once) only when the turn is
    still running PROGRESS_AFTER seconds in, so quick answers stay one message."""

    def __init__(self, send, after: float):
        self.send, self.after, self.start = send, after, clock.monotonic()
        self.text, self.task, self.sent = "", None, False

    def __call__(self, text: str) -> None:
        text = text.strip()
        if self.sent or not text.strip(" .-"):
            return
        self.text = text  # the latest line is the most current
        if self.task is None:
            self.task = asyncio.create_task(self._later())

    async def _later(self) -> None:
        await asyncio.sleep(max(0.0, self.after - (clock.monotonic() - self.start)))
        self.sent = True
        await self.send(self.text)

    def cancel(self) -> None:
        if self.task and not self.sent:
            self.task.cancel()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\-가-힣 ]+", "_", name).strip(" .") or "file"
    return name[:120]


class Friday(WatchBot):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        ensure_dirs(cfg)
        self.store = Store(cfg.db_path)
        self.agent_lock = asyncio.Lock()
        # Chat turns' Codex: one app-server process, started on the first message (codex_server.py)
        self.codex = CodexServer(cfg) if cfg.codex_server else None
        self.app: Application | None = None
        self._tasks: list[asyncio.Task] = []
        self.runner = Runner(cfg, self.store)
        self.own_repo: str | None = None  # Friday's own GitHub owner/name, for the Deploy button
        self._job_runner: asyncio.Task | None = None  # runs one job and reports it; one at a time
        self._job_id: int | None = None  # the job _job_runner is running
        self._job_exec: asyncio.Task | None = None  # the agent/git part of it, which /stop cancels
        self._job_busy: set[int] = set()  # jobs with a button action in flight (double taps)
        self._file_timer: asyncio.Task | None = None  # answers files sent without a message
        self._downloads = 0  # incoming files still downloading
        self._run_task: asyncio.Task | None = None  # the scheduled browser run in progress, if any
        self._run_id: int | None = None
        # /files buttons carry an index into this list, not a path (callback data is 64 bytes at most)
        self._fs_paths: list[str] = []
        self._fs_index: dict[str, int] = {}
        self._bg: set[asyncio.Task] = set()  # continuation turns after the owner answered a request

    # --- setup -------------------------------------------------------------

    def build(self) -> Application:
        app = (
            botapi.builder(self.cfg)  # the local Bot API server when TELEGRAM_API_ID is set
            .token(self.cfg.telegram_token)
            .concurrent_updates(True)
            .post_init(self._on_start)
            .post_shutdown(self._on_stop)
            .build()
        )
        me = filters.User(user_id=list(self.cfg.allowed_user_ids))
        app.add_handler(CommandHandler(["start", "help"], self.cmd_help, filters=me))
        app.add_handler(CommandHandler("new", self.cmd_new, filters=me))
        app.add_handler(CommandHandler("reminders", self.cmd_reminders, filters=me))
        app.add_handler(CommandHandler("tasks", self.cmd_tasks, filters=me))
        app.add_handler(CallbackQueryHandler(self.on_task_button, pattern=r"^task:(done|snooze):\d+$"))
        app.add_handler(CommandHandler("today", self.cmd_today, filters=me))
        app.add_handler(CommandHandler("tz", self.cmd_tz, filters=me))
        app.add_handler(CommandHandler("model", self.cmd_model, filters=me))
        app.add_handler(CommandHandler("backup", self.cmd_backup, filters=me))
        app.add_handler(CommandHandler("status", self.cmd_status, filters=me))
        app.add_handler(CommandHandler("code", self.cmd_code, filters=me))
        app.add_handler(CommandHandler("jobs", self.cmd_jobs, filters=me))
        app.add_handler(CommandHandler("stop", self.cmd_stop, filters=me))
        app.add_handler(CommandHandler("runs", self.cmd_runs, filters=me))
        app.add_handler(CallbackQueryHandler(self.on_approval_button, pattern=r"^appr:(yes|no):\d+$"))
        app.add_handler(CommandHandler("login", self.cmd_login, filters=me))
        app.add_handler(CommandHandler("logins", self.cmd_logins, filters=me))
        app.add_handler(CallbackQueryHandler(self.on_login_button, pattern=r"^login:cancel:\d+$"))
        app.add_handler(CallbackQueryHandler(self.on_vault_button, pattern=r"^vault:(untrust|del|delyes|delno):[A-Za-z0-9_.-]{1,40}$"))
        app.add_handler(CallbackQueryHandler(self.on_job_button, pattern=r"^job:(merge|close|deploy|undo):\d+$"))
        app.add_handler(CallbackQueryHandler(self.on_proposal_button, pattern=r"^prop:(accept|dismiss):\d+$"))
        app.add_handler(CallbackQueryHandler(self.on_submission_button, pattern=r"^sub:(remind|approve|auto):\d+:\d+$"))
        app.add_handler(CommandHandler("files", self.cmd_files, filters=me))
        app.add_handler(CallbackQueryHandler(self.on_files_button, pattern=r"^fs:(r|[dfgxy]:\d{1,6}(:\d{1,4})?)$"))
        self._watch_handlers(app, me)  # /watches and its buttons (watches.py)
        app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(me & (filters.LOCATION | filters.VENUE), self.on_location))
        attachments = filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VIDEO | filters.VOICE
        app.add_handler(MessageHandler(me & attachments, self.on_file))
        app.add_handler(MessageHandler(~me, self.on_stranger), group=1)
        app.add_error_handler(self.on_error)
        self.app = app
        return app

    async def _on_start(self, app: Application) -> None:
        try:
            await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in COMMANDS])
        except Exception:
            log.exception("could not register the command menu")
        try:
            await self._jobs_startup()
        except Exception:
            log.exception("coding jobs startup failed")
        try:
            await self._runs_startup()
        except Exception:
            log.exception("browser runs startup failed")
        for loop in (self.reminder_loop, self.outbox_loop, self.calendar_loop, self.brief_loop, self.job_loop,
                     self.browser_run_loop, self.watch_loop):
            self._tasks.append(asyncio.create_task(self._forever(loop), name=loop.__name__))
        log.info("Friday started; tz=%s, allowed=%s", self.tz, self.cfg.allowed_user_ids)

    async def _on_stop(self, app: Application) -> None:
        # A running job stays 'running' in the database; the next start marks it failed and says so.
        tasks = self._tasks + [t for t in (self._job_runner, self._run_task, self._watch_task) if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.codex:
            await self.codex.close()
        self.store.close()

    @staticmethod
    async def _forever(step) -> None:
        """Run a loop step forever, sleeping the seconds it returns; an exception is logged and
        retried a minute later instead of killing the loop."""
        while True:
            try:
                wait = await step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s failed", step.__name__)
                wait = 60
            await asyncio.sleep(wait or 30)

    @property
    def tz(self) -> str:
        return self.store.tz(self.cfg.default_tz)

    @property
    def owner(self) -> int:
        assert self.cfg.owner_chat_id is not None
        return self.cfg.owner_chat_id

    async def say(self, text: str, chat_id: int | None = None) -> None:
        for chunk in split_message(text):
            await self.app.bot.send_message(chat_id or self.owner, chunk)  # type: ignore[union-attr]

    # --- conversation ------------------------------------------------------

    def _wrap(self, text: str, files: list[str], new_thread: bool) -> str:
        head = [f"[Now: {tu.local_str(tu.now_utc(), self.tz)} ({self.tz})]"]
        raw = self.store.get("last_location")
        if raw:  # a location shared in the last 3 hours still answers "here" / "near me"
            loc = json.loads(raw)
            at = tu.from_iso(loc["at"])
            if tu.now_utc() - at < timedelta(hours=3):
                head.append(f"[User's last shared location ({tu.local_str(at, self.tz)}): {loc['where']}]")
        if files:
            head.append("[Files the user sent, saved in the workspace: " + ", ".join(files) + "]")
        has_image = any(Path(f).suffix.lower() in IMAGE_SUFFIXES for f in files)
        body = "\n".join(head) + "\n\n" + (text or (IMAGES_ONLY if has_image else FILES_ONLY))
        if not new_thread:
            return body
        context = []
        if mems := self.store.memories("", 20):
            context.append("[What you know about the user (saved memories, newest first; recall has more):\n"
                           + "\n".join(f"#{m['id']} {short(m['text'], 120)}" for m in mems) + "]")
        if carry := self.store.get("carry"):
            self.store.delete("carry")
            context.append(carry)
        prompt = instructions(self.cfg)
        self.store.set("prompt_hash", _digest(prompt))
        return "\n".join([f"{prompt}\n---", *context, body])

    def _current_thread(self) -> str | None:
        last = self.store.get("last_message_at")
        if last and tu.now_utc() - tu.from_iso(last) > IDLE_NEW_THREAD:
            self._reset_thread()
        return self.store.get("thread_id")

    def _reset_thread(self) -> None:
        """A fresh conversation: no Codex thread, and the optional toolsets off again."""
        for key in ("thread_id", "toolsets", "carry"):
            self.store.delete(key)

    def _note_exchange(self, said: str, replied: str) -> None:
        note = f"the user said «{short(' '.join(said.split()) or '(only files)', 300)}»"
        if replied.strip(" .\"'`-—"):
            note += f" and you replied «{short(' '.join(replied.split()), 300)}»"
        self.store.set("last_exchange", note)

    def _continue_fresh(self) -> None:
        """Go on in a new Codex thread that starts with a note of the last exchange (_wrap)."""
        if note := self.store.get("last_exchange"):
            self.store.set("carry", f"[Continuing this conversation in a fresh context. Last exchange: {note}.]")
        self.store.delete("thread_id")

    def _toolsets(self) -> set[str]:
        """Toolsets for a chat turn: the browser, and those this conversation turned on
        (mcp_server.use_tools)."""
        on = set(json.loads(self.store.get("toolsets") or "[]"))
        return on | {"browser"} if self.cfg.browser_enabled else on

    def _last_shown(self) -> int:
        return self.store.conn.execute("SELECT COALESCE(MAX(id), 0) FROM outbox WHERE kind = 'html'").fetchone()[0]

    def _last_proposal(self) -> int:
        return self.store.conn.execute("SELECT COALESCE(MAX(id), 0) FROM proposals").fetchone()[0]

    def _followups(self) -> set[str]:
        """Requests to the owner the chat agent didn't wait for (mcp_server._follow_up)."""
        return {r[0] for r in self.store.conn.execute("SELECT key FROM kv WHERE key LIKE 'followup:%'")}

    async def ask(self, chat_id: int, text: str, files: list[str] | None = None, images: list[Path] | None = None) -> None:
        files = (self._take_pending_files()) + (files or [])
        # Show Codex the images among the files, not just their paths.
        images = list(images or [])
        for f in files:
            path = self.cfg.workspace / f
            if path.suffix.lower() in IMAGE_SUFFIXES and path not in images:
                images.append(path)
        # A thread keeps the instructions it started with: after a deploy or a FRIDAY.md edit, the
        # next message goes on in a fresh one (not while a request's answer is due in this one).
        if (self.store.get("thread_id") and not self._followups()
                and self.store.get("prompt_hash") != _digest(instructions(self.cfg))):
            self._continue_fresh()
        await self._turn(chat_id, text, files, images)

    async def _turn(self, chat_id: int, text: str, files: list[str] | None = None,
                    images: list[Path] | None = None) -> None:
        """Answer a message (or continue after a note) in the chat's Codex thread. When the agent
        turns on a toolset (use_tools), the thread is continued right away with the toolset
        attached, at most MAX_CONTINUATIONS times; only the last turn's reply is sent."""
        async with self.agent_lock:
            typing = asyncio.create_task(self._keep_typing(chat_id))
            note = ProgressNote(lambda t: self._quiet(self.say(plain_text(t), chat_id)), PROGRESS_AFTER)
            requests, proposal, used = self._followups(), self._last_proposal(), set()
            shown = self._last_shown()
            try:
                thread = self._current_thread()
                prompt = self._wrap(text, files or [], thread is None)
                for step in range(MAX_CONTINUATIONS + 1):
                    toolsets = self._toolsets()
                    try:
                        result = await self._codex_turn(prompt, thread, (images or []) if step == 0 else [], toolsets,
                                                        note)
                    except Exception as e:  # e.g. codex binary missing
                        log.exception("codex run failed")
                        result = AgentResult("", thread, [repr(e)])
                    used |= result.mcp_servers
                    thread = result.thread_id or thread
                    if thread:
                        self.store.set("thread_id", thread)
                    self.store.set("last_message_at", tu.to_utc_iso(tu.now_utc()))
                    new = self._toolsets() - toolsets
                    if not new or not thread or step == MAX_CONTINUATIONS:
                        break
                    prompt = f"[{' and '.join(sorted(new))} tools are now available; continue the user's request]"
            finally:
                typing.cancel()
                note.cancel()
            made_request = bool(self._followups() - requests)
            self._note_exchange(text, result.text)
            # Not while a request is open: its answer continues this thread.
            if thread and not self._followups() and result.input_tokens > MAX_TURN_INPUT_TOKENS:
                self._continue_fresh()
        if result.text:
            # A one-liner next to a proposal or a tree (show_tree) only says it was sent.
            listed = self._last_proposal() > proposal or self._last_shown() > shown
            if self._worth_sending(result.text, made_request, listed):
                await self.say(plain_text(result.text), chat_id)
        elif result.errors or not made_request:
            detail = "; ".join(result.errors) or "no reply"
            await self.say(f"Codex 오류: {detail[:1500]}\n계속 이러면 /new 를 보내주세요.", chat_id)
        await self.outbox_loop()

    async def _codex_turn(self, prompt: str, thread: str | None, images: list[Path], toolsets: set[str],
                          on_note=None) -> AgentResult:
        """One chat turn: in the app-server, or with `codex exec` when it is off or failed."""
        kw = dict(model=self.store.get("model") or "", effort=self.store.get("effort") or "", toolsets=toolsets,
                  tz=self.tz)
        if self.codex:
            try:
                return await self.codex.run(prompt, thread, images, on_note=on_note, **kw)
            except CodexServerError as e:
                log.warning("codex app-server failed, retrying with codex exec: %s", e)
                if e.partial:
                    prompt = RETRY_NOTE + prompt
        return await run_codex(self.cfg, prompt, thread, images, chat=True, **kw)

    @staticmethod
    def _worth_sending(text: str, made_request: bool, made_proposal: bool = False) -> bool:
        """Not "-" (the agent's "nothing to add"), and not a one-liner that only says a request
        was sent when the turn sent one, or any one-liner next to a proposal (the user sees the
        request or the list itself)."""
        t = text.strip()
        if t.strip(" .\"'`") in ("", "-", "—"):
            return False
        one_line = "\n" not in t and len(t) <= 120
        return not (one_line and (made_proposal or (made_request and REQUEST_ECHO.search(t))))

    async def _keep_typing(self, chat_id: int) -> None:
        while True:
            if not self._waiting_on_owner():  # a tool is waiting for the owner's tap, not working
                try:
                    await self.app.bot.send_chat_action(chat_id, ChatAction.TYPING)  # type: ignore[union-attr]
                except Exception:
                    pass
            await asyncio.sleep(4)

    def _waiting_on_owner(self) -> bool:
        """Is a tool waiting for an answer to a recent approval or form (not one it didn't wait for)?"""
        since = tu.to_utc_iso(tu.now_utc() - APPROVAL_EXPIRES)
        rows = self.store.conn.execute(
            "SELECT 'approval', id FROM approvals WHERE status = 'pending' AND created_at > ? "
            "UNION ALL SELECT 'login', id FROM login_requests WHERE status = 'pending' AND created_at > ?",
            (since, since)).fetchall()
        return any(self.store.get(f"followup:{kind}:{rid}") is None for kind, rid in rows)

    # --- requests the chat agent didn't wait for (mcp_server._follow_up) ------

    async def _request_done(self, kind: str, rid: int) -> None:
        """A request from a chat turn (kind 'login': request_form/login/code, 'approval':
        request_approval) was answered, cancelled or expired: continue the chat thread with the
        outcome. Nothing happens for requests a tool waited for, or after /new."""
        key = f"followup:{kind}:{rid}"
        raw = self.store.get(key)
        if raw is None:
            return
        self.store.delete(key)
        note = self._request_outcome(kind, rid, json.loads(raw)["tool"])
        if not self._current_thread():
            return
        task = asyncio.create_task(self._turn(self.owner, note))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _request_outcome(self, kind: str, rid: int, tool: str) -> str:
        """What the agent learns: like the tool's result when it waited. Sensitive values are only
        named ("stored as …"); the form's other values are read once and erased (take_result)."""
        if kind == "approval":
            status = self.store.approval(rid)["status"]
            answer = {"approved": "approved: go ahead with exactly that step",
                      "denied": "denied: don't do it"}.get(status, "not answered in time: don't do it")
            return f"[Your request_approval was {answer}. Continue the user's request.]"
        r = self.store.login_request(rid)
        spec = self._request_spec(r)
        values = self.store.take_result(rid)
        if r["status"] == "cancelled":
            outcome = "the user cancelled it"
        elif r["status"] != "saved":
            outcome = f"no answer within {FOLLOWUP_EXPIRES.seconds // 60} minutes; it expired"
        elif tool == "request_code":
            outcome = f"Code: {values.get('code', '')}"
        elif tool == "request_login":
            outcome = (f"Saved login {r['name']} for {r['domain']} (fill_login with that name won't ask "
                       "again on that site)") + (f". Other values: {json.dumps(values, ensure_ascii=False)}"
                                                   if values else "")
        else:
            values.update({f["name"]: f"stored as {r['name']}.{f['name']}" for f in spec["fields"] if f["sensitive"]})
            outcome = "Submitted: " + json.dumps(values, ensure_ascii=False)
        return f"[Answer to your {tool} ({spec['title']}): {outcome}. Continue the user's request.]"

    async def _expire_followups(self) -> None:
        now = tu.now_utc()
        for key in self._followups():
            info = json.loads(self.store.get(key) or "{}")
            if not info or now - tu.from_iso(info["at"]) < FOLLOWUP_EXPIRES:
                continue
            _, kind, raw = key.split(":")
            rid = int(raw)
            if kind == "login" and self.store.finish_login_request(rid, "expired"):
                await self._close_login_request(rid, "⌛ 시간이 지나 요청이 끝났어요")
            elif kind == "approval":
                self.store.decide_approval(rid, "expired")
            await self._request_done(kind, rid)

    def _take_pending_files(self) -> list[str]:
        raw = self.store.get("pending_files")
        self.store.delete("pending_files")
        return json.loads(raw) if raw else []

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        # A reply to a login request holds a password: it goes to the vault, never to the agent.
        req = self.store.login_request_for_message(msg.reply_to_message.message_id) if msg.reply_to_message else None
        if req:
            await self._login_reply(msg, req)
            return
        # A reply to a message about a coding job continues that job instead of the chat.
        jid = self.store.job_for_message(msg.reply_to_message.message_id) if msg.reply_to_message else None
        job = self.store.job(jid) if jid else None
        if job:
            await self._job_followup(msg, job)
            return
        # A photo sent just before this message may still be downloading; let it join this message.
        waited = 0.0
        while self._downloads and waited < self.cfg.file_download_wait_seconds:
            await asyncio.sleep(0.25)
            waited += 0.25
        await self.ask(update.effective_chat.id, self._reply_context(msg) + msg.text)

    async def on_location(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A shared location (or venue): remembered for 3 hours and passed to the agent with the
        next messages (_wrap), so "this cafe" or "near me" can be answered; on its own it gets a 👍,
        not a turn. Live-location updates arrive as edits and are
        ignored; the first position is enough for a question."""
        msg = update.message
        loc = msg.venue.location if msg.venue else msg.location
        where = f"{loc.latitude:.6f},{loc.longitude:.6f}"
        place = f" ({msg.venue.title}, {msg.venue.address})" if msg.venue else ""
        self.store.set("last_location", json.dumps({"at": tu.to_utc_iso(tu.now_utc()), "where": where + place}))
        if not msg.caption:  # usually the question follows; _wrap adds the location to it
            await self._quiet(self.app.bot.set_message_reaction(msg.chat_id, msg.message_id, "👍"))  # type: ignore[union-attr]
            return
        note = (f"[The user shared their location: {where}{place}. Map link: "
                f"https://maps.google.com/?q={where}. Use it for anything about where they are.]")
        await self.ask(msg.chat_id, self._reply_context(msg) + note + "\n" + msg.caption)

    @staticmethod
    def _reply_context(msg) -> str:
        """A line telling the agent which message the owner replied to (the quoted part if they
        quoted one), so "this" or "that one" makes sense. Empty when it isn't a reply."""
        r = msg.reply_to_message
        if not r:
            return ""
        quote = getattr(msg, "quote", None)
        text = (quote.text if quote and quote.text else "") or r.text or r.caption or ""
        if not text:
            text = "(a photo or file)" if (r.photo or r.document or r.audio or r.video or r.voice) else "(no text)"
        text = " ".join(text.split())
        text = text if len(text) <= 300 else text[:300] + "…"
        own = r.from_user and msg.from_user and r.from_user.id == msg.from_user.id
        who = "their own earlier message" if own else "your (Friday's) message"
        return f"[The user is replying to {who}: «{text}»]\n"

    async def on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        stamp = datetime.now(ZoneInfo(self.tz)).strftime("%H%M%S")
        is_photo = bool(msg.photo)
        if is_photo:
            media, name = msg.photo[-1], f"photo_{stamp}.jpg"
        else:
            media = msg.document or msg.audio or msg.video or msg.voice
            default = {"audio": "audio.mp3", "video": "video.mp4", "voice": "voice.ogg"}
            kind = "audio" if msg.audio else "video" if msg.video else "voice" if msg.voice else "file"
            name = getattr(media, "file_name", None) or default.get(kind, "file")
            name = f"{stamp}_{safe_name(name)}"
            mime = getattr(media, "mime_type", None) or ""
            ext = mimetypes.guess_extension(mime) if mime.startswith("image/") else None
            if ext in IMAGE_SUFFIXES and Path(name).suffix.lower() not in IMAGE_SUFFIXES:
                name += ext  # an image sent as a file without its extension: still show it as an image
        if media.file_size and media.file_size > botapi.download_limit(self.cfg):
            await msg.reply_text("텔레그램 봇은 20MB까지만 받을 수 있어요. 더 큰 파일은 scp로 복사해주세요."
                                 if not self.cfg.local_botapi else "2GB가 넘어서 텔레그램 봇이 받을 수 없어요.")
            return

        day_dir = self.cfg.files_dir / "inbox" / datetime.now(ZoneInfo(self.tz)).strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        dest = day_dir / name
        self._downloads += 1
        try:
            await self._download(media, dest)
        finally:
            self._downloads -= 1
        rel = str(dest.relative_to(self.cfg.workspace))

        if msg.voice or msg.audio:
            await self._on_recording(msg, dest, rel)
            return
        if msg.caption:
            await self.ask(msg.chat_id, msg.caption, files=[rel])
        else:
            self._queue_files(msg.chat_id, [rel])

    def _queue_files(self, chat_id: int, rels: list[str]) -> None:
        """Hold files sent without a message for cfg.file_followup_wait_seconds, then answer them on their own.
        A message typed in the meantime takes them instead (ask() empties pending_files), and each
        new file restarts the wait, so an album or a photo followed by a question gets one reply."""
        self.store.set("pending_files", json.dumps(self._take_pending_files() + rels))
        if self._file_timer and not self._file_timer.done():
            self._file_timer.cancel()
        self._file_timer = asyncio.create_task(self._answer_files_later(chat_id))

    async def _answer_files_later(self, chat_id: int) -> None:
        await asyncio.sleep(self.cfg.file_followup_wait_seconds)
        if self.store.get("pending_files"):
            await self.ask(chat_id, "")

    async def _on_recording(self, msg, dest: Path, rel: str) -> None:
        """A voice message is treated as something the user said; an audio file as a file with a
        transcript next to it. Codex can't listen, so it only ever gets the text."""
        if not self.cfg.transcription_on:
            self.store.set("pending_files", json.dumps(self._take_pending_files() + [rel]))
            await msg.reply_text(
                f"{rel} 에 저장했어요. 아직 음성을 들을 수 없어요: 서버에 TRANSCRIBE_API_KEY 를 설정하면 "
                "음성 메시지를 받아 적어요."
            )
            return

        typing = asyncio.create_task(self._keep_typing(msg.chat_id))
        try:
            text = await transcribe(
                dest,
                provider=self.cfg.transcribe_provider,
                api_key=self.cfg.transcribe_api_key,
                model=self.cfg.transcribe_model,
                language=self.cfg.transcribe_language,
                timeout_seconds=self.cfg.transcribe_timeout_seconds,
            )
        except TranscriptionError as e:
            log.warning("Transcription of %s failed: %s", rel, e)
            self.store.set("pending_files", json.dumps(self._take_pending_files() + [rel]))
            await msg.reply_text(f"{rel} 에 저장했지만 받아 적지 못했어요 ({e}). 글로 보내주시겠어요?")
            return
        finally:
            typing.cancel()

        if not text:
            await msg.reply_text("녹음에서 말을 알아듣지 못했어요.")
            return
        transcript = dest.with_name(dest.name + ".txt")
        transcript.write_text(text + "\n", encoding="utf-8")
        transcript_rel = str(transcript.relative_to(self.cfg.workspace))

        if msg.voice:  # answered like a typed message; the transcript stays in the workspace
            spoken = f"{msg.caption}\n\n{text}" if msg.caption else text
            await self.ask(msg.chat_id, VOICE_NOTE + spoken)
        elif msg.caption:
            await self.ask(msg.chat_id, msg.caption, files=[rel, transcript_rel])
        else:
            self._queue_files(msg.chat_id, [rel, transcript_rel])

    async def on_stranger(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message and user and not self.cfg.allowed_user_ids:
            await update.message.reply_text(
                f"Your Telegram user id is {user.id}. Put it in TELEGRAM_ALLOWED_USER_IDS and restart Friday."
            )
        elif user:
            log.warning("Ignored message from unknown user %s (@%s)", user.id, user.username)

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("Update failed", exc_info=ctx.error)

    # --- commands ----------------------------------------------------------

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(HELP)

    async def cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self._reset_thread()
        await update.message.reply_text("새 대화를 시작했어요. 기억, 할 일, 리마인더는 그대로예요.")

    async def cmd_reminders(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        rs = self.store.active_reminders()
        await self.say("\n".join(r.describe(self.tz) for r in rs) or "예정된 리마인더가 없어요.", update.effective_chat.id)

    async def cmd_tasks(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        now, zone = tu.now_utc(), ZoneInfo(self.tz)
        lines = []
        for t in self.store.open_tasks():
            due = ""
            if t.due_at:
                due = ("⚠️ 지남 " if t.due_at < now else "") + f"{t.due_at.astimezone(zone):%a %m/%d %H:%M}  "
            lines.append(f"#{t.id} {due}{t.title}")
        text = "\n".join(lines) or "남은 할 일이 없어요. 🎉"
        await self.say(text + ("\n\n\"#3 다 했어\", \"보고서 끝냈어\"처럼 말하면 완료로 바꿔요." if lines else ""),
                       update.effective_chat.id)

    async def on_task_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q.from_user or q.from_user.id not in self.cfg.allowed_user_ids:
            await q.answer()
            return
        _, action, tid = q.data.split(":")
        t = self.store.task(int(tid))
        if action == "done":
            ok = self.store.complete_task(int(tid))
            await q.answer("완료 ✅" if ok else "이미 끝난 할 일이에요")
            if t:
                await q.edit_message_text(f"✅ {t.title}")
        else:  # snooze an hour
            ok = bool(self.store.update_task(int(tid), remind=tu.now_utc() + timedelta(hours=1)))
            await q.answer("1시간 뒤에 다시 알려드릴게요" if ok else "이미 끝난 할 일이에요")
            if t and ok:
                await q.edit_message_text(f"⏳ {t.title} (1시간 뒤 다시 알림)")

    async def _send_task_alert(self, text: str, t: Task) -> None:
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 완료", callback_data=f"task:done:{t.id}"),
            InlineKeyboardButton("⏳ 1시간 뒤", callback_data=f"task:snooze:{t.id}"),
        ]])
        await self.app.bot.send_message(self.owner, text, reply_markup=buttons)  # type: ignore[union-attr]

    async def cmd_today(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.say(await self.brief(), update.effective_chat.id)

    async def cmd_tz(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if ctx.args:
            try:
                ZoneInfo(ctx.args[0])
            except Exception:
                await update.message.reply_text("모르는 시간대예요. Asia/Seoul, Europe/Paris 같은 이름으로 써주세요.")
                return
            self.store.set("tz", ctx.args[0])
        await update.message.reply_text(f"시간대: {self.tz}. 지금: {tu.local_str(tu.now_utc(), self.tz)}")

    def _model_summary(self) -> str:
        return models_summary(self.store, self.cfg.codex_model, self.cfg.codex_effort)

    async def cmd_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        models = available_models()
        args = [a.strip() for a in (ctx.args or []) if a.strip()][:2]
        try:
            note = switch_model(self.store, self.cfg.codex_model, args, models) if args else ""
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        text = self._model_summary() + note
        if not args:
            text += "\n\n" + model_list(models)
        await update.message.reply_text(text)

    async def cmd_backup(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / f"friday-{datetime.now(ZoneInfo(self.tz)):%Y%m%d-%H%M}.db"
            dst = sqlite3.connect(copy)
            self.store.conn.backup(dst)
            dst.close()
            with copy.open("rb") as fh:
                await update.message.reply_document(fh, filename=copy.name, caption="Friday 데이터베이스 백업")

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        proc = await asyncio.create_subprocess_exec(
            self.cfg.codex_bin, "login", "status",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        codex = (out.decode(errors="replace").strip().splitlines() or ["?"])[-1]
        extra = mcp_config.load(self.cfg.mcp_config)
        lines = [
            f"Codex: {codex}",
            self._model_summary(),
            f"Web search: {self.cfg.web_search}",
            "GitHub: " + (
                ("connected" + (" (read-only)" if self.cfg.github_readonly else " (read and write)"))
                if self.cfg.github_token_set else "not connected"
            ),
            f"Google Calendar: {'connected' if gcal.is_connected(self.cfg.google_token) else 'not connected'}",
            "Voice transcription: " + (
                f"{self.cfg.transcribe_provider} ({model_for(self.cfg.transcribe_provider, self.cfg.transcribe_model)})"
                if self.cfg.transcription_on else "off"
            ),
            "Extra MCP servers: " + (", ".join(extra.names) or "none"),
            *(f"  ⚠ {p}" for p in extra.problems),
            f"Timezone: {self.tz}",
            f"Upcoming reminders: {len(self.store.active_reminders())}",
            f"Conversation: {'ongoing' if self.store.get('thread_id') else 'fresh'}",
            f"Coding jobs: {self.cfg.code_engine} by default, "
            + (f"#{self._running_job_id()} running" if self._running_job_id() else "idle"),
        ]
        await update.message.reply_text("\n".join(lines))

    # --- coding jobs -------------------------------------------------------

    async def cmd_code(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        # Split the raw text ourselves (not ctx.args) so a multi-line task keeps its line breaks.
        rest = (update.message.text or "").split(maxsplit=1)[1:]
        rest = rest[0] if rest else ""
        first = rest.split(maxsplit=1)
        has_engine = bool(first) and first[0].lower() in ("claude", "codex")
        try:
            engine, repo, task = parse_code_args(rest.split(maxsplit=2 if has_engine else 1), self.cfg.code_engine)
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        jid = self.store.add_job(repo, task, engine)
        busy = self._running_job_id()
        note = f" 작업 #{busy}이 끝나면 시작해요." if busy else ""
        msg = await update.message.reply_text(f"🛠 작업 #{jid} 대기 중: {repo} ({engine})…{note}")
        self.store.link_job_message(msg.message_id, jid)
        self.store.set(f"job_queued_msg:{jid}", str(msg.message_id))  # _run_job edits it into "started"

    async def cmd_jobs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        lines = []
        for j in self.store.recent_jobs(10):
            line = f"#{j['id']} {j['status']} {j['repo']} ({j['engine']}) – {short(j['task'], 50)}"
            lines.append(line + (f" – {j['pr_url']}" if j["pr_url"] else ""))
        await self.say("\n".join(lines) or "아직 코딩 작업이 없어요. /code <repo> <할 일> 로 시작해요.",
                       update.effective_chat.id)

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        arg = (ctx.args or [""])[0].lstrip("#")
        jid = int(arg) if arg.isdigit() else None
        if not arg:  # plain /stop: the running job
            jid = self._running_job_id()
        job = self.store.job(jid) if jid else None
        if not job:
            await update.message.reply_text("사용법: /stop <작업 번호>. 목록은 /jobs")
            return
        if job["status"] == "queued":
            self.store.update_job(jid, status="cancelled", followup=None)
            await update.message.reply_text(f"⏹ 작업 #{jid}을 시작 전에 취소했어요.")
        elif job["status"] == "running" and jid == self._running_job_id() and self._job_exec:
            # _run_job sees the cancellation, kills the agent's processes and reports it.
            self._job_exec.cancel()
        elif job["status"] == "running":  # not ours to cancel (shouldn't happen); don't leave it stuck
            self.store.update_job(jid, status="cancelled")
            await update.message.reply_text(f"⏹ 작업 #{jid}을 취소로 표시했어요.")
        else:
            await update.message.reply_text(f"작업 #{jid}은 실행 중이 아니에요 (상태: {job['status']}).")

    async def _job_followup(self, msg, job) -> None:
        jid, status = job["id"], job["status"]
        if status in ("queued", "running"):
            await msg.reply_text(f"작업 #{jid}은 아직 {status} 상태예요. 끝나면 다시 답장하거나 /stop {jid}")
        elif status in ("review", "done", "failed"):
            self.store.update_job(jid, followup=msg.text, status="queued")
            sent = await msg.reply_text(f"↩️ 작업 #{jid} 후속 지시 대기 중")
            self.store.link_job_message(sent.message_id, jid)
            self.store.set(f"job_queued_msg:{jid}", str(sent.message_id))
        else:
            await msg.reply_text(f"작업 #{jid}은 {status} 상태예요. 새 작업은 /code {job['repo']} <할 일>")

    async def on_job_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q.from_user or q.from_user.id not in self.cfg.allowed_user_ids:
            await q.answer()
            return
        _, action, raw = q.data.split(":")
        jid = int(raw)
        job = self.store.job(jid)
        if not job:
            await q.answer("없는 작업이에요")
            return
        if jid in self._job_busy:
            await q.answer("처리 중이에요…")
            return
        self._job_busy.add(jid)
        try:
            if action == "deploy":
                await self._deploy_button(q, job)
                return
            if action == "undo":
                if job["status"] != "done" or not job["commits"]:
                    await q.answer(f"작업 #{jid}은 되돌릴 게 없어요.")
                    return
                await q.answer("되돌리는 중…")
                await self._quiet(q.edit_message_reply_markup(None))
                await self.runner.undo(job)
                self.store.update_job(jid, status="undone")
                await self._job_send(jid, f"↩️ 작업 #{jid} 되돌림: {job['repo']}의 변경을 새 커밋으로 되돌렸어요 "
                                          "(기록에는 둘 다 남아요).")
                return
            if job["status"] != "review":
                await q.answer(f"작업 #{jid}은 {job['status']} 상태예요.")
                await self._quiet(q.edit_message_reply_markup(None))
                return
            # Answer now: merging can take longer than Telegram waits for a callback answer.
            await q.answer("머지하는 중…" if action == "merge" else "닫는 중…")
            await self._quiet(q.edit_message_reply_markup(None))
            if action == "merge":
                await self.runner.merge(job)
                self.store.update_job(jid, status="merged")
                own = self._is_own_repo(job)
                buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 배포", callback_data=f"job:deploy:{jid}"),
                ]]) if own else None
                hint = "\n배포하면 Friday가 이 버전으로 재시작해요." if own else ""
                await self._job_send(jid, f"🔀 #{jid} 머지함: {job['pr_url']}{hint}", buttons)
            else:
                await self.runner.close(job)
                self.store.update_job(jid, status="closed")
                await self._job_send(jid, f"🗑 #{jid} 닫음: 풀 리퀘스트를 닫고 브랜치를 지웠어요.")
        except Exception as e:
            if not isinstance(e, JobError):
                log.exception("job button %s failed", q.data)
            detail = str(e) if isinstance(e, JobError) else f"{type(e).__name__}: {e}"
            await self._quiet(q.answer("실패"))  # no-op if already answered
            await self._job_send(jid, f"❌ #{jid} {action} 실패: {detail}")
        finally:
            self._job_busy.discard(jid)

    async def _deploy_button(self, q, job) -> None:
        jid = job["id"]
        if job["status"] != "merged" or not self._is_own_repo(job):
            await q.answer("배포할 게 없어요.")
            return
        running = self._running_job_id()
        if running:  # the restart would kill it
            await q.answer(f"작업 #{running}이 실행 중이에요. 끝난 뒤에 배포하거나 /stop 하세요.", show_alert=True)
            return
        await q.answer("배포하는 중…")
        unit = f"friday-deploy-{datetime.now():%Y%m%d-%H%M%S}"
        # deploy.sh restarts Friday, so it runs as its own systemd unit (outside friday.service, which
        # the restart kills), and the next start reads this flag to say it's back.
        self.store.set("deploy_notify", json.dumps({"job": jid, "chat": q.message.chat_id if q.message else None}))
        try:
            code, out, err = await run_cmd(
                ["systemd-run", "--user", "--collect", "--unit", unit, "bash", str(PROJECT_ROOT / "deploy" / "deploy.sh")],
                timeout=30,
            )
        except OSError as e:
            code, out, err = 1, "", str(e)
        if code != 0:
            self.store.delete("deploy_notify")
            raise JobError(f"couldn't start the deploy: {(err or out).strip()[-300:]}")
        text = (q.message.text if q.message and q.message.text else f"🔀 Merged #{jid}")
        await self._quiet(q.edit_message_text(
            f"{text}\n\n🚀 배포하는 중… 돌아오면 알려드릴게요. 소식이 없으면: journalctl --user -u {unit}"
        ))

    def _is_own_repo(self, job) -> bool:
        return bool(self.own_repo and job["gh_repo"] and job["gh_repo"].lower() == self.own_repo.lower())

    def _running_job_id(self) -> int | None:
        return self._job_id if self._job_runner and not self._job_runner.done() else None

    async def _jobs_startup(self) -> None:
        try:
            code, out, _ = await run_cmd(["git", "remote", "get-url", "origin"], cwd=PROJECT_ROOT, timeout=30)
            self.own_repo = github_slug(out) if code == 0 else None
        except OSError:
            self.own_repo = None

        raw = self.store.get("deploy_notify")
        if raw:
            try:
                chat = json.loads(raw).get("chat")
                _, out, _ = await run_cmd(["git", "log", "-1", "--format=%h %s"], cwd=PROJECT_ROOT, timeout=30)
                await self.app.bot.send_message(chat or self.owner, f"🚀 배포 완료: {out.strip() or '?'}")  # type: ignore[union-attr]
            finally:
                self.store.delete("deploy_notify")

        for jid in self.store.fail_interrupted_jobs():
            job = self.store.job(jid)
            await self._job_send(jid, f"⚠️ Friday가 재시작돼서 작업 #{jid} ({job['repo']})이 멈췄어요.\n"
                                      "다시 하려면 이 메시지에 답장으로 지시를 보내주세요.")

    async def _job_send(self, jid: int, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        """Message the owner about a job and remember the message ids, so a reply to any of them continues it."""
        chunks = split_message(text)
        try:
            for i, chunk in enumerate(chunks):
                last = i == len(chunks) - 1
                m = await self.app.bot.send_message(self.owner, chunk, reply_markup=markup if last else None)  # type: ignore[union-attr]
                self.store.link_job_message(m.message_id, jid)
        except Exception:  # the job's state is already saved; /jobs still shows it
            log.exception("could not send the message for job %s", jid)

    @staticmethod
    async def _quiet(coro) -> None:
        """Best-effort Telegram calls (editing an old message, a second answer) that mustn't stop the action."""
        try:
            await coro
        except Exception as e:
            log.info("ignored Telegram error: %s", e)

    async def job_loop(self) -> float:
        # Polled, because the MCP server (another process) can queue jobs too.
        if self._running_job_id():
            return 3
        job = self.store.next_queued_job()
        if job:
            self.store.update_job(job["id"], status="running", error=None)
            self._job_id = job["id"]
            self._job_runner = asyncio.create_task(self._run_job(self.store.job(job["id"])), name=f"job-{job['id']}")
        return 3

    async def _run_job(self, job) -> None:
        """Run one job and report it. Never raises (except when Friday shuts down), so the loop goes on."""
        jid = job["id"]
        head = f"#{jid} {job['repo']} ({job['engine']})"
        what = f"후속 지시: {short(job['followup'], 200)}" if job["followup"] else short(job["task"], 200)
        started = f"🛠 작업 {head} 시작: {what}"
        queued = self.store.get(f"job_queued_msg:{jid}")  # the /code or follow-up message: edit it
        self.store.delete(f"job_queued_msg:{jid}")
        try:
            if not queued:
                raise LookupError
            await self.app.bot.edit_message_text(started, chat_id=self.owner, message_id=int(queued))  # type: ignore[union-attr]
        except Exception:  # no such message (a job from the chat agent), or it can't be edited
            await self._job_send(jid, started)
        self._job_exec = asyncio.create_task(self.runner.execute(job))
        try:
            outcome = await self._job_exec
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():  # Friday is stopping, not /stop: next start reports it
                raise
            await self._stopped_job(jid, head)
            return
        except JobError as e:
            outcome = Outcome("failed", str(e))
        except Exception as e:
            log.exception("job %s crashed", jid)
            outcome = Outcome("failed", f"{type(e).__name__}: {e}")
        finally:
            self._job_exec = None

        try:
            failed = outcome.status == "failed"
            self.store.update_job(jid, status=outcome.status, error=outcome.message if failed else None)
            buttons = None
            if outcome.status == "review":
                text = f"✅ 작업 {head} 검토 준비됨:\n\n{outcome.message}"
                buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔀 머지", callback_data=f"job:merge:{jid}"),
                    InlineKeyboardButton("🗑 닫기", callback_data=f"job:close:{jid}"),
                ]])
            elif failed:
                row = self.store.job(jid)
                summary = row["summary"] if row else None
                text = f"❌ 작업 {head} 실패: {outcome.message}"
                if summary and summary not in outcome.message:
                    text += f"\n\n에이전트의 말:\n{summary}"
            else:
                text = f"✅ 작업 {head} 완료:\n\n{outcome.message}"
                if outcome.undo:  # a local project changed in place: offer to revert the job's commits
                    buttons = InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ 되돌리기", callback_data=f"job:undo:{jid}"),
                    ]])
            await self._job_send(jid, text + "\n\n이 메시지에 답장하면 추가 지시를 줄 수 있어요.", buttons)
        except Exception:
            log.exception("reporting job %s failed", jid)

    async def _stopped_job(self, jid: int, head: str) -> None:
        try:
            self.store.update_job(jid, status="cancelled", followup=None)
            job = self.store.job(jid)
            await self.runner.cleanup(job)  # cancelled jobs can't be resumed, so drop the worktree
            note = f"\n풀 리퀘스트는 아직 열려 있어요: {job['pr_url']}" if job["pr_number"] else ""
            await self._job_send(jid, f"⏹ 작업 {head} 중지됨.{note}")
        except Exception:
            log.exception("cleaning up stopped job %s failed", jid)

    # --- browser (approvals from the MCP tools, scheduled runs) -------------

    async def _send_approval(self, aid: int, shot: str | None) -> None:
        """A question from fill_login / request_approval / schedule_browser_run, with buttons.
        The tool waits for the answer in the approvals table."""
        a = self.store.approval(aid)
        if not a or a["status"] != "pending":
            return
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 허용", callback_data=f"appr:yes:{aid}"),
            InlineKeyboardButton("❌ 거절", callback_data=f"appr:no:{aid}"),
        ]])
        path, text = Path(shot) if shot else None, a["text"]
        if path and path.is_file():  # captions are limited to 1024 characters
            with path.open("rb") as fh:
                await self.app.bot.send_photo(self.owner, fh, caption=text[:1000], reply_markup=buttons)  # type: ignore[union-attr]
        else:
            await self.app.bot.send_message(self.owner, text[:TELEGRAM_LIMIT], reply_markup=buttons)  # type: ignore[union-attr]

    async def on_approval_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q.from_user or q.from_user.id not in self.cfg.allowed_user_ids:
            await q.answer()
            return
        _, answer, raw = q.data.split(":")
        aid = int(raw)
        a = self.store.approval(aid)
        if not a:
            await q.answer("없는 요청이에요")
            return
        # The tool stops waiting after 5 minutes (or dies with its run); a later tap mustn't count.
        # A chat request nobody waits for (followup) lasts longer; _expire_followups ends it.
        late = (a["status"] == "pending" and self.store.get(f"followup:approval:{aid}") is None
                and tu.now_utc() - tu.from_iso(a["created_at"]) > APPROVAL_EXPIRES)
        if late:
            self.store.decide_approval(aid, "expired")
        decided = self.store.decide_approval(aid, "approved" if answer == "yes" else "denied")
        label = {"approved": "✅ 허용됨", "denied": "❌ 거절됨", "expired": "⌛ 시간 초과"}[self.store.approval(aid)["status"]]
        await q.answer(label if decided else f"이미 처리됨: {label}")
        if not (decided or late):  # a double tap: the message already shows the answer
            await self._quiet(q.edit_message_reply_markup(None))
        elif q.message and q.message.caption is not None:
            await self._quiet(q.edit_message_caption(f"{q.message.caption}\n\n{label}"))
        elif q.message:
            await self._quiet(q.edit_message_text(f"{q.message.text}\n\n{label}"))
        if decided:
            await self._request_done("approval", aid)

    # --- forms and website logins (request_form/login/code, /login, /logins) ---

    @staticmethod
    def _request_spec(r) -> dict:
        spec = json.loads(r["spec"] or "{}")
        return spec if spec.get("fields") else login_spec(r["name"], r["domain"], r["reason"])  # /login

    def _request_title(self, r) -> str:
        return self._request_spec(r)["title"]

    async def _send_login_request(self, rid: int) -> None:
        """Send a form request (request_form, request_login, request_code, /login). The owner
        replies to it, one field per line (on_text, _login_reply). The text says where sensitive
        values go: it comes from Friday, not from the agent's wording, so a phishing attempt shows."""
        r = self.store.login_request(rid)
        if not r or r["status"] != "pending" or r["message_id"]:
            return
        spec = self._request_spec(r)
        icon = {"login": "🔑", "code": "🔢"}.get(r["kind"], "📝")
        why = f" ({short(' '.join(spec['description'].split()), 120)})" if spec["description"] else ""
        lines = [f"{icon} {spec['title']}{why}"]
        secret = [f["label"] for f in spec["fields"] if f["sensitive"]]
        if secret:
            where = f"used only on {spec['site']}" if spec["site"] else "not tied to any site"
            lines.append(f"🔐 {', '.join(secret)}: stored only in the vault as '{r['name']}', never shown to "
                         f"the AI ({where}).")
        lines.append("Reply to this message with one value per line; I delete your reply right away:")
        lines += [f"{i}. {self._field_hint(f)}" for i, f in enumerate(spec["fields"], 1)]
        cancel = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"login:cancel:{rid}")]])
        m = await self.app.bot.send_message(self.owner, "\n".join(lines)[:TELEGRAM_LIMIT],  # type: ignore[union-attr]
                                            reply_markup=cancel)
        self.store.set_login_request_message(rid, m.message_id)

    @staticmethod
    def _field_hint(f: dict) -> str:
        """One line of a request message: the field's label and what a valid answer looks like."""
        hint = {"checkbox": "yes or no", "date": "YYYY-MM-DD", "time": "HH:MM"}.get(f["type"], "")
        if f.get("options"):
            hint = " / ".join(o["label"] for o in f["options"])
        extra = [h for h in (hint, f.get("help", ""), "" if f["required"] else "optional, empty line to skip") if h]
        return f["label"] + (f" ({'; '.join(extra)})" if extra else "")

    async def _close_login_request(self, rid: int, text: str, chat_id: int | None = None) -> None:
        """Show how a request ended by editing its message."""
        r = self.store.login_request(rid)
        if r and r["message_id"]:
            await self._quiet(self.app.bot.edit_message_text(  # type: ignore[union-attr]
                text, chat_id=chat_id or self.owner, message_id=r["message_id"]))

    def _save_answer(self, req, values: dict) -> tuple[bool, str]:
        """Route what the owner typed (form or reply), by the request's own spec: sensitive values
        go to the vault under the request's name (trusted for its site, which the form showed),
        the rest wait in the request row for the tool to take. Returns (saved, message). Never
        logs or returns the values."""
        spec = self._request_spec(req)
        try:
            clean = check_values(spec["fields"], values)
        except ValueError as e:
            return False, f"저장하지 않았어요. {e}"
        secret = {f["name"]: str(clean[f["name"]]) for f in spec["fields"]
                  if f["sensitive"] and clean[f["name"]] not in ("", False)}
        public = {f["name"]: clean[f["name"]] for f in spec["fields"] if not f["sensitive"]}
        if secret:
            try:
                Vault(self.cfg.vault_dir).save(req["name"], secret, [req["domain"]] if req["domain"] else [],
                                               trusted=True)
            except Exception as e:
                log.error("saving form request %s failed (%s)", req["id"], type(e).__name__)
                return False, f"❌ 저장하지 못했어요 ({type(e).__name__})."
        if not self.store.finish_login_request(req["id"], "saved", public):
            return False, "이 요청은 이미 끝났어요."
        if secret:
            return True, f"✅ {req['name']} 저장됨" + (" (나머지는 전달함)" if public else "")
        return True, f"✅ {spec['title']} 전달함"

    async def _login_reply(self, msg, req) -> None:
        """The user's reply to a form request: one field per line, in the form's order. Routed like
        the form's answer and deleted from the chat; the text is never logged."""
        lines = (msg.text or "").split("\n")
        try:
            deleted = bool(await msg.delete())
        except Exception as e:
            log.warning("could not delete a login reply (%s)", type(e).__name__)
            deleted = False
        undeleted = "" if deleted else "\n⚠️ 메시지를 지우지 못했어요. 직접 삭제해주세요."
        if req["status"] != "pending":
            again = f" 다시 하려면 /login {req['name']} {req['domain']}" if req["kind"] == "login" else ""
            await self.say(f"이 요청은 이미 끝났어요. 저장하지 않았어요.{again}{undeleted}")
            return
        fields = self._request_spec(req)["fields"]
        if len(lines) != len(fields):
            order = ", ".join(f["label"] for f in fields)
            await self.say(f"형식이 맞지 않아 저장하지 않았어요. 요청 메시지에 다시 답장해주세요: 한 줄에 하나씩 "
                           f"{order} ({len(fields)}줄).{undeleted}")
            return
        ok, text = self._save_answer(req, {f["name"]: line for f, line in zip(fields, lines)})
        if not ok:
            await self.say(text + undeleted)
            return
        await self._close_login_request(req["id"], text + (" (메시지 삭제함)" if deleted else ""), msg.chat_id)
        if not deleted:
            await self.say(f"{text}{undeleted}")
        await self._request_done("login", req["id"])

    async def cmd_login(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        try:
            if len(args) != 2:
                raise ValueError("사용법: /login <이름> <도메인>, 예: /login school lms.example.edu")
            domain = check(args[0], [args[1]])[0]
        except ValueError as e:
            await update.message.reply_text(f"요청하지 않았어요: {e}")
            return
        await self._send_login_request(self.store.add_login_request(args[0], domain))

    @staticmethod
    def _vault_entry(name: str, domains, trusted) -> tuple[str, InlineKeyboardMarkup]:
        state = "묻지 않고 입력 (승인됨)" if trusted else "사용 전에 물어봄"
        buttons = [InlineKeyboardButton("🗑 삭제", callback_data=f"vault:del:{name}")]
        if trusted:
            buttons.insert(0, InlineKeyboardButton("🔒 다시 승인받기", callback_data=f"vault:untrust:{name}"))
        return f"🔑 {name}: {', '.join(domains) or '사이트 없음'}\n{state}", InlineKeyboardMarkup([buttons])

    def _vault_lookup(self, name: str):
        return next((e for e in Vault(self.cfg.vault_dir).entries() if e[0] == name), None)

    async def cmd_logins(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            entries = Vault(self.cfg.vault_dir).entries()
        except Exception as e:
            await update.message.reply_text(f"로그인 금고를 읽을 수 없어요 ({type(e).__name__}).")
            return
        if not entries:
            await update.message.reply_text("저장된 로그인이 없어요. /login <이름> <도메인> 으로 추가해요.")
            return
        for entry in entries:
            text, buttons = self._vault_entry(*entry)
            await update.message.reply_text(text, reply_markup=buttons)

    async def on_login_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q.from_user or q.from_user.id not in self.cfg.allowed_user_ids:
            await q.answer()
            return
        rid = int(q.data.split(":")[2])
        r = self.store.login_request(rid)
        if r and self.store.finish_login_request(rid, "cancelled"):
            await q.answer("취소됨")
            await self._close_login_request(rid, f"❌ {self._request_title(r)} 요청 취소됨",
                                            q.message.chat_id if q.message else None)
            await self._request_done("login", rid)
        else:
            await q.answer("이미 끝난 요청이에요")
            await self._quiet(q.edit_message_reply_markup(None))

    async def on_vault_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q.from_user or q.from_user.id not in self.cfg.allowed_user_ids:
            await q.answer()
            return
        _, action, name = q.data.split(":", 2)
        v = Vault(self.cfg.vault_dir)
        if not self._vault_lookup(name):
            await q.answer("없는 로그인이에요")
            await self._quiet(q.edit_message_reply_markup(None))
            return
        if action == "untrust":
            v.untrust(name)
            await q.answer("다음 사용 때 다시 물어볼게요")
        elif action == "del":
            await q.answer()
            await self._quiet(q.edit_message_text(
                f"🗑 '{name}' 로그인을 금고에서 삭제할까요? 되돌릴 수 없어요.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🗑 삭제", callback_data=f"vault:delyes:{name}"),
                    InlineKeyboardButton("취소", callback_data=f"vault:delno:{name}"),
                ]])))
            return
        elif action == "delyes":
            v.remove(name)
            await q.answer("삭제됨")
            await self._quiet(q.edit_message_text(f"🗑 {name} 삭제됨"))
            return
        else:
            await q.answer()
        text, buttons = self._vault_entry(*self._vault_lookup(name))
        await self._quiet(q.edit_message_text(text, reply_markup=buttons))

    async def cmd_runs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = [a.lstrip("#") for a in (ctx.args or [])]
        if args[:1] == ["cancel"]:
            rid = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
            if rid and self.store.set_run_status(rid, "cancelled", only_from="pending"):
                text = f"⏹ 브라우저 작업 #{rid}을 취소했어요."
            elif rid and rid == self._running_run_id() and self.store.set_run_status(rid, "cancelled", only_from="running"):
                self._run_task.cancel()  # type: ignore[union-attr]  # _browser_run reports it
                text = f"⏹ 브라우저 작업 #{rid}을 멈추는 중…"
            else:
                text = "사용법: /runs cancel <번호> (대기 중이거나 실행 중인 작업). 목록은 /runs"
            await update.message.reply_text(text)
            return
        lines = []
        for r in self.store.runs_overview():
            line = f"#{r['id']} {r['status']} {tu.local_str(tu.from_iso(r['run_at']), self.tz)} [{r['mode']}] {short(r['prompt'], 60)}"
            if r["status"] in ("done", "failed") and r["result"]:
                line += f"\n   → {short(r['result'], 100)}"
            lines.append(line)
        await self.say("\n".join(lines) + "\n\n취소: /runs cancel <번호>" if lines else "브라우저 작업이 없어요.",
                       update.effective_chat.id)

    def _running_run_id(self) -> int | None:
        return self._run_id if self._run_task and not self._run_task.done() else None

    async def _runs_startup(self) -> None:
        for rid in self.store.fail_interrupted_runs():
            await self.say(f"⚠️ Friday가 재시작돼서 브라우저 작업 #{rid}이 멈췄어요. /runs 에서 볼 수 있어요.")

    async def browser_run_loop(self) -> float:
        # Polled, because schedule_browser_run (in the MCP server) adds runs too. One run at a time,
        # and not while a watch check (watches.py) is running: one unattended Codex run at once.
        if self._running_run_id() or self._watch_busy():
            return 10
        run = self.store.next_due_run(tu.now_utc())
        if run and self.store.set_run_status(run["id"], "running", only_from="pending"):
            self._run_id = run["id"]
            self._run_task = asyncio.create_task(self._browser_run(self.store.scheduled_run(run["id"])), name=f"run-{run['id']}")
        return 10

    async def _browser_run(self, run) -> None:
        """A fresh Codex thread does the run (its run id lets fill_login use its approved logins);
        the result and a last screenshot go to the user. Never raises except on shutdown."""
        rid = run["id"]
        await self._quiet(self.say(f"🌐 브라우저 작업 #{rid} 시작 ({run['mode']}): {short(run['prompt'], 200)}"))
        prompt = instructions(self.cfg) + "\n---\n" + browser.run_prompt(run, tu.local_str(tu.now_utc(), self.tz))
        try:
            result = await run_codex(
                replace(self.cfg, codex_timeout_seconds=browser.RUN_TIMEOUT_SECONDS), prompt,
                model=self.store.get("model") or "", effort=self.store.get("effort") or "", run_id=rid,
                toolsets={"browser"}, tz=self.tz,
            )
        except asyncio.CancelledError:
            if self.store.scheduled_run(rid)["status"] != "cancelled":  # Friday is stopping: next start reports it
                raise
            await self._quiet(self.say(f"⏹ 브라우저 작업 #{rid} 중지됨."))
            return
        except Exception as e:
            log.exception("browser run %s crashed", rid)
            result = AgentResult("", None, [f"{type(e).__name__}: {e}"])
        text = result.text or "; ".join(result.errors) or "no reply"
        self.store.set_run_status(rid, "done" if result.ok else "failed", text, only_from="running")
        try:
            shot = self.cfg.files_dir / "browser" / f"run-{rid}.png"
            if await browser.screenshot(shot):
                with shot.open("rb") as fh:
                    await self.app.bot.send_photo(self.owner, fh, caption=f"🌐 #{rid} 마지막 화면")  # type: ignore[union-attr]
        except Exception as e:
            log.info("no screenshot for run %s: %s", rid, e)
        head = f"✅ 브라우저 작업 #{rid} 완료" if result.ok else f"❌ 브라우저 작업 #{rid} 실패"
        await self._quiet(self.say(f"{head}:\n\n{text}"))

    # --- proposals (items read from an image or file, added on the user's tap)

    def _ko_time(self, dt: datetime) -> str:
        d = dt.astimezone(ZoneInfo(self.tz))
        return f"{d:%m/%d}({'월화수목금토일'[d.weekday()]}) {d:%H:%M}"

    def _proposal_line(self, item: dict) -> str:
        kind, title = item["kind"], item["title"]
        if kind == "task":
            line = f"📝 {title}" + (f" (마감 {self._ko_time(tu.from_iso(item['due']))})" if item.get("due") else "")
        elif kind == "reminder":
            line = f"⏰ {title} ({self._ko_time(tu.from_iso(item['when']))}"
            line += f", 반복 {item['repeat']})" if item.get("repeat") else ")"
        else:
            start, end = tu.from_iso(item["start"]), tu.from_iso(item["end"])
            same_day = start.astimezone(ZoneInfo(self.tz)).date() == end.astimezone(ZoneInfo(self.tz)).date()
            to = f"{end.astimezone(ZoneInfo(self.tz)):%H:%M}" if same_day else self._ko_time(end)
            line = f"📅 {title} ({self._ko_time(start)}~{to})" + (f" @ {item['location']}" if item.get("location") else "")
        if item.get("submission"):
            line += f"\n   📤 {item['submission']['site']} 제출"
        if item.get("notes"):
            line += f"\n   {short(item['notes'], 100)}"
        return line

    async def _send_proposal(self, pid: int) -> None:
        p = self.store.proposal(pid)
        if not p or p["status"] != "pending":
            return
        lines = ["📋 이것들을 추가할까요?"]
        lines += [f"{i}. {self._proposal_line(item)}" for i, item in enumerate(p["items"], 1)]
        if p["note"]:
            lines.append(f"\n{p['note']}")
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 모두 추가", callback_data=f"prop:accept:{pid}"),
            InlineKeyboardButton("❌ 무시", callback_data=f"prop:dismiss:{pid}"),
        ]])
        await self.app.bot.send_message(self.owner, "\n".join(lines)[:TELEGRAM_LIMIT], reply_markup=buttons)  # type: ignore[union-attr]

    async def _accept_proposal(self, pid: int) -> list[str] | None:
        """Add a proposal's items. None if it was already accepted or dismissed (a double tap)."""
        if not self.store.settle_proposal(pid, "accepted"):
            return None
        p = self.store.proposal(pid)
        lines = []
        for item in p["items"]:  # type: ignore[index]
            try:
                lines.append(await self._add_proposed_item(item))
            except Exception as e:
                log.exception("adding proposed item failed: %s", item)
                lines.append(f"⚠️ {item['title']}: 추가 실패 ({e})")
        return lines

    async def _add_proposed_item(self, item: dict) -> str:
        tz, now, title = self.tz, tu.now_utc(), item["title"]
        notes = item.get("notes", "")
        if item.get("submission"):
            sub = item["submission"]
            notes = (notes + "\n" if notes else "") + f"제출: {sub['site']}" + (f" - {sub['what']}" if sub["what"] else "")

        def task(due: datetime | None, remind: str | None = None, extra: str = "") -> str:
            if remind == "none":
                nudge = None
            elif remind:
                nudge = tu.from_iso(remind)
            else:
                nudge = tu.default_task_reminder(due, tz, now) if due else None
            t = self.store.add_task(title, due, nudge, notes)
            return f"📝 #{t.id} {title}" + (f" (마감 {self._ko_time(due)})" if due else "") + extra

        if item["kind"] == "task":
            return task(tu.from_iso(item["due"]) if item.get("due") else None, item.get("remind"))
        if item["kind"] == "reminder":
            when = tu.from_iso(item["when"])
            if not item.get("repeat") and when < now:
                return f"⏭ {title}: 시간이 지나 건너뛰었어요"
            r = self.store.add_reminder(title, when, tz, item.get("repeat"))
            return f"⏰ 리마인더 #{r.id} {title} ({self._ko_time(r.next_at)})"
        start, end = tu.from_iso(item["start"]), tu.from_iso(item["end"])
        if not gcal.is_connected(self.cfg.google_token):
            return task(start, extra=" (캘린더가 연결되지 않아 할 일로 추가)")
        try:
            await asyncio.to_thread(
                gcal.create_event, self.cfg.google_token, title, start, end, notes, item.get("location", "")
            )
        except Exception as e:
            log.warning("calendar event for a proposal failed: %s", e)
            return task(start, extra=f" (캘린더 오류로 할 일로 추가: {short(str(e), 80)})")
        return f"📅 {title} ({self._ko_time(start)}) 캘린더에 추가"

    async def on_proposal_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q.from_user or q.from_user.id not in self.cfg.allowed_user_ids:
            await q.answer()
            return
        _, action, raw = q.data.split(":")
        pid = int(raw)
        p = self.store.proposal(pid)
        if not p or p["status"] != "pending":
            await q.answer("이미 처리했어요" if p else "없는 제안이에요")
            await self._quiet(q.edit_message_reply_markup(None))
            return
        if action == "dismiss":
            if self.store.settle_proposal(pid, "dismissed"):
                await q.answer("무시했어요")
                text = q.message.text if q.message and q.message.text else "📋"
                await self._quiet(q.edit_message_text(f"{text}\n\n❌ 무시했어요"))
            else:
                await q.answer("이미 처리했어요")
            return
        await q.answer("추가하는 중…")  # answer now: calendar calls can outlast Telegram's wait
        lines = await self._accept_proposal(pid)
        if lines is None:
            return
        await self._quiet(q.edit_message_text(("✅ 추가했어요:\n" + "\n".join(lines))[:TELEGRAM_LIMIT]))
        for i, item in enumerate(p["items"]):
            if item.get("submission"):
                buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏰ 리마인더만", callback_data=f"sub:remind:{pid}:{i}"),
                    InlineKeyboardButton("🧭 준비 후 승인", callback_data=f"sub:approve:{pid}:{i}"),
                    InlineKeyboardButton("🤖 자동 제출", callback_data=f"sub:auto:{pid}:{i}"),
                ]])
                await self.app.bot.send_message(  # type: ignore[union-attr]
                    self.owner, f"📤 {item['title']} ({item['submission']['site']}) 제출은 어떻게 할까요?",
                    reply_markup=buttons,
                )

    @staticmethod
    def _submission_prompt(item: dict, due: datetime | None, tz: str) -> str:
        sub = item["submission"]
        when = f" (due {tu.local_str(due, tz)}, {tz})" if due else ""
        prompt = (
            f"Log in to {sub['site']}, find '{item['title']}'{when} and submit "
            f"{sub['what'] or 'what it asks for'}. Use files the user sent, saved in the workspace under "
            "files/inbox/ (newest first), if they clearly match; never submit something that doesn't. "
            "If something needed is missing (a file, an answer, a login step you can't complete), ask the "
            "user instead of guessing."
        )
        return prompt + (f"\nNotes: {item['notes']}" if item.get("notes") else "")

    async def on_submission_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q.from_user or q.from_user.id not in self.cfg.allowed_user_ids:
            await q.answer()
            return
        _, choice, rp, ri = q.data.split(":")
        pid, idx = int(rp), int(ri)
        p = self.store.proposal(pid)
        item = p["items"][idx] if p and p["status"] == "accepted" and idx < len(p["items"]) else None
        if not item or not item.get("submission"):
            await q.answer("없는 항목이에요")
            return
        key = f"proposal_sub:{pid}:{idx}"
        if not self.store.claim_once(key, choice):
            await q.answer("이미 정했어요")
            await self._quiet(q.edit_message_reply_markup(None))
            return
        title = item["title"]
        if choice == "remind":
            await q.answer("리마인더만")
            await self._quiet(q.edit_message_text(f"⏰ {title}: 마감 알림만 드릴게요."))
            return
        raw_due = item.get("due") or item.get("when") or item.get("start")
        due = tu.from_iso(raw_due) if raw_due else None
        now = tu.now_utc()
        run_at = due - timedelta(hours=24) if due else now
        if run_at <= now:
            run_at = now + timedelta(minutes=1)
        # Tapping "자동 제출" is the owner's approval for this site's saved logins during this run;
        # in "approve" mode each login still asks on Telegram.
        site = str(item["submission"].get("site") or "")
        host = re.sub(r"^[a-z]+://", "", site.strip().lower()).split("/")[0]
        logins = Vault(self.cfg.vault_dir).names_for(host) if host else []
        try:
            rid = self.store.add_scheduled_run(run_at, self._submission_prompt(item, due, self.tz), choice,
                                               logins=logins if choice == "auto" else ())
        except Exception as e:
            log.exception("scheduling a submission run failed")
            self.store.delete(key)  # let the user try again
            await q.answer(f"실패: {short(str(e), 150)}", show_alert=True)
            return
        await q.answer("예약했어요")
        how = "제출 전에 승인을 받을게요" if choice == "approve" else "자동으로 제출할게요"
        missing = "" if logins or not host else (
            f"\n⚠️ {host} 로그인이 금고에 없어요. /login 이름 {host} 로 추가해주세요.")
        await self._quiet(q.edit_message_text(
            f"{'🧭' if choice == 'approve' else '🤖'} {title}: {self._ko_time(run_at)}에 시작해서 {how}. (실행 #{rid}){missing}"
        ))

    # --- files (getting files in and out; /files browses the workspace) ------

    async def _download(self, media, dest: Path) -> None:
        """Save an incoming file at dest. The local Bot API server has fetched it into its own folder
        by the time getFile answers: move it from there (same disk, so instant), don't copy 2 GB."""
        if not self.cfg.local_botapi:
            await (await media.get_file()).download_to_drive(dest)
            return
        f = await media.get_file(read_timeout=botapi.TRANSFER_TIMEOUT)
        src = Path(f.file_path or "")
        if src.is_absolute() and src.is_file() and src.resolve().is_relative_to(self.cfg.botapi_dir.resolve()):
            await asyncio.to_thread(shutil.move, src, dest)
        else:
            await f.download_to_drive(dest)

    async def _send_path(self, path: Path, caption: str | None = None, chat_id: int | None = None) -> str | None:
        """Send a file as a document. Returns why not when it's too big for Telegram."""
        size, limit = path.stat().st_size, botapi.upload_limit(self.cfg)
        if size > limit:
            how = "" if self.cfg.local_botapi else " Copy it with scp, or set up the local Bot API server (README)."
            return f"{path.name} is {wsfiles.human_size(size)}; Telegram lets me send up to {wsfiles.human_size(limit)}.{how}"
        timeouts = {"read_timeout": botapi.TRANSFER_TIMEOUT, "write_timeout": botapi.TRANSFER_TIMEOUT}
        if self.cfg.local_botapi:  # a Path becomes a file:// URI: the local server reads the file itself
            await self.app.bot.send_document(chat_id or self.owner, path, filename=path.name, caption=caption, **timeouts)  # type: ignore[union-attr]
        else:
            with path.open("rb") as fh:
                await self.app.bot.send_document(chat_id or self.owner, fh, filename=path.name, caption=caption, **timeouts)  # type: ignore[union-attr]
        return None

    def _fs_id(self, rel: str) -> int:
        if rel not in self._fs_index:
            self._fs_index[rel] = len(self._fs_paths)
            self._fs_paths.append(rel)
        return self._fs_index[rel]

    def _fs_button(self, e: wsfiles.Entry) -> InlineKeyboardButton:
        if e.is_dir:
            return InlineKeyboardButton(f"📂 {short(e.name, 40)}", callback_data=f"fs:d:{self._fs_id(e.rel)}:0")
        when = wsfiles.local_time(e.mtime, self.tz, "%m/%d %H:%M")
        return InlineKeyboardButton(f"📄 {short(e.name, 30)} · {wsfiles.human_size(e.size)} · {when}",
                                    callback_data=f"fs:f:{self._fs_id(e.rel)}")

    def _fs_recent_view(self) -> tuple[str, InlineKeyboardMarkup]:
        files = wsfiles.recent(self.cfg.workspace)
        rows = [[self._fs_button(e)] for e in files]
        rows.append([InlineKeyboardButton("📂 폴더 보기", callback_data=f"fs:d:{self._fs_id('.')}:0")])
        return ("🕘 최근 파일 (눌러서 받기·삭제)" if files else "아직 파일이 없어요."), InlineKeyboardMarkup(rows)

    def _fs_dir_view(self, rel: str, page: int = 0, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
        items = wsfiles.entries(self.cfg.workspace, rel)
        back = InlineKeyboardButton("🕘 최근 파일", callback_data="fs:r")
        if items is None:
            return f"{note}폴더를 찾을 수 없어요: {rel}", InlineKeyboardMarkup([[back]])
        pages = max(1, -(-len(items) // FILES_PAGE))
        page = min(max(page, 0), pages - 1)
        rows = [[self._fs_button(e)] for e in items[page * FILES_PAGE:(page + 1) * FILES_PAGE]]
        did = self._fs_id(rel)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"fs:d:{did}:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"fs:d:{did}:{page + 1}"))
        if nav:
            rows.append(nav)
        bottom = [back]
        if rel not in ("", "."):
            parent = Path(rel).parent.as_posix()
            bottom.insert(0, InlineKeyboardButton("⬅️ 뒤로", callback_data=f"fs:d:{self._fs_id(parent)}:0"))
        rows.append(bottom)
        name = "workspace" if rel in ("", ".") else rel
        count = f"{len(items)}개" + (f", {page + 1}/{pages}쪽" if pages > 1 else "")
        return f"{note}📂 {name} ({count})" + ("" if items else "\n비어 있어요."), InlineKeyboardMarkup(rows)

    def _fs_file_view(self, rel: str, path: Path) -> tuple[str, InlineKeyboardMarkup]:
        st = path.stat()
        fid = self._fs_id(rel)
        text = f"📄 {rel}\n{wsfiles.human_size(st.st_size)} · {wsfiles.local_time(st.st_mtime, self.tz)}"
        return text, InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 받기", callback_data=f"fs:g:{fid}"),
             InlineKeyboardButton("🗑 삭제", callback_data=f"fs:x:{fid}")],
            [InlineKeyboardButton("⬅️ 뒤로", callback_data=f"fs:d:{self._fs_id(Path(rel).parent.as_posix())}:0")],
        ])

    async def cmd_files(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        folder = " ".join(ctx.args or []).strip().strip("/")
        if folder:
            text, markup = self._fs_dir_view(folder)
        else:
            text, markup = await asyncio.to_thread(self._fs_recent_view)
        await update.message.reply_text(text, reply_markup=markup)

    async def on_files_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q.from_user or q.from_user.id not in self.cfg.allowed_user_ids:
            await q.answer()
            return
        parts = q.data.split(":")
        if parts[1] == "r":
            await q.answer()
            text, markup = await asyncio.to_thread(self._fs_recent_view)
            await self._quiet(q.edit_message_text(text, reply_markup=markup))
            return
        op, idx = parts[1], int(parts[2])
        rel = self._fs_paths[idx] if idx < len(self._fs_paths) else None
        if rel is None:  # buttons from before a restart
            await q.answer("목록이 오래됐어요. /files 를 다시 보내주세요.", show_alert=True)
            return
        if op == "d":
            await q.answer()
            text, markup = self._fs_dir_view(rel, int(parts[3]) if len(parts) > 3 else 0)
            await self._quiet(q.edit_message_text(text, reply_markup=markup))
            return
        path = wsfiles.inside(self.cfg.workspace, rel)
        if path is None or not path.is_file():
            await q.answer("이 파일은 이제 없어요")
            text, markup = self._fs_dir_view(Path(rel).parent.as_posix())
            await self._quiet(q.edit_message_text(text, reply_markup=markup))
            return
        if op == "f":
            await q.answer()
            text, markup = self._fs_file_view(rel, path)
        elif op == "g":
            await q.answer("보내는 중…")
            problem = await self._send_path(path, chat_id=q.message.chat_id if q.message else None)
            if problem:
                await self.say(f"⚠️ {problem}")
            return
        elif op == "x":
            await q.answer()
            text = f"🗑 {rel}\n삭제할까요? 되돌릴 수 없어요."
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 삭제", callback_data=f"fs:y:{idx}"),
                InlineKeyboardButton("취소", callback_data=f"fs:f:{idx}"),
            ]])
        else:  # "y": delete. The link itself if rel is one (inside() checked where it leads), not its target.
            try:
                (self.cfg.workspace.resolve() / rel).unlink()
            except OSError as e:
                await q.answer(f"삭제하지 못했어요 ({type(e).__name__})", show_alert=True)
                return
            await q.answer("삭제했어요")
            text, markup = self._fs_dir_view(Path(rel).parent.as_posix(), note=f"🗑 삭제했어요: {Path(rel).name}\n\n")
        await self._quiet(q.edit_message_text(text, reply_markup=markup))

    # --- background loops (each returns seconds until its next run) --------

    async def reminder_loop(self) -> float:
        now = tu.now_utc()
        for r in self.store.due_reminders(now):
            text = f"⏰ {r.text}"
            if now - r.next_at > timedelta(minutes=10):
                text += f"\n({self._ko_time(r.next_at)} 예정이었어요)"
            await self.say(text)
            self.store.advance_reminder(r, now)

        zone = ZoneInfo(self.tz)
        for t in self.store.tasks_needing_nudge(now):
            due = f"\n마감 {self._ko_time(t.due_at)}" if t.due_at else ""
            await self._send_task_alert(f"📝 {t.title}{due}", t)
            self.store.mark_task_nudged(t.id)
        for t in self.store.tasks_now_due(now):
            await self._send_task_alert(f"⏰ 지금 마감: {t.title}", t)
            self.store.mark_task_due_notified(t.id)
        await self._expire_followups()
        # Sleep until the next alert, so it goes out on time; at most 15 s, since the MCP server
        # (another process) may add an earlier one meanwhile.
        nxt = self.store.next_alert_at()
        return 15 if nxt is None else min(15, max(0.5, (nxt - tu.now_utc()).total_seconds() + 0.05))

    async def outbox_loop(self) -> float:
        for row in self.store.pending_outbox():
            if row["kind"] == "file":
                path = Path(row["path"])
                if path.is_file():
                    problem = await self._send_path(path, row["text"])
                    if problem:
                        await self.say(f"⚠️ {problem}")
            elif row["kind"] == "proposal":
                await self._send_proposal(int(row["text"]))
            elif row["kind"] == "approval":  # text holds the approval id, path an optional screenshot
                await self._send_approval(int(row["text"]), row["path"])
            elif row["kind"] == "login_request":  # text holds the login request id
                await self._send_login_request(int(row["text"]))
            elif row["kind"] == "login_expired":  # request_login / request_code gave up waiting
                await self._close_login_request(int(row["text"]), "⌛ 시간이 지나 요청이 끝났어요")
            elif row["kind"] == "html":  # show_tree: already escaped, too long ones split as plain text
                if len(row["text"]) <= TELEGRAM_LIMIT:
                    await self.app.bot.send_message(self.owner, row["text"], parse_mode="HTML")  # type: ignore[union-attr]
                else:
                    await self.say(html.unescape(re.sub(r"<[^>]+>", "", row["text"])))
            elif row["text"]:
                await self.say(row["text"])
            self.store.mark_sent(row["id"])
        return 3

    async def calendar_loop(self) -> float:
        if not gcal.is_connected(self.cfg.google_token):
            return 300
        zone, lead = ZoneInfo(self.tz), timedelta(minutes=self.cfg.meeting_lead_min)
        now = tu.now_utc()
        try:
            events = await asyncio.to_thread(gcal.list_events, self.cfg.google_token, now, now + lead, zone)
        except Exception as e:
            if self.store.get("gcal_error") != str(e):
                self.store.set("gcal_error", str(e))
                await self.say(f"Google 캘린더가 작동하지 않아요: {e}")
            raise
        self.store.delete("gcal_error")
        for ev in events:
            if ev.start and now < ev.start <= now + lead and self.store.claim_event_notice(ev.id, ev.start.isoformat()):
                mins = max(1, round((ev.start - now).total_seconds() / 60))
                lines = [f"📅 {mins}분 뒤: {ev.title}"]
                if ev.location:
                    lines.append(ev.location)
                if ev.link:
                    lines.append(ev.link)
                await self.say("\n".join(lines))
        return 60

    async def brief_loop(self) -> float:
        if not self.cfg.morning_brief_at:
            return 3600
        zone = ZoneInfo(self.tz)
        local = datetime.now(zone)
        at = datetime.combine(local.date(), time.fromisoformat(self.cfg.morning_brief_at), zone)
        today = local.date().isoformat()
        if at <= local < at + timedelta(hours=1) and self.store.get("brief_date") != today:
            self.store.set("brief_date", today)
            await self.say(await self.brief())
        return 30

    async def brief(self) -> str:
        zone = ZoneInfo(self.tz)
        local = datetime.now(zone)
        start = datetime.combine(local.date(), time(0), zone)
        end = start + timedelta(days=1)
        lines = [f"☀️ {local:%Y-%m-%d} ({'월화수목금토일'[local.weekday()]})"]

        if gcal.is_connected(self.cfg.google_token):
            try:
                events = await asyncio.to_thread(gcal.list_events, self.cfg.google_token, start, end, zone)
                lines.append("\n캘린더:")
                lines += [f"  {e.start:%H:%M} {e.title}" if e.start else f"  (종일) {e.title}" for e in events] or ["  없음"]
            except Exception as e:
                lines.append(f"\n캘린더를 불러오지 못했어요: {e}")

        now = tu.now_utc()
        open_tasks = self.store.open_tasks()
        overdue = [t for t in open_tasks if t.due_at and t.due_at < start]
        due_today = [t for t in open_tasks if t.due_at and start <= t.due_at < end]
        if overdue:
            lines.append("\n지난 마감:")
            lines += [f"  #{t.id} {t.title} (마감 {t.due_at.astimezone(zone):%m/%d %H:%M})" for t in overdue]
        lines.append("\n오늘 마감:")
        lines += [f"  {t.due_at.astimezone(zone):%H:%M} #{t.id} {t.title}" + (" ⚠️" if t.due_at < now else "")
                  for t in due_today] or ["  없음"]
        no_deadline = sum(1 for t in open_tasks if not t.due_at)
        if no_deadline:
            lines.append(f"  (+ 마감 없는 할 일 {no_deadline}개, /tasks)")

        rs = self.store.active_reminders(until=end)
        lines.append("\n리마인더:")
        lines += [f"  {r.next_at.astimezone(zone):%H:%M} {r.text}" for r in rs] or ["  없음"]
        return "\n".join(lines)


def run_bot(cfg: Config) -> None:
    if not cfg.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. See .env.example.")
    friday = Friday(cfg)
    friday.build().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
