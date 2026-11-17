"""The Telegram bot: chat with Codex, coding jobs, browser runs and approvals, and delivery of
reminders, task alerts, meeting heads-ups and the morning brief."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import shutil
import sqlite3
import tempfile
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
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
from .config import PROJECT_ROOT, Config, ensure_dirs
from .db import Store, Task
from .jobs import JobError, Outcome, Runner, github_slug, parse_code_args, short
from .jobs import run as run_cmd
from .models import available_models, model_list
from .models import summary as models_summary
from .models import switch as switch_model
from .transcribe import TranscriptionError, model_for, transcribe
from .vault import Vault, check
from .webform import FormKey, check_values, form_url, login_spec

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000
FILES_PAGE = 8  # entries per /files page
IDLE_NEW_THREAD = timedelta(hours=6)  # start a fresh Codex thread after this much silence
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
VOICE_NOTE = "[The user sent this as a voice message; it was transcribed automatically and may contain recognition errors.]\n"
HELP = """\
Talk to me normally, in Korean or English. I can keep your to-dos and deadlines, set reminders,
check and add Google Calendar events, remember things about you, and work with files you send.
Voice messages work too: I transcribe them and answer what you said.

/today - today's events, tasks and reminders
/tasks - open to-dos and deadlines
/reminders - upcoming reminders
/new - start a fresh conversation
/model [name] [effort] - show or change the Codex model, e.g. /model gpt-6-sol high
/code [claude|codex] <repo> <task> - have a coding agent change a repo and open a pull request.
  Reply to the job's messages to give it more instructions.
