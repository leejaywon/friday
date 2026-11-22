"""The conversation log's text, and importing the chats from before it existed.

Friday logs each chat turn (bot._turn). Chats from before that are only in Codex's session files
(~/.codex/sessions/**/rollout-*.jsonl, one per thread); import_codex_sessions copies the user's
messages and Friday's final replies from those into the log once, so recall finds them too.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from pathlib import Path

from . import timeutil as tu
from .db import Store

log = logging.getLogger(__name__)

VOICE_NOTE = "[The user sent this as a voice message; it was transcribed automatically and may contain recognition errors.]\n"
_FILES = "[Files the user sent, saved in the workspace: "
# What bot._wrap writes when the user sent only files (IMAGES_ONLY, FILES_ONLY) or steered with them.
DUPLICATE = timedelta(minutes=30)
_ONLY_FILES = ("[The user sent the image(s) above without a message", "(The user sent only the files above",
               "(only the files above)")


def said(text: str, files: list[str]) -> str:
    """The user's message as the conversation log keeps it."""
    if text.startswith(VOICE_NOTE):
        text = "(voice) " + text.removeprefix(VOICE_NOTE)
    return (text + (f"\n[files: {', '.join(files)}]" if files else "")).strip()


def user_text(prompt: str) -> str | None:
    """The user's own message in a chat prompt (bot._wrap, bot._steer_text): what follows the
    [Now: …] header lines. None for notes from Friday ("[… tools are now available …]")."""
    at = prompt.rfind("[Now: ")  # the instructions before it mention "[Now: …]" too
    if at < 0:
        return None
    head, sep, text = prompt[at:].partition("\n\n")
    if not sep:
        return None
    files = [f for line in head.splitlines() if line.startswith(_FILES)
             for f in line.removeprefix(_FILES).rstrip("]").split(", ")]
    text = text.strip()
    if text.startswith(_ONLY_FILES):
        text = ""
    elif text.startswith("[") and text.endswith("]"):
        return None
    return said(text, files) or None


def codex_sessions_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def import_codex_sessions(store: Store, sessions: Path, workspace: Path) -> int:
    """Chat threads from Codex's session files into the log, up to where the log begins (so
    nothing is there twice); each thread becomes a closed conversation, summarized later
    (bot._summarize_conversations). Returns how many were imported."""
    first = store.first_message_at()
    seen = {(m["role"], m["text"]): m["at"] for m in store.all_messages()}
    count = 0
    for path in sorted(sessions.rglob("rollout-*.jsonl")):
        try:
            messages, cwd = _read_session(path)
        except (OSError, ValueError) as e:
            log.warning("skipped %s: %s", path, e)
            continue
        if cwd != str(workspace):  # coding jobs run elsewhere
            continue
        kept = []
        for role, text, at in messages:
            # Already there: a thread that replays earlier messages (with new times), a turn retried in
            # a new thread. A short reply ("고마워") may really come twice, hours apart.
            before = seen.get((role, text))
            if (first and at >= first) or (before and (len(text) >= 20 or abs(tu.from_iso(at) - tu.from_iso(before)) < DUPLICATE)):
                continue
            seen[(role, text)] = at
            kept.append((role, text, at))
        messages = kept
        if any(role == "user" for role, _, _ in messages):
            store.add_conversation(messages)
            count += 1
    return count


def _read_session(path: Path) -> tuple[list[tuple[str, str, str]], str]:
    """(role, text, UTC ISO time) of the user's messages and Friday's final replies; the thread's cwd."""
    messages, cwd = [], ""
    with path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            p = row.get("payload") or {}
            if row.get("type") == "session_meta":
                cwd = p.get("cwd", "")
            if row.get("type") != "response_item" or p.get("type") != "message":
                continue
            text = "".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
            at = tu.to_utc_iso(tu.from_iso(row["timestamp"].replace("Z", "+00:00")))
            if p.get("role") == "user":
                if (mine := user_text(text)) is not None:
                    messages.append(("user", mine, at))
            elif p.get("role") == "assistant" and p.get("phase") in ("final_answer", None):  # not commentary
                if text.strip(" .\"'`-—"):
                    messages.append(("friday", text.strip(), at))
    return messages, cwd
