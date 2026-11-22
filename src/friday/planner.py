"""Tasks, reminders and calendar events in Telegram: cards with buttons in the chat, and the Mini
App's planner (site/app/components/planner.js).

Cards: /tasks is one message listing open tasks, a row of buttons each ([✅ title] [➡️] [🗑]),
edited in place as they're tapped. What the chat agent adds or changes gets no card: its reply says
what it did. task_card, reminder_card and event_card still update the
cards older messages carry when their buttons are tapped.

Planner: /plan opens the Mini App with this week's tasks, events and reminders in the URL fragment
(miniapp.py). The owner checks, moves, removes and adds things there, and Save sends the changes
back in one go ({done, del, edit, new, evdel, remdel}); apply() makes them. Ids the page sends
are checked against the snapshot taken when it opened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import timeutil as tu
from .db import Reminder, Store, Task
from .secrets_guard import looks_secret

TASKS_PAGE = 8  # tasks per /tasks page
WEEK = timedelta(days=7)  # what the planner shows ahead
MAX_ITEMS = 40  # of each kind in the planner (its URL has room for that much)
DEFAULT_TIME = time(9, 0)  # "tomorrow" for a task without a time


def short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def when(dt: datetime, tz: str, now: datetime) -> str:
    """'Today 18:00', 'Tomorrow 09:00', 'Fri 11/21 18:00'."""
    local, today = dt.astimezone(ZoneInfo(tz)), now.astimezone(ZoneInfo(tz)).date()
    day = {today: "Today", today + timedelta(days=1): "Tomorrow"}.get(local.date(), f"{local:%a %m/%d}")
    return f"{day} {local:%H:%M}"


def task_line(t: Task, tz: str, now: datetime) -> str:
    if not t.due_at:
        return f"☐ {t.title}"
    late = "⚠️ " if t.due_at < now else ""
    return f"☐ {t.title} · {late}{when(t.due_at, tz, now)}"


# --- cards --------------------------------------------------------------------------------

def tasks_card(tasks: list[Task], tz: str, now: datetime, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """/tasks: a page of open tasks, each with [✅ title] [➡️ tomorrow] [🗑 remove]."""
    pages = max(1, -(-len(tasks) // TASKS_PAGE))
    page = min(max(page, 0), pages - 1)
    shown = tasks[page * TASKS_PAGE:(page + 1) * TASKS_PAGE]
    rows = [[InlineKeyboardButton(f"✅ {short(t.title, 24)}", callback_data=f"tl:d:{t.id}:{page}"),
             InlineKeyboardButton("➡️", callback_data=f"tl:m:{t.id}:{page}"),
             InlineKeyboardButton("🗑", callback_data=f"tl:x:{t.id}:{page}")] for t in shown]
    if pages > 1:
        rows.append([InlineKeyboardButton("‹", callback_data=f"tl:p:{page - 1}"),
                     InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=f"tl:p:{page}"),
                     InlineKeyboardButton("›", callback_data=f"tl:p:{page + 1}")])
    rows.append([InlineKeyboardButton("🗓 Planner", callback_data="tl:plan")])
    if not tasks:
        return "📋 No open tasks. 🎉", InlineKeyboardMarkup(rows)
    head = f"📋 Open tasks ({len(tasks)})"
    body = "\n".join(task_line(t, tz, now) for t in shown)
    hint = "\n\n✅ done · ➡️ tomorrow · 🗑 remove"
    return f"{head}\n\n{body}{hint}", InlineKeyboardMarkup(rows)


def task_card(t: Task, tz: str, now: datetime) -> tuple[str, InlineKeyboardMarkup | None]:
    """A task the agent added, or what became of it after a tap."""
    lines = [f"📌 {t.title}"]
    if t.due_at:
        lines.append(f"Due {when(t.due_at, tz, now)}")
    if t.remind_at:
        lines.append(f"Heads-up {when(t.remind_at, tz, now)}")
    if t.notes:
        lines.append(short(t.notes, 300))
    if t.status != "open":
        lines[0] = {"done": "✅ ", "cancelled": "🗑 "}.get(t.status, "") + t.title
        return "\n".join(lines), None
    return "\n".join(lines), InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done", callback_data=f"tc:d:{t.id}"),
        InlineKeyboardButton("➡️ Tomorrow", callback_data=f"tc:m:{t.id}"),
        InlineKeyboardButton("🗑 Remove", callback_data=f"tc:x:{t.id}")]])


def reminder_card(r: Reminder, tz: str, now: datetime) -> tuple[str, InlineKeyboardMarkup | None]:
    head = f"⏰ {r.text}"
    if r.status != "active":
        return f"🔕 {r.text} (cancelled)", None
    repeat = " · repeats" if r.rrule else ""
    return f"{head}\n{when(r.next_at, tz, now)}{repeat}", InlineKeyboardMarkup([[
        InlineKeyboardButton("🔕 Cancel", callback_data=f"rc:x:{r.id}")]])


def event_card(ev: dict, key: str, removed: bool = False) -> tuple[str, InlineKeyboardMarkup | None]:
    """ev: {title, when, location}; key names it in the button (the event id is too long for it)."""
    lines = [f"📅 {ev.get('title', '')}", ev.get("when", "")]
    if ev.get("location"):
        lines.append(f"📍 {ev['location']}")
    text = "\n".join(x for x in lines if x)
    if removed:
        return f"🗑 {text.removeprefix('📅 ')}\n(removed from Google Calendar)", None
    return text, InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Remove", callback_data=f"ev:x:{key}")]])


def tomorrow(t: Task, tz: str, now: datetime) -> datetime:
    """'➡️': the same time of day as its deadline (or 09:00), tomorrow."""
    zone = ZoneInfo(tz)
    at = t.due_at.astimezone(zone).time() if t.due_at else DEFAULT_TIME
    day = now.astimezone(zone).date() + timedelta(days=1)
    return datetime.combine(day, at, tzinfo=zone)


def move_to_tomorrow(store: Store, t: Task, tz: str, now: datetime) -> Task | None:
    due = tomorrow(t, tz, now)
    remind = tu.default_task_reminder(due, tz, now)
    return store.update_task(t.id, due=due, remind=remind, clear_remind=remind is None)


# --- the Mini App planner -----------------------------------------------------------------

def _local(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


@dataclass
class Snapshot:
    """What the planner showed: the ids its changes may name (kept in the store by request id)."""
    tasks: list[int]
    events: list[str]  # Google event ids, by the index the page uses
    reminders: list[int]


def spec(tasks: list[Task], events: list, reminders: list[Reminder], tz: str, now: datetime,
         limit: int = MAX_ITEMS) -> tuple[dict, Snapshot]:
    """The planner component's spec (miniapp.url adds v, rid and key) and its snapshot."""
    tasks, events, reminders = tasks[:limit], events[:limit], reminders[:limit]
    out = {
        "kind": "planner", "title": "Planner", "submit": "Save",
        "today": now.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d"),
        "tasks": [{"id": t.id, "t": short(t.title, 120), "d": _local(t.due_at, tz) if t.due_at else ""}
                  for t in tasks],
        "events": [{"i": i, "t": short(e.title, 120),
                    "s": e.all_day_date or (_local(e.start, tz) if e.start else ""),
                    "e": _local(e.end, tz)[11:] if e.end and not e.all_day_date else "",
                    "l": short(e.location, 60)} for i, e in enumerate(events)],
        "rem": [{"id": r.id, "t": short(r.text, 120), "at": _local(r.next_at, tz), "r": bool(r.rrule)}
                for r in reminders],
    }
    return out, Snapshot([t.id for t in tasks], [e.id for e in events], [r.id for r in reminders])


