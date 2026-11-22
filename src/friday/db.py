"""SQLite storage shared by the bot process and the MCP tool server (WAL mode allows both)."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import timeutil as tu

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL,
    next_at     TEXT NOT NULL,          -- UTC ISO
    start_local TEXT NOT NULL,          -- naive local ISO of the series start
    tz          TEXT NOT NULL,
    rrule       TEXT,                   -- NULL for one-off reminders
    status      TEXT NOT NULL DEFAULT 'active',  -- active | done | cancelled
    created_at  TEXT NOT NULL,
    last_sent_at TEXT
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, next_at);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    notes        TEXT NOT NULL DEFAULT '',
    due_at       TEXT,                  -- UTC ISO deadline, NULL = no deadline
    remind_at    TEXT,                  -- UTC ISO nudge, cleared once sent
    status       TEXT NOT NULL DEFAULT 'open',    -- open | done | cancelled
    due_notified INTEGER NOT NULL DEFAULT 0,      -- 1 once the "due now" message went out
    created_at   TEXT NOT NULL,
    done_at      TEXT
);
CREATE INDEX IF NOT EXISTS tasks_open ON tasks(status, due_at);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,           -- text | file | proposal | approval | login_request | login_expired
    text       TEXT,                    -- message or caption; for the others, the row id to send
    path       TEXT,
    created_at TEXT NOT NULL,
    sent_at    TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY,
    repo       TEXT NOT NULL,           -- folder under ~/code, or owner/name on GitHub
    task       TEXT NOT NULL,
    engine     TEXT NOT NULL,           -- claude | codex
    status     TEXT NOT NULL DEFAULT 'queued',
        -- queued | running | review (PR open) | done | merged | closed | undone | failed | cancelled
    followup   TEXT,                    -- instruction for the next run when a follow-up is queued
    branch     TEXT,
    base       TEXT,
    worktree   TEXT,
    gh_repo    TEXT,                    -- owner/name
    session_id TEXT,                    -- agent session, resumed for follow-ups
    pr_number  INTEGER,
    pr_url     TEXT,
    commits    TEXT,                    -- local projects: the job's commits, space-separated, for Undo
    summary    TEXT,
    error      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, id);

CREATE TABLE IF NOT EXISTS job_messages (   -- Telegram messages about a job, so a reply continues it
    message_id INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (      -- yes/no questions for the user's tap (browser logins, submits)
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,           -- login | action | schedule
    text       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | denied | expired
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS login_requests ( -- forms: request_form, request_code, /login
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,           -- vault name
    domain     TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | saved | cancelled | expired
    message_id INTEGER,                 -- the Telegram message the user replies to
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_runs ( -- browser work an agent does later, on its own
    id         INTEGER PRIMARY KEY,
    run_at     TEXT NOT NULL,           -- UTC ISO
    prompt     TEXT NOT NULL,
    mode       TEXT NOT NULL,           -- approve (ask before submitting) | auto
    logins     TEXT NOT NULL DEFAULT '',  -- comma-separated vault names fill_login may use unasked
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed | cancelled
    result     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scheduled_runs_due ON scheduled_runs(status, run_at);

CREATE TABLE IF NOT EXISTS proposals (      -- items read from an image/file, waiting for the user's tap
    id         INTEGER PRIMARY KEY,
    items      TEXT NOT NULL,           -- JSON array of items (see mcp_server.propose_items)
    note       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | dismissed
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watches (        -- "tell me when X": a condition an agent checks periodically (watches.py)
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    check_prompt    TEXT NOT NULL,          -- complete instruction for the unattended checking agent
    every           TEXT NOT NULL,          -- '30m' (minutes) | 'daily 09:00' (watches.parse_every)
    toolsets        TEXT NOT NULL DEFAULT '',  -- comma-separated: browser, github
    logins          TEXT NOT NULL DEFAULT '',  -- vault names fill_login may use unasked during a check
    status          TEXT NOT NULL DEFAULT 'active',  -- active | paused | done | failed
    checking        INTEGER NOT NULL DEFAULT 0,      -- 1 while a check runs (fill_login's pre-approval)
    last_checked_at TEXT,
    next_check_at   TEXT NOT NULL,          -- UTC ISO
    last_state      TEXT,                   -- JSON of the last check's result, shown to the next check
    last_fingerprint TEXT,                  -- of the last notification: the same one isn't sent twice
    last_error      TEXT,
    fail_count      INTEGER NOT NULL DEFAULT 0,      -- failed checks in a row
    expires_at      TEXT,                   -- UTC ISO, NULL = until cancelled
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS watches_due ON watches(status, next_check_at);
CREATE TABLE IF NOT EXISTS contacts (       -- the user's phone book (contacts.py)
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,              -- as the user calls them
    aliases     TEXT NOT NULL DEFAULT '[]', -- JSON list: other spellings and nicknames (Minsu Kim, 민수형)
    phones      TEXT NOT NULL DEFAULT '[]', -- JSON [{"number": "+821012345678", "label": "휴대폰"}]
    emails      TEXT NOT NULL DEFAULT '[]', -- JSON list
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT
);
"""

