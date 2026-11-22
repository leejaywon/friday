"""Time parsing and recurrence. Times are stored in UTC; recurrence runs on local wall-clock time."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_when(value: str, tz: str) -> datetime:
    """Parse an ISO 8601 date-time. A value without an offset is taken as local time in `tz`."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt


def parse_dates(value: str) -> tuple[date, ...]:
    """'2026-12-25, 2027-01-01' -> dates. Empty string -> ()."""
    return tuple(sorted({date.fromisoformat(p.strip()) for p in value.replace(";", ",").split(",") if p.strip()}))


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def local_str(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %Y-%m-%d %H:%M")


def normalize_rrule(rule: str) -> str:
    """Accept 'FREQ=DAILY' or 'RRULE:FREQ=DAILY' and return the bare form."""
    rule = rule.strip()
    if rule.upper().startswith("RRULE:"):
        rule = rule[6:]
    return rule


def validate_rrule(rule: str, start_local: datetime) -> None:
    rrulestr(normalize_rrule(rule), dtstart=start_local)


def next_occurrence(
    rule: str,
    start_local: datetime,
    tz: str,
    after: datetime,
    skip: Iterable[date] = (),
    inclusive: bool = False,
) -> datetime | None:
    """Next occurrence after `after` (or at it, when inclusive), skipping `skip` dates.

    `start_local` is the naive local wall-clock time of the series start, so a daily 09:00
    reminder stays at 09:00 across daylight-saving changes. Returns an aware datetime, or None
    when the rule has ended.
    """
    zone, skip = ZoneInfo(tz), set(skip)
    cursor = after.astimezone(zone).replace(tzinfo=None)
    rr = rrulestr(normalize_rrule(rule), dtstart=start_local)
    for _ in range(10_000):  # guards against a rule whose every occurrence is skipped
        nxt = rr.after(cursor, inc=inclusive)
        if nxt is None:
            return None
        if nxt.date() not in skip:
            return nxt.replace(tzinfo=zone)
        cursor, inclusive = nxt, False
    return None


def upcoming(
    rule: str, start_local: datetime, tz: str, first: datetime, n: int = 3, skip: Iterable[date] = ()
) -> list[datetime]:
    """`first` plus the following occurrences, n in total."""
    out, cur = [first], first
    while len(out) < n:
        cur = next_occurrence(rule, start_local, tz, cur, skip)
        if cur is None:
            break
        out.append(cur)
    return out


# Defaults for vague times. Kept in code so "저녁까지" means the same thing every time.
VAGUE_TIMES = """\
Vague times (use these unless the user says otherwise):
- 아침 / morning: 09:00          - 오전 중 / by noon: 12:00       - 점심 / lunch: 12:30
- 오후 / afternoon: 15:00        - 퇴근 전 / end of workday: 18:00 - 저녁까지 / by evening: 18:00
- 저녁에 / in the evening: 19:00 - 밤 / at night: 21:00           - 자기 전 / before bed: 22:30
- 오늘 중 / today (deadline): 23:59 - 이번 주까지 / this week: Friday 18:00 - 주말 / weekend: Saturday 10:00
"""


def parse_span(span: str) -> tuple[time, time] | None:
    """'02:00-07:00' -> (02:00, 07:00); empty -> None. It may cross midnight ('23:00-07:00')."""
    if not span.strip():
        return None
    start, end = (time.fromisoformat(x.strip()) for x in span.split("-"))
    return start, end


def quiet_end(dt: datetime, tz: str, span: str) -> datetime | None:
    """The end of the quiet hours dt falls in (local time span like '02:00-07:00'), or None."""
    hours = parse_span(span)
    if not hours:
        return None
    start, end = hours
    local = dt.astimezone(ZoneInfo(tz))
    t, day = local.time(), local.date()
    if start <= end:
        inside = start <= t < end
    else:
        inside = t >= start or t < end
        if t >= start:
            day += timedelta(days=1)
    return datetime.combine(day, end, tzinfo=ZoneInfo(tz)) if inside else None


def heartbeat_slots(day: date, tz: str, first: time, every: timedelta, quiet: str) -> list[datetime]:
    """The day's check-ins: from `first`, every `every`, until the next day's first, outside quiet hours."""
    zone = ZoneInfo(tz)
    at, until = datetime.combine(day, first, tzinfo=zone), datetime.combine(day + timedelta(days=1), first, tzinfo=zone)
    slots = []
    while at < until:
        if not quiet_end(at, tz, quiet):
            slots.append(at)
        at += every
    return slots