def _ids(values: dict, key: str, allowed: list) -> list:
    raw = values.get(key)
    return [x for x in raw if x in allowed] if isinstance(raw, list) else []


def apply(store: Store, values: dict, snap: Snapshot, tz: str, now: datetime,
          event_titles: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Make the planner's changes. Returns (what was done, as lines for the owner; the Google event
    ids to delete, which the caller does). Anything not in the snapshot or malformed is skipped."""
    done: list[str] = []
    for tid in _ids(values, "done", snap.tasks):
        t = store.task(tid)
        if t and store.complete_task(tid):
            done.append(f"✅ {t.title}")
    for tid in _ids(values, "del", snap.tasks):
        t = store.task(tid)
        if t and store.cancel_task(tid):
            done.append(f"🗑 {t.title}")
    for e in values.get("edit") or []:
        if not isinstance(e, dict) or e.get("id") not in snap.tasks:
            continue
        title = short(e["t"], 200) if isinstance(e.get("t"), str) and e["t"].strip() else None
        if title and looks_secret(title):
            title = None
        due = None
        if isinstance(e.get("d"), str) and e["d"]:
            try:
                due = tu.parse_when(e["d"], tz)
            except ValueError:
                pass
        if title is None and due is None:
            continue
        remind = tu.default_task_reminder(due, tz, now) if due else None
        t = store.update_task(e["id"], title=title, due=due, remind=remind, clear_remind=bool(due) and remind is None)
        if t:
            done.append(f"✏️ {t.title}" + (f" → {when(t.due_at, tz, now)}" if due and t.due_at else ""))
    for n in (values.get("new") or [])[:20]:
        title = short(n.get("t", ""), 200) if isinstance(n, dict) and isinstance(n.get("t"), str) else ""
        if not title.strip() or looks_secret(title):
            continue
        due = None
        if isinstance(n.get("d"), str) and n["d"]:
            try:
                due = tu.parse_when(n["d"], tz)
            except ValueError:
                pass
        t = store.add_task(title, due, tu.default_task_reminder(due, tz, now) if due else None)
        done.append(f"➕ {t.title}" + (f" · {when(due, tz, now)}" if due else ""))
    for rid in _ids(values, "remdel", snap.reminders):
        r = next((x for x in store.active_reminders() if x.id == rid), None)
        if r and store.cancel_reminder(rid):
            done.append(f"🔕 {r.text}")
    idx = _ids(values, "evdel", list(range(len(snap.events))))
    gone = [snap.events[i] for i in idx]
    for eid in gone:
        done.append(f"📅🗑 {(event_titles or {}).get(eid, 'event')}")
    return done, gone


def week_window(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """From the start of today to a week later, local time."""
    zone = ZoneInfo(tz)
    start = datetime.combine(now.astimezone(zone).date(), time(0), tzinfo=zone)
    return start, start + WEEK