JOB_FIELDS = (
    "status", "followup", "branch", "base", "worktree", "gh_repo", "session_id",
    "pr_number", "pr_url", "commits", "summary", "error",
)

# Columns added after the first release: (table, column, definition).
MIGRATIONS = [
    ("reminders", "skip_dates", "TEXT NOT NULL DEFAULT ''"),  # comma-separated local dates
    ("memories", "updated_at", "TEXT"),
    # login_requests are forms (request_form, request_code, /login): `spec` is the
    # form (forms.make_spec; '{}' = a plain login), `result` the non-sensitive values handed back
    # to the agent, erased as soon as it reads them. name/domain: where sensitive values are saved.
    ("login_requests", "kind", "TEXT NOT NULL DEFAULT 'login'"),  # login | code | form
    ("login_requests", "form_message_id", "INTEGER"),  # the message showing the Mini App keyboard
    ("login_requests", "spec", "TEXT NOT NULL DEFAULT '{}'"),
    ("login_requests", "result", "TEXT"),
]

# Korean particles stripped from search words, longest first ("엄마한테" -> "엄마").
_PARTICLES = sorted(
    "은 는 이 가 을 를 에 의 도 와 과 랑 로 으로 에서 에게 한테 께 하고 이랑 까지 부터 처럼 보다 이나 나".split(),
    key=len,
    reverse=True,
)


def search_terms(query: str) -> list[str]:
    terms = []
    for word in re.findall(r"[\w@.+-]+", query.lower()):
        for p in _PARTICLES:
            if len(word) > len(p) + 1 and word.endswith(p):
                word = word[: -len(p)]
                break
        if word and word not in terms:
            terms.append(word)
    return terms