/jobs - recent coding jobs
/stop <id> - stop a coding job
/runs - scheduled browser runs; /runs cancel <id> to cancel one
/login <name> <domain> - save a website login for the browser (you reply with it; I delete the message)
/logins - saved website logins: ask again before use, or delete
/tz [Area/City] - show or change your timezone
/files [folder] - browse my files: tap one to get or delete it
/backup - send the database file
/status - health check"""

# The menu Telegram shows when you type "/".
COMMANDS = [
    ("today", "Today's events, tasks and reminders"),
    ("tasks", "Open to-dos and deadlines"),
    ("reminders", "Upcoming reminders"),
    ("model", "Show or change the Codex model"),
    ("code", "Coding job: /code [claude|codex] <repo> <task>"),
    ("jobs", "Recent coding jobs"),
    ("stop", "Stop a coding job"),
    ("runs", "Scheduled browser runs"),
    ("login", "Save a website login: /login <name> <domain>"),
    ("logins", "Saved website logins"),
    ("new", "Start a fresh conversation"),
    ("tz", "Show or change your timezone"),
    ("files", "Browse files: get or delete them"),
    ("status", "Health check"),
    ("backup", "Send the database file"),
    ("help", "What Friday can do"),
]


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    chunks, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return chunks + ([rest] if rest else [])


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\-가-힣 ]+", "_", name).strip(" .") or "file"
    return name[:120]


class Friday:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        ensure_dirs(cfg)
        self.store = Store(cfg.db_path)
        self.agent_lock = asyncio.Lock()
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
        app.add_handler(MessageHandler(me & filters.StatusUpdate.WEB_APP_DATA, self.on_form_data))
        app.add_handler(CallbackQueryHandler(self.on_vault_button, pattern=r"^vault:(untrust|del|delyes|delno):[A-Za-z0-9_.-]{1,40}$"))
        app.add_handler(CallbackQueryHandler(self.on_job_button, pattern=r"^job:(merge|close|deploy|undo):\d+$"))
        app.add_handler(CallbackQueryHandler(self.on_proposal_button, pattern=r"^prop:(accept|dismiss):\d+$"))
        app.add_handler(CallbackQueryHandler(self.on_submission_button, pattern=r"^sub:(remind|approve|auto):\d+:\d+$"))
        app.add_handler(CommandHandler("files", self.cmd_files, filters=me))
        app.add_handler(CallbackQueryHandler(self.on_files_button, pattern=r"^fs:(r|[dfgxy]:\d{1,6}(:\d{1,4})?)$"))
        app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, self.on_text))
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
                     self.browser_run_loop):
            self._tasks.append(asyncio.create_task(self._forever(loop), name=loop.__name__))
        log.info("Friday started; tz=%s, allowed=%s", self.tz, self.cfg.allowed_user_ids)

    async def _on_stop(self, app: Application) -> None:
        # A running job stays 'running' in the database; the next start marks it failed and says so.
        tasks = self._tasks + [t for t in (self._job_runner, self._run_task) if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
        if files:
            head.append("[Files the user sent, saved in the workspace: " + ", ".join(files) + "]")
        has_image = any(Path(f).suffix.lower() in IMAGE_SUFFIXES for f in files)
        body = "\n".join(head) + "\n\n" + (text or (IMAGES_ONLY if has_image else FILES_ONLY))
        return f"{instructions(self.cfg)}\n---\n{body}" if new_thread else body

    def _current_thread(self) -> str | None:
        last = self.store.get("last_message_at")
        if last and tu.now_utc() - tu.from_iso(last) > IDLE_NEW_THREAD:
            self.store.delete("thread_id")
        return self.store.get("thread_id")

    async def ask(self, chat_id: int, text: str, files: list[str] | None = None, images: list[Path] | None = None) -> None:
        files = (self._take_pending_files()) + (files or [])
        # Show Codex the images among the files, not just their paths.
        images = list(images or [])
        for f in files:
            path = self.cfg.workspace / f
            if path.suffix.lower() in IMAGE_SUFFIXES and path not in images:
                images.append(path)
        async with self.agent_lock:
            typing = asyncio.create_task(self._keep_typing(chat_id))
            try:
                thread = self._current_thread()
                try:
                    result = await run_codex(
                        self.cfg,
                        self._wrap(text, files, thread is None),
                        thread,
                        images,
                        model=self.store.get("model") or "",
                        effort=self.store.get("effort") or "",
                    )
                except Exception as e:  # e.g. codex binary missing
                    log.exception("codex run failed")
                    result = AgentResult("", thread, [repr(e)])
                if result.thread_id:
                    self.store.set("thread_id", result.thread_id)
                self.store.set("last_message_at", tu.to_utc_iso(tu.now_utc()))
            finally:
                typing.cancel()
        if result.text:
            await self.say(result.text, chat_id)
        else:
            detail = "; ".join(result.errors) or "no reply"
            await self.say(f"Codex failed: {detail[:1500]}\nIf this keeps happening, send /new.", chat_id)
        await self.outbox_loop()

    async def _keep_typing(self, chat_id: int) -> None:
        while True:
            try:
                await self.app.bot.send_chat_action(chat_id, ChatAction.TYPING)  # type: ignore[union-attr]
            except Exception:
                pass
            await asyncio.sleep(4)

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
        await self.ask(update.effective_chat.id, msg.text)

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
            await msg.reply_text("Telegram only lets bots download files up to 20 MB. Copy bigger files with scp."
                                 if not self.cfg.local_botapi else "That's over 2 GB, more than Telegram lets bots download.")
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
                f"Saved {rel}. I can't listen to audio yet: set TRANSCRIBE_API_KEY on the server "
                "to have voice messages transcribed."
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
            await msg.reply_text(f"Saved {rel}, but transcribing it failed ({e}). Type what you said instead?")
            return
        finally:
            typing.cancel()

        if not text:
            await msg.reply_text("I couldn't make out any words in that recording.")
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
        self.store.delete("thread_id")
        await update.message.reply_text("Started a fresh conversation. Memories, tasks and reminders are kept.")

    async def cmd_reminders(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        rs = self.store.active_reminders()
        await self.say("\n".join(r.describe(self.tz) for r in rs) or "No upcoming reminders.", update.effective_chat.id)

    async def cmd_tasks(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        now, zone = tu.now_utc(), ZoneInfo(self.tz)
        lines = []
        for t in self.store.open_tasks():
            due = ""
            if t.due_at:
                due = ("⚠️ overdue " if t.due_at < now else "") + f"{t.due_at.astimezone(zone):%a %m/%d %H:%M}  "
            lines.append(f"#{t.id} {due}{t.title}")
        text = "\n".join(lines) or "No open tasks. 🎉"
        await self.say(text + ("\n\nSay \"#3 다 했어\" or \"finished the report\" to tick one off." if lines else ""),
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
            await q.answer("Done ✅" if ok else "Already closed")
            if t:
                await q.edit_message_text(f"✅ {t.title}")
        else:  # snooze an hour
            ok = bool(self.store.update_task(int(tid), remind=tu.now_utc() + timedelta(hours=1)))
            await q.answer("I'll remind you in 1 hour" if ok else "Already closed")
            if t and ok:
                await q.edit_message_text(f"⏳ {t.title} (reminding again in 1 hour)")

    async def _send_task_alert(self, text: str, t: Task) -> None:
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Done", callback_data=f"task:done:{t.id}"),
            InlineKeyboardButton("⏳ 1 hour", callback_data=f"task:snooze:{t.id}"),
        ]])
        await self.app.bot.send_message(self.owner, text, reply_markup=buttons)  # type: ignore[union-attr]

    async def cmd_today(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.say(await self.brief(), update.effective_chat.id)

    async def cmd_tz(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if ctx.args:
            try:
                ZoneInfo(ctx.args[0])
            except Exception:
                await update.message.reply_text("Unknown timezone. Use a name like Asia/Seoul or Europe/Paris.")
                return
            self.store.set("tz", ctx.args[0])
        await update.message.reply_text(f"Timezone: {self.tz}. Now: {tu.local_str(tu.now_utc(), self.tz)}")

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
                await update.message.reply_document(fh, filename=copy.name, caption="Friday database backup")

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
        note = f" It starts after job #{busy}." if busy else ""
        msg = await update.message.reply_text(f"🛠 Queued job #{jid}: {repo} ({engine})…{note}")
        self.store.link_job_message(msg.message_id, jid)

    async def cmd_jobs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        lines = []
        for j in self.store.recent_jobs(10):
            line = f"#{j['id']} {j['status']} {j['repo']} ({j['engine']}) – {short(j['task'], 50)}"
            lines.append(line + (f" – {j['pr_url']}" if j["pr_url"] else ""))
        await self.say("\n".join(lines) or "No coding jobs yet. Start one with /code <repo> <task>.",
                       update.effective_chat.id)

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        arg = (ctx.args or [""])[0].lstrip("#")
        jid = int(arg) if arg.isdigit() else None
        if not arg:  # plain /stop: the running job
            jid = self._running_job_id()
        job = self.store.job(jid) if jid else None
        if not job:
            await update.message.reply_text("Usage: /stop <job id>. /jobs lists them.")
            return
        if job["status"] == "queued":
            self.store.update_job(jid, status="cancelled", followup=None)
            await update.message.reply_text(f"⏹ Cancelled job #{jid} before it started.")
        elif job["status"] == "running" and jid == self._running_job_id() and self._job_exec:
            # _run_job sees the cancellation, kills the agent's processes and reports it.
            self._job_exec.cancel()
        elif job["status"] == "running":  # not ours to cancel (shouldn't happen); don't leave it stuck
            self.store.update_job(jid, status="cancelled")
            await update.message.reply_text(f"⏹ Marked job #{jid} cancelled.")
        else:
            await update.message.reply_text(f"Job #{jid} isn't running; it's {job['status']}.")

    async def _job_followup(self, msg, job) -> None:
        jid, status = job["id"], job["status"]
        if status in ("queued", "running"):
            await msg.reply_text(f"Job #{jid} is still {status}. Reply again when it's done, or /stop {jid}.")
        elif status in ("review", "done", "failed"):
            self.store.update_job(jid, followup=msg.text, status="queued")
            sent = await msg.reply_text(f"↩️ Follow-up queued for #{jid}")
            self.store.link_job_message(sent.message_id, jid)
        else:
            await msg.reply_text(f"Job #{jid} is {status}. Start a new one with /code {job['repo']} <task>.")

    async def on_job_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q.from_user or q.from_user.id not in self.cfg.allowed_user_ids:
            await q.answer()
            return
        _, action, raw = q.data.split(":")
        jid = int(raw)
        job = self.store.job(jid)
        if not job:
            await q.answer("Unknown job")
            return
        if jid in self._job_busy:
            await q.answer("Already on it…")
            return
        self._job_busy.add(jid)
        try:
            if action == "deploy":
                await self._deploy_button(q, job)
                return
            if action == "undo":
                if job["status"] != "done" or not job["commits"]:
                    await q.answer(f"Nothing to undo for job #{jid}.")
                    return
                await q.answer("Undoing…")
                await self._quiet(q.edit_message_reply_markup(None))
                await self.runner.undo(job)
                self.store.update_job(jid, status="undone")
                await self._job_send(jid, f"↩️ Undid job #{jid}: its changes to {job['repo']} were reverted "
                                          "with a new commit, so the history keeps both.")
                return
            if job["status"] != "review":
                await q.answer(f"Job #{jid} is {job['status']}.")
                await self._quiet(q.edit_message_reply_markup(None))
                return
            # Answer now: merging can take longer than Telegram waits for a callback answer.
            await q.answer("Merging…" if action == "merge" else "Closing…")
            await self._quiet(q.edit_message_reply_markup(None))
            if action == "merge":
                await self.runner.merge(job)
                self.store.update_job(jid, status="merged")
                own = self._is_own_repo(job)
                buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 Deploy", callback_data=f"job:deploy:{jid}"),
                ]]) if own else None
                hint = "\nDeploy to restart Friday with it." if own else ""
                await self._job_send(jid, f"🔀 Merged #{jid}: {job['pr_url']}{hint}", buttons)
            else:
                await self.runner.close(job)
                self.store.update_job(jid, status="closed")
                await self._job_send(jid, f"🗑 Closed #{jid}: pull request closed, branch deleted.")
        except Exception as e:
            if not isinstance(e, JobError):
                log.exception("job button %s failed", q.data)
            detail = str(e) if isinstance(e, JobError) else f"{type(e).__name__}: {e}"
            await self._quiet(q.answer("Failed"))  # no-op if already answered
            await self._job_send(jid, f"❌ {action.capitalize()} #{jid} failed: {detail}")
        finally:
            self._job_busy.discard(jid)

    async def _deploy_button(self, q, job) -> None:
        jid = job["id"]
        if job["status"] != "merged" or not self._is_own_repo(job):
            await q.answer("Nothing to deploy.")
            return
        running = self._running_job_id()
        if running:  # the restart would kill it
            await q.answer(f"Job #{running} is running. Deploy after it finishes, or /stop it.", show_alert=True)
            return
        await q.answer("Deploying…")
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
            f"{text}\n\n🚀 Deploying… I'll say when I'm back. If I'm not: journalctl --user -u {unit}"
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
                await self.app.bot.send_message(chat or self.owner, f"🚀 Deployed: {out.strip() or '?'}")  # type: ignore[union-attr]
            finally:
                self.store.delete("deploy_notify")

        for jid in self.store.fail_interrupted_jobs():
            job = self.store.job(jid)
            await self._job_send(jid, f"⚠️ Job #{jid} ({job['repo']}) was stopped because Friday restarted.\n"
                                      "Reply to this message with instructions to run it again.")

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
        what = f"follow-up: {short(job['followup'], 200)}" if job["followup"] else short(job["task"], 200)
        await self._job_send(jid, f"🛠 Started job {head}, {what}")
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
                text = f"✅ Job {head} is ready for review:\n\n{outcome.message}"
                buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔀 Merge", callback_data=f"job:merge:{jid}"),
                    InlineKeyboardButton("🗑 Close", callback_data=f"job:close:{jid}"),
                ]])
            elif failed:
                row = self.store.job(jid)
                summary = row["summary"] if row else None
                text = f"❌ Job {head} failed: {outcome.message}"
                if summary and summary not in outcome.message:
                    text += f"\n\nThe agent said:\n{summary}"
            else:
                text = f"✅ Job {head} finished:\n\n{outcome.message}"
                if outcome.undo:  # a local project changed in place: offer to revert the job's commits
                    buttons = InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ Undo", callback_data=f"job:undo:{jid}"),
                    ]])
            await self._job_send(jid, text + "\n\nReply to this message to give it more instructions.", buttons)
        except Exception:
            log.exception("reporting job %s failed", jid)

    async def _stopped_job(self, jid: int, head: str) -> None:
        try:
            self.store.update_job(jid, status="cancelled", followup=None)
            job = self.store.job(jid)
            await self.runner.cleanup(job)  # cancelled jobs can't be resumed, so drop the worktree
            note = f"\nIts pull request is still open: {job['pr_url']}" if job["pr_number"] else ""
            await self._job_send(jid, f"⏹ Stopped job {head}.{note}")
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
            await q.answer("Unknown request")
            return
        # The tool stops waiting after 5 minutes (or dies with its run); a later tap mustn't count.
        late = a["status"] == "pending" and tu.now_utc() - tu.from_iso(a["created_at"]) > APPROVAL_EXPIRES
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

    # --- forms and website logins (request_form/login/code, /login, /logins) ---

    @staticmethod
    def _request_spec(r) -> dict:
        spec = json.loads(r["spec"] or "{}")
        return spec if spec.get("fields") else login_spec(r["name"], r["domain"], r["reason"])  # /login

    def _request_title(self, r) -> str:
        return self._request_spec(r)["title"]

    async def _send_login_request(self, rid: int) -> None:
        """Send a form request (request_form, request_login, request_code, /login). With
        FRIDAY_FORM_URL the message carries a reply-keyboard button that opens the encrypted form
        (on_form_data), and a second small message the [취소] button; a reply to the request
        (on_text, _login_reply) works either way. The text says where sensitive values go: it comes
        from Friday, not from the agent's wording, so a phishing attempt shows."""
        r = self.store.login_request(rid)
        if not r or r["status"] != "pending" or r["message_id"]:
            return
        spec = self._request_spec(r)
        fields = spec["fields"]
        cancel = InlineKeyboardMarkup([[InlineKeyboardButton("취소", callback_data=f"login:cancel:{rid}")]])
        url = ""
        if self.cfg.form_url:
            try:
                url = form_url(self.cfg.form_url, {**spec, "v": 1, "rid": rid,
                                                   "key": FormKey(self.cfg.vault_dir).public_b64()})
            except Exception as e:  # no form then: the reply still works
                log.error("form unavailable (%s)", type(e).__name__)
        icon = {"login": "🔑", "code": "🔢"}.get(r["kind"], "📝")
        lines = [f"{icon} {spec['title']}"]
        if spec["description"]:
            lines.append(spec["description"])
        secret = [f["label"] for f in fields if f["sensitive"]]
        if secret:
            where = f"{spec['site']} 에서만 쓰여요" if spec["site"] else "특정 사이트에 묶이지 않아요"
            try:
                replaces = r["name"] in {n for n, _ in Vault(self.cfg.vault_dir).names()}
            except Exception:
                replaces = False
            lines.append(f"🔐 {', '.join(secret)}: 금고의 '{r['name']}'에만 저장되고 AI에게는 보이지 않아요 "
                         f"({where})." + (f" 지금 있는 '{r['name']}'은 새 값으로 바뀌어요." if replaces else ""))
        how = "아래 [📝 입력하기]를 눌러 입력해주세요 (암호화해서 보내요). 또는 이 메시지에 답장으로" if url \
            else "이 메시지에 답장으로"
        lines.append(f"{how} 한 줄에 하나씩: {', '.join(f['label'] for f in fields)}. 답장은 바로 채팅에서 지워요.")
        text = "\n".join(lines)[:TELEGRAM_LIMIT]
        if not url:
            m = await self.app.bot.send_message(self.owner, text, reply_markup=cancel)  # type: ignore[union-attr]
            self.store.set_login_request_message(rid, m.message_id)
            return
        # sendData only works from a reply-keyboard button, and a message has one keyboard, so
        # [취소] goes on its own message.
        keyboard = ReplyKeyboardMarkup([[KeyboardButton("📝 입력하기", web_app=WebAppInfo(url))]],
                                       resize_keyboard=True, one_time_keyboard=True)
        m = await self.app.bot.send_message(self.owner, text, reply_markup=keyboard)  # type: ignore[union-attr]
        self.store.set_login_request_message(rid, m.message_id)
        c = await self.app.bot.send_message(self.owner, "필요 없으면:", reply_markup=cancel)  # type: ignore[union-attr]
        self.store.set_login_request_form_message(rid, c.message_id)

    async def _close_login_request(self, rid: int, text: str, chat_id: int | None = None) -> None:
        """Show how a request ended: with the form, drop the [취소] message and the keyboard;
        otherwise edit the request message."""
        r = self.store.login_request(rid)
        if not r:
            return
        bot, chat = self.app.bot, chat_id or self.owner  # type: ignore[union-attr]
        if r["form_message_id"]:
            await self._quiet(bot.delete_message(chat, r["form_message_id"]))
            await self._quiet(bot.send_message(chat, text, reply_markup=ReplyKeyboardRemove()))
        elif r["message_id"]:
            await self._quiet(bot.edit_message_text(text, chat_id=chat, message_id=r["message_id"]))

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

    async def on_form_data(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """The form's answer (Telegram.WebApp.sendData): ciphertext only the vault's form key opens
        (webform.py). Routed like a reply; the service message is deleted."""
        msg = update.message
        data = msg.web_app_data.data if msg.web_app_data else ""
        await self._quiet(msg.delete())
        bot = self.app.bot  # type: ignore[union-attr]
        try:
            payload = FormKey(self.cfg.vault_dir).decrypt(data)
        except ValueError as e:
            log.warning("form answer unreadable (%s)", e)
            await bot.send_message(msg.chat_id, "❌ 입력 내용을 읽지 못했어요. 다시 시도해주세요.")
            return
        req = self.store.login_request(payload["rid"])
        if not req or req["status"] != "pending":
            await bot.send_message(msg.chat_id, "이 요청은 이미 끝났거나 없는 요청이에요. 저장하지 않았어요.",
                                   reply_markup=ReplyKeyboardRemove())
            return
        ok, text = self._save_answer(req, payload["values"])
        if ok:
            await self._close_login_request(req["id"], text, msg.chat_id)
        else:
            await bot.send_message(msg.chat_id, text)

    async def cmd_login(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        try:
            if len(args) != 2:
                raise ValueError("usage: /login <name> <domain>, e.g. /login school lms.example.edu")
            domain = check(args[0], [args[1]])[0]
        except ValueError as e:
            await update.message.reply_text(f"Not requested: {e}")
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
            await update.message.reply_text(f"The login vault can't be read ({type(e).__name__}).")
            return
        if not entries:
            await update.message.reply_text("No saved logins. Add one with /login <name> <domain>.")
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
                text = f"⏹ Cancelled run #{rid}."
            elif rid and rid == self._running_run_id() and self.store.set_run_status(rid, "cancelled", only_from="running"):
                self._run_task.cancel()  # type: ignore[union-attr]  # _browser_run reports it
                text = f"⏹ Stopping run #{rid}…"
            else:
                text = "Usage: /runs cancel <id> (a pending or running run). /runs lists them."
            await update.message.reply_text(text)
            return
        lines = []
        for r in self.store.runs_overview():
            line = f"#{r['id']} {r['status']} {tu.local_str(tu.from_iso(r['run_at']), self.tz)} [{r['mode']}] {short(r['prompt'], 60)}"
            if r["status"] in ("done", "failed") and r["result"]:
                line += f"\n   → {short(r['result'], 100)}"
            lines.append(line)
        await self.say("\n".join(lines) + "\n\nCancel one: /runs cancel <id>" if lines else "No browser runs.",
                       update.effective_chat.id)

    def _running_run_id(self) -> int | None:
        return self._run_id if self._run_task and not self._run_task.done() else None

    async def _runs_startup(self) -> None:
        for rid in self.store.fail_interrupted_runs():
            await self.say(f"⚠️ Browser run #{rid} was stopped because Friday restarted. /runs shows it.")

    async def browser_run_loop(self) -> float:
        # Polled, because schedule_browser_run (in the MCP server) adds runs too. One run at a time.
        if self._running_run_id():
            return 10
        run = self.store.next_due_run(tu.now_utc())
        if run and self.store.set_run_status(run["id"], "running", only_from="pending"):
            self._run_id = run["id"]
            self._run_task = asyncio.create_task(self._browser_run(self.store.scheduled_run(run["id"])), name=f"run-{run['id']}")
        return 10

    async def _browser_run(self, run) -> None:
        """A fresh Codex thread does the run (FRIDAY_RUN_ID lets fill_login use its approved logins);
        the result and a last screenshot go to the user. Never raises except on shutdown."""
        rid = run["id"]
        await self._quiet(self.say(f"🌐 Browser run #{rid} started ({run['mode']}): {short(run['prompt'], 200)}"))
        prompt = instructions(self.cfg) + "\n---\n" + browser.run_prompt(run, tu.local_str(tu.now_utc(), self.tz))
        try:
            result = await run_codex(
                replace(self.cfg, codex_timeout_seconds=browser.RUN_TIMEOUT_SECONDS), prompt,
                model=self.store.get("model") or "", effort=self.store.get("effort") or "", run_id=rid,
            )
        except asyncio.CancelledError:
            if self.store.scheduled_run(rid)["status"] != "cancelled":  # Friday is stopping: next start reports it
                raise
            await self._quiet(self.say(f"⏹ Stopped browser run #{rid}."))
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
                    await self.app.bot.send_photo(self.owner, fh, caption=f"🌐 #{rid} last page")  # type: ignore[union-attr]
        except Exception as e:
            log.info("no screenshot for run %s: %s", rid, e)
        head = f"✅ Browser run #{rid} finished" if result.ok else f"❌ Browser run #{rid} failed"
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
            await q.answer("이미 처리했어요" if p else "Unknown proposal")
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
            await q.answer("Unknown item")
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
            await q.answer(f"Failed: {short(str(e), 150)}", show_alert=True)
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
                text += f"\n(due {tu.local_str(r.next_at, self.tz)})"
            await self.say(text)
            self.store.advance_reminder(r, now)

        zone = ZoneInfo(self.tz)
        for t in self.store.tasks_needing_nudge(now):
            due = f"\nDue {t.due_at.astimezone(zone):%a %H:%M}" if t.due_at else ""
            await self._send_task_alert(f"📝 {t.title}{due}", t)
            self.store.mark_task_nudged(t.id)
        for t in self.store.tasks_now_due(now):
            await self._send_task_alert(f"⏰ Due now: {t.title}", t)
            self.store.mark_task_due_notified(t.id)
        return 15

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
                await self.say(f"Google Calendar stopped working: {e}")
            raise
        self.store.delete("gcal_error")
        for ev in events:
            if ev.start and now < ev.start <= now + lead and self.store.claim_event_notice(ev.id, ev.start.isoformat()):
                mins = max(1, round((ev.start - now).total_seconds() / 60))
                lines = [f"📅 In {mins} min: {ev.title}"]
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
        lines = [f"☀️ {local:%a %Y-%m-%d}"]

        if gcal.is_connected(self.cfg.google_token):
            try:
                events = await asyncio.to_thread(gcal.list_events, self.cfg.google_token, start, end, zone)
                lines.append("\nCalendar:")
                lines += [f"  {e.start:%H:%M} {e.title}" if e.start else f"  (all day) {e.title}" for e in events] or ["  nothing"]
            except Exception as e:
                lines.append(f"\nCalendar unavailable: {e}")

        now = tu.now_utc()
        open_tasks = self.store.open_tasks()
        overdue = [t for t in open_tasks if t.due_at and t.due_at < start]
        due_today = [t for t in open_tasks if t.due_at and start <= t.due_at < end]
        if overdue:
            lines.append("\nOverdue:")
            lines += [f"  #{t.id} {t.title} (was due {t.due_at.astimezone(zone):%m/%d %H:%M})" for t in overdue]
        lines.append("\nDue today:")
        lines += [f"  {t.due_at.astimezone(zone):%H:%M} #{t.id} {t.title}" + (" ⚠️" if t.due_at < now else "")
                  for t in due_today] or ["  nothing"]
        no_deadline = sum(1 for t in open_tasks if not t.due_at)
        if no_deadline:
            lines.append(f"  (+{no_deadline} open without a deadline, see /tasks)")

        rs = self.store.active_reminders(until=end)
        lines.append("\nReminders:")
        lines += [f"  {r.next_at.astimezone(zone):%H:%M} {r.text}" for r in rs] or ["  none"]
        return "\n".join(lines)


def run_bot(cfg: Config) -> None:
    if not cfg.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. See .env.example.")
    friday = Friday(cfg)
    friday.build().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