@dataclass(frozen=True)
class Reminder:
    id: int
    text: str
    next_at: datetime
    start_local: datetime
    tz: str
    rrule: str | None
    status: str
    skip_dates: tuple[date, ...] = ()

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Reminder:
        return cls(
            id=r["id"],
            text=r["text"],
            next_at=tu.from_iso(r["next_at"]),
            start_local=tu.from_iso(r["start_local"]),
            tz=r["tz"],
            rrule=r["rrule"],
            status=r["status"],
            skip_dates=tu.parse_dates(r["skip_dates"] or ""),
        )

    def describe(self, tz: str) -> str:
        repeat = f" (repeats: {self.rrule})" if self.rrule else ""
        skip = f" (skips: {', '.join(d.isoformat() for d in self.skip_dates)})" if self.skip_dates else ""
        return f"#{self.id} {tu.local_str(self.next_at, tz)} - {self.text}{repeat}{skip}"

    def preview(self, tz: str, n: int = 3) -> list[str]:
        if not self.rrule:
            return [tu.local_str(self.next_at, tz)]
        times = tu.upcoming(self.rrule, self.start_local, self.tz, self.next_at, n, self.skip_dates)
        return [tu.local_str(t, tz) for t in times]


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    notes: str
    due_at: datetime | None
    remind_at: datetime | None
    status: str
    due_notified: bool

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Task:
        return cls(
            id=r["id"],
            title=r["title"],
            notes=r["notes"],
            due_at=tu.from_iso(r["due_at"]) if r["due_at"] else None,
            remind_at=tu.from_iso(r["remind_at"]) if r["remind_at"] else None,
            status=r["status"],
            due_notified=bool(r["due_notified"]),
        )

    def describe(self, tz: str) -> str:
        parts = [f"#{self.id} {self.title}"]
        if self.due_at:
            parts.append(f"due {tu.local_str(self.due_at, tz)}")
        if self.remind_at:
            parts.append(f"nudge {tu.local_str(self.remind_at, tz)}")
        if self.status != "open":
            parts.append(f"[{self.status}]")
        if self.notes:
            parts.append(f"- {self.notes}")
        return "  ".join(parts)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, column, definition in MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _now() -> str:
        return tu.to_utc_iso(tu.now_utc())

    # --- key/value ---------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    def tz(self, default: str) -> str:
        return self.get("tz", default) or default

    # --- reminders ---------------------------------------------------------

    def add_reminder(
        self, text: str, when: datetime, tz: str, rrule: str | None = None, skip: tuple[date, ...] = ()
    ) -> Reminder:
        """Store a reminder. For a repeating one, the first alert is the first occurrence of the
        rule at or after `when` (e.g. BYDAY=MO starting on a Wednesday fires the next Monday)."""
        start_local = when.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
        first = when
        if rrule:
            rrule = tu.normalize_rrule(rrule)
            tu.validate_rrule(rrule, start_local)
            first = tu.next_occurrence(rrule, start_local, tz, when, skip, inclusive=True)
            if first is None:
                raise ValueError("this repeat rule never fires after the start time")
        cur = self.conn.execute(
            "INSERT INTO reminders(text, next_at, start_local, tz, rrule, skip_dates, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (text, tu.to_utc_iso(first), start_local.isoformat(), tz, rrule,
             ",".join(d.isoformat() for d in skip), self._now()),
        )
        return self.reminder(cur.lastrowid)  # type: ignore[arg-type,return-value]

    def reminder(self, rid: int) -> Reminder | None:
        row = self.conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
        return Reminder.from_row(row) if row else None

    def active_reminders(self, until: datetime | None = None) -> list[Reminder]:
        sql, args = "SELECT * FROM reminders WHERE status = 'active'", []
        if until is not None:
            sql += " AND next_at <= ?"
            args.append(tu.to_utc_iso(until))
        rows = self.conn.execute(sql + " ORDER BY next_at", args).fetchall()
        return [Reminder.from_row(r) for r in rows]

    def due_reminders(self, now: datetime) -> list[Reminder]:
        return self.active_reminders(until=now)

    def advance_reminder(self, r: Reminder, now: datetime) -> datetime | None:
        """Mark a fired reminder: schedule its next occurrence, or finish it. Returns the next time."""
        nxt = tu.next_occurrence(r.rrule, r.start_local, r.tz, now, r.skip_dates) if r.rrule else None
        if nxt:
            self.conn.execute(
                "UPDATE reminders SET next_at = ?, last_sent_at = ? WHERE id = ?",
                (tu.to_utc_iso(nxt), tu.to_utc_iso(now), r.id),
            )
        else:
            self.conn.execute(
                "UPDATE reminders SET status = 'done', last_sent_at = ? WHERE id = ?",
                (tu.to_utc_iso(now), r.id),
            )
        return nxt

    def cancel_reminder(self, rid: int) -> bool:
        cur = self.conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'active'", (rid,)
        )
        return cur.rowcount > 0

    def reschedule_reminder(self, rid: int, when: datetime) -> Reminder | None:
        """Move a reminder (or series) to `when`. A one-off that already fired is re-armed."""
        r = self.reminder(rid)
        if not r or not (r.status == "active" or (r.status == "done" and not r.rrule)):
            return None
        start_local = when.astimezone(ZoneInfo(r.tz)).replace(tzinfo=None)
        first = when
        if r.rrule:
            first = tu.next_occurrence(r.rrule, start_local, r.tz, when, r.skip_dates, inclusive=True) or when
        self.conn.execute(
            "UPDATE reminders SET next_at = ?, start_local = ?, status = 'active' WHERE id = ?",
            (tu.to_utc_iso(first), start_local.isoformat(), rid),
        )
        return self.reminder(rid)

    def next_alert_at(self) -> datetime | None:
        """The soonest pending reminder, task heads-up or task deadline, for the reminder loop."""
        row = self.conn.execute(
            "SELECT MIN(t) FROM (SELECT MIN(next_at) AS t FROM reminders WHERE status = 'active'"
            " UNION ALL SELECT MIN(remind_at) FROM tasks WHERE status = 'open'"
            " UNION ALL SELECT MIN(due_at) FROM tasks WHERE status = 'open' AND due_notified = 0)"
        ).fetchone()
        return tu.from_iso(row[0]) if row and row[0] else None

    # --- tasks -------------------------------------------------------------

    def add_task(
        self, title: str, due: datetime | None = None, remind: datetime | None = None, notes: str = ""
    ) -> Task:
        cur = self.conn.execute(
            "INSERT INTO tasks(title, notes, due_at, remind_at, created_at) VALUES(?, ?, ?, ?, ?)",
            (title, notes, tu.to_utc_iso(due) if due else None, tu.to_utc_iso(remind) if remind else None,
             self._now()),
        )
        return self.task(cur.lastrowid)  # type: ignore[arg-type,return-value]

    def task(self, tid: int) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        return Task.from_row(row) if row else None

    def open_tasks(self) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = 'open' ORDER BY due_at IS NULL, due_at, id"
        ).fetchall()
        return [Task.from_row(r) for r in rows]

    def recent_done_tasks(self, limit: int = 10) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = 'done' ORDER BY done_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Task.from_row(r) for r in rows]

    def tasks_needing_nudge(self, now: datetime) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = 'open' AND remind_at IS NOT NULL AND remind_at <= ?",
            (tu.to_utc_iso(now),),
        ).fetchall()
        return [Task.from_row(r) for r in rows]

    def tasks_now_due(self, now: datetime) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = 'open' AND due_notified = 0 AND due_at IS NOT NULL AND due_at <= ?",
            (tu.to_utc_iso(now),),
        ).fetchall()
        return [Task.from_row(r) for r in rows]

    def mark_task_nudged(self, tid: int) -> None:
        self.conn.execute("UPDATE tasks SET remind_at = NULL WHERE id = ?", (tid,))

    def mark_task_due_notified(self, tid: int) -> None:
        self.conn.execute("UPDATE tasks SET due_notified = 1 WHERE id = ?", (tid,))

    def complete_task(self, tid: int) -> bool:
        cur = self.conn.execute(
            "UPDATE tasks SET status = 'done', done_at = ?, remind_at = NULL WHERE id = ? AND status = 'open'",
            (self._now(), tid),
        )
        return cur.rowcount > 0

    def cancel_task(self, tid: int) -> bool:
        cur = self.conn.execute(
            "UPDATE tasks SET status = 'cancelled', remind_at = NULL WHERE id = ? AND status = 'open'", (tid,)
        )
        return cur.rowcount > 0

    def update_task(
        self,
        tid: int,
        title: str | None = None,
        notes: str | None = None,
        due: datetime | None = None,
        remind: datetime | None = None,
        clear_remind: bool = False,
    ) -> Task | None:
        t = self.task(tid)
        if not t or t.status != "open":
            return None
        sets, args = [], []
        if title is not None:
            sets.append("title = ?"), args.append(title)
        if notes is not None:
            sets.append("notes = ?"), args.append(notes)
        if due is not None:
            sets += ["due_at = ?", "due_notified = 0"]
            args.append(tu.to_utc_iso(due))
        if remind is not None:
            sets.append("remind_at = ?"), args.append(tu.to_utc_iso(remind))
        elif clear_remind:
            sets.append("remind_at = NULL")
        if sets:
            self.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", (*args, tid))
        return self.task(tid)

    # --- memories ----------------------------------------------------------

    def remember(self, text: str) -> tuple[int, bool]:
        """Save a memory. Returns (id, created); an identical existing memory is reused."""
        text = text.strip()
        row = self.conn.execute("SELECT id FROM memories WHERE text = ?", (text,)).fetchone()
        if row:
            return row["id"], False
        cur = self.conn.execute("INSERT INTO memories(text, created_at) VALUES(?, ?)", (text, self._now()))
        return cur.lastrowid, True  # type: ignore[return-value]

    def update_memory(self, mid: int, text: str) -> bool:
        cur = self.conn.execute(
            "UPDATE memories SET text = ?, updated_at = ? WHERE id = ?", (text.strip(), self._now(), mid)
        )
        return cur.rowcount > 0

    def memory_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def memories(self, query: str = "", limit: int = 20) -> list[sqlite3.Row]:
        """Best matches for the query's words (most words matched, then newest); newest if no query."""
        terms = search_terms(query)
        if not terms:
            return self.conn.execute(
                "SELECT * FROM memories ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        score = " + ".join("(instr(lower(text), ?) > 0)" for _ in terms)
        return self.conn.execute(
            f"SELECT *, ({score}) AS score FROM memories WHERE ({score}) > 0 "
            "ORDER BY score DESC, COALESCE(updated_at, created_at) DESC, id DESC LIMIT ?",
            (*terms, *terms, limit),
        ).fetchall()

    def forget(self, mid: int) -> bool:
        return self.conn.execute("DELETE FROM memories WHERE id = ?", (mid,)).rowcount > 0

    # --- contacts ------------------------------------------------------------

    def add_contact(self, name: str, aliases: list[str], phones: list[dict], emails: list[str], notes: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO contacts(name, aliases, phones, emails, notes, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (name, json.dumps(aliases, ensure_ascii=False), json.dumps(phones, ensure_ascii=False),
             json.dumps(emails, ensure_ascii=False), notes, self._now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def contact(self, cid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()

    def contacts(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM contacts ORDER BY name COLLATE NOCASE, id").fetchall()

    def update_contact(self, cid: int, **fields) -> bool:
        """fields: name, notes (text) or aliases, phones, emails (lists); only those given change."""
        sets, args = [], []
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            args.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        if not sets:
            return self.contact(cid) is not None
        sets.append("updated_at = ?")
        return self.conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?",
                                 (*args, self._now(), cid)).rowcount > 0

    def delete_contact(self, cid: int) -> bool:
        return self.conn.execute("DELETE FROM contacts WHERE id = ?", (cid,)).rowcount > 0

    # --- outbox (messages the agent's tools ask the bot to send) ------------

    def queue(self, kind: str, text: str | None = None, path: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO outbox(kind, text, path, created_at) VALUES(?, ?, ?, ?)",
            (kind, text, path, self._now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def pending_outbox(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id").fetchall()

    def claim_outbox(self, oid: int) -> bool:
        """Take a row to send (marked sent now); False when another sender already took it."""
        cur = self.conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ? AND sent_at IS NULL", (self._now(), oid))
        return cur.rowcount == 1

    def unclaim_outbox(self, oid: int) -> None:
        """Sending it failed: it's sent again next time."""
        self.conn.execute("UPDATE outbox SET sent_at = NULL WHERE id = ?", (oid,))

    # --- coding jobs -------------------------------------------------------

    def add_job(self, repo: str, task: str, engine: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO jobs(repo, task, engine, created_at) VALUES(?, ?, ?, ?)",
            (repo, task, engine, self._now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def job(self, jid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()

    def update_job(self, jid: int, **fields) -> None:
        bad = set(fields) - set(JOB_FIELDS)
        if not fields:
            return
        if bad:
            raise ValueError(f"unknown job fields: {bad}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), self._now(), jid)
        )

    def next_queued_job(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()

    def recent_jobs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def link_job_message(self, message_id: int, jid: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO job_messages(message_id, job_id) VALUES(?, ?)", (message_id, jid)
        )

    def job_for_message(self, message_id: int) -> int | None:
        row = self.conn.execute("SELECT job_id FROM job_messages WHERE message_id = ?", (message_id,)).fetchone()
        return row["job_id"] if row else None

    def interrupted_jobs(self) -> list[int]:
        """Jobs left 'running' by a restart (bot.jobs_startup starts them again)."""
        return [r["id"] for r in self.conn.execute("SELECT id FROM jobs WHERE status = 'running' ORDER BY id")]

    # --- proposals (items the user confirms with a tap) -------------------

    def add_proposal(self, items: list[dict], note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO proposals(items, note, created_at) VALUES(?, ?, ?)",
            (json.dumps(items, ensure_ascii=False), note, self._now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def proposal(self, pid: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        return {**dict(row), "items": json.loads(row["items"])} if row else None

    def settle_proposal(self, pid: int, status: str) -> bool:
        """Move a pending proposal to accepted/dismissed. False if it was already settled (double tap)."""
        cur = self.conn.execute(
            "UPDATE proposals SET status = ? WHERE id = ? AND status = 'pending'", (status, pid)
        )
        return cur.rowcount > 0

    def claim_once(self, key: str, value: str = "1") -> bool:
        """True only the first time `key` is claimed (e.g. one answer per submission question)."""
        return self.conn.execute("INSERT OR IGNORE INTO kv(key, value) VALUES(?, ?)", (key, value)).rowcount > 0

    # --- browser runs and approvals ------------------------------------------

    def add_approval(self, kind: str, text: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO approvals(kind, text, created_at) VALUES(?, ?, ?)", (kind, text, self._now())
        )
        return cur.lastrowid  # type: ignore[return-value]

    def approval(self, aid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()

    def decide_approval(self, aid: int, status: str) -> bool:
        """pending -> approved | denied | expired, once: a late tap can't revive an expired question."""
        cur = self.conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            (status, self._now(), aid),
        )
        return cur.rowcount > 0

    def add_login_request(self, name: str, domain: str, reason: str = "", kind: str = "login",
                          spec: dict | None = None) -> int:
        """A form request. spec: forms.make_spec's form; None = a plain login (username, password)."""
        cur = self.conn.execute(
            "INSERT INTO login_requests(name, domain, reason, kind, spec, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (name, domain, reason, kind, json.dumps(spec or {}, ensure_ascii=False), self._now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def take_result(self, rid: int) -> dict:
        """A form's non-sensitive values, read once: erased from the database right away."""
        row = self.conn.execute("SELECT result FROM login_requests WHERE id = ?", (rid,)).fetchone()
        self.conn.execute("UPDATE login_requests SET result = NULL WHERE id = ?", (rid,))
        return json.loads(row["result"]) if row and row["result"] else {}

    def login_request(self, rid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM login_requests WHERE id = ?", (rid,)).fetchone()

    def login_request_for_message(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM login_requests WHERE message_id = ?", (message_id,)).fetchone()

    def set_login_request_message(self, rid: int, message_id: int) -> None:
        self.conn.execute("UPDATE login_requests SET message_id = ? WHERE id = ?", (message_id, rid))

    def set_login_request_form_message(self, rid: int, message_id: int) -> None:
        self.conn.execute("UPDATE login_requests SET form_message_id = ? WHERE id = ?", (message_id, rid))

    def finish_login_request(self, rid: int, status: str, result: dict | None = None) -> bool:
        """pending -> saved | cancelled | expired, once. result: the non-sensitive values (take_result)."""
        cur = self.conn.execute(
            "UPDATE login_requests SET status = ?, result = ? WHERE id = ? AND status = 'pending'",
            (status, json.dumps(result, ensure_ascii=False) if result else None, rid)
        )
        return cur.rowcount > 0

    def add_scheduled_run(self, run_at: datetime, prompt: str, mode: str, logins: list[str] | tuple[str, ...] = ()) -> int:
        """Queue browser work for run_at. mode: 'approve' (the agent asks before any submit) or
        'auto'. logins are vault names fill_login may use in this run without asking again;
        creating the run is the user's approval for them."""
        if mode not in ("approve", "auto"):
            raise ValueError(f"mode must be 'approve' or 'auto', not {mode!r}")
        cur = self.conn.execute(
            "INSERT INTO scheduled_runs(run_at, prompt, mode, logins, created_at) VALUES(?, ?, ?, ?, ?)",
            (tu.to_utc_iso(run_at), prompt, mode, ",".join(logins), self._now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def scheduled_run(self, rid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM scheduled_runs WHERE id = ?", (rid,)).fetchone()

    def next_due_run(self, now: datetime) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scheduled_runs WHERE status = 'pending' AND run_at <= ? ORDER BY run_at, id LIMIT 1",
            (tu.to_utc_iso(now),),
        ).fetchone()

    def set_run_status(self, rid: int, status: str, result: str | None = None, only_from: str | None = None) -> bool:
        sql, args = "UPDATE scheduled_runs SET status = ?, result = COALESCE(?, result) WHERE id = ?", [status, result, rid]
        if only_from:
            sql += " AND status = ?"
            args.append(only_from)
        return self.conn.execute(sql, args).rowcount > 0

    def run_allows_login(self, rid: int, name: str) -> bool:
        """fill_login's pre-approval: only while that run is running, only for its logins."""
        run = self.scheduled_run(rid)
        return bool(run and run["status"] == "running" and name in run["logins"].split(","))

    def runs_overview(self, recent: int = 5) -> list[sqlite3.Row]:
        """Upcoming and running runs (soonest first), then the most recent finished ones."""
        upcoming = self.conn.execute(
            "SELECT * FROM scheduled_runs WHERE status IN ('pending', 'running') ORDER BY run_at, id"
        ).fetchall()
        done = self.conn.execute(
            "SELECT * FROM scheduled_runs WHERE status NOT IN ('pending', 'running') ORDER BY id DESC LIMIT ?",
            (recent,),
        ).fetchall()
        return upcoming + done

    def fail_interrupted_runs(self) -> list[int]:
        """Runs left 'running' by a restart can't be resumed mid-run; mark them failed."""
        ids = [r["id"] for r in self.conn.execute("SELECT id FROM scheduled_runs WHERE status = 'running'")]
        for rid in ids:
            self.set_run_status(rid, "failed", "Friday restarted while this run was going.")
        return ids

    # --- watches ("tell me when X", see watches.py) ----------------------------

    WATCH_FIELDS = ("status", "checking", "last_checked_at", "next_check_at", "last_state",
                    "last_fingerprint", "last_error", "fail_count")

    def add_watch(self, title: str, check: str, every: str, next_at: datetime, toolsets: list[str] | tuple[str, ...] = (),
                  logins: list[str] | tuple[str, ...] = (), expires_at: datetime | None = None) -> int:
        """A watch the owner approved; creating it is also their approval of its logins for its checks."""
        cur = self.conn.execute(
            "INSERT INTO watches(title, check_prompt, every, toolsets, logins, next_check_at, expires_at, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (title, check, every, ",".join(toolsets), ",".join(logins), tu.to_utc_iso(next_at),
             tu.to_utc_iso(expires_at) if expires_at else None, self._now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def watch(self, wid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM watches WHERE id = ?", (wid,)).fetchone()

    def watches(self, statuses: tuple[str, ...] = ("active", "paused")) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM watches WHERE status IN ({marks}) ORDER BY status, next_check_at, id", statuses
        ).fetchall()

    def active_watch_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM watches WHERE status = 'active'").fetchone()[0]

    def next_due_watch(self, now: datetime) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM watches WHERE status = 'active' AND next_check_at <= ? ORDER BY next_check_at, id LIMIT 1",
            (tu.to_utc_iso(now),),
        ).fetchone()

    def update_watch(self, wid: int, **fields) -> None:
        bad = set(fields) - set(self.WATCH_FIELDS)
        if bad:
            raise ValueError(f"unknown watch fields: {bad}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE watches SET {cols} WHERE id = ?", (*fields.values(), wid))

    def set_watch_status(self, wid: int, status: str, only_from: str | None = None) -> bool:
        sql, args = "UPDATE watches SET status = ? WHERE id = ?", [status, wid]
        if only_from:
            sql += " AND status = ?"
            args.append(only_from)
        return self.conn.execute(sql, args).rowcount > 0

    def watch_allows_login(self, wid: int, name: str) -> bool:
        """fill_login's pre-approval: only while that watch's check runs, only for its logins."""
        w = self.watch(wid)
        return bool(w and w["status"] == "active" and w["checking"] and name in w["logins"].split(","))

    def clear_watch_checks(self) -> None:
        """No check is running (e.g. after a restart): no watch keeps its login pre-approval live."""
        self.conn.execute("UPDATE watches SET checking = 0 WHERE checking = 1")
