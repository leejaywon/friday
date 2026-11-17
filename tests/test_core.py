import os
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from friday import timeutil as tu
from friday.agent import codex_args, parse_events
from friday.bot import split_message
from friday.config import load_config
from friday.db import Store
from friday.secrets_guard import looks_secret

SEOUL = "Asia/Seoul"


def local(s: str) -> datetime:
    return tu.parse_when(s, SEOUL)


# --- reminders -------------------------------------------------------------------

def test_one_off_reminder_fires_once(store):
    when = local("2026-11-17T09:00")
    assert when.astimezone(UTC) == datetime(2026, 11, 17, 0, 0, tzinfo=UTC)  # no offset = local time
    r = store.add_reminder("call mom", when, SEOUL)
    assert store.due_reminders(when - timedelta(minutes=1)) == []
    [due] = store.due_reminders(when)
    assert due.id == r.id
    assert store.advance_reminder(due, when) is None
    assert store.due_reminders(when + timedelta(days=1)) == []
    assert store.reminder(r.id).status == "done"


def test_weekly_reminder_follows_its_rule(store):
    # Starts on a Wednesday, rule says Mondays: the first alert is the next Monday.
    r = store.add_reminder("plan", local("2026-11-18T10:00"), SEOUL, "RRULE:FREQ=WEEKLY;BYDAY=MO")
    assert r.next_at == local("2026-11-23T10:00")
    assert store.advance_reminder(r, r.next_at) == local("2026-11-30T10:00")
    assert store.reminder(r.id).status == "active"


def test_daily_reminder_keeps_wall_clock_across_dst(store):
    berlin = "Europe/Berlin"
    when = tu.parse_when("2027-03-27T09:00", berlin)  # DST starts 2027-03-28
    r = store.add_reminder("vitamins", when, berlin, "FREQ=DAILY")
    assert store.advance_reminder(r, when).astimezone(ZoneInfo(berlin)).hour == 9


def test_missed_repeats_skip_to_future(store):
    r = store.add_reminder("stretch", local("2026-11-01T08:00"), SEOUL, "FREQ=DAILY")
    assert store.advance_reminder(r, local("2026-11-16T12:00")) == local("2026-11-17T08:00")


def test_skip_dates(store):
    r = store.add_reminder(
        "stretch", local("2026-12-24T08:30"), SEOUL, "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR",
        skip=(date(2026, 12, 25),),
    )
    assert r.preview(SEOUL) == ["Thu 2026-12-24 08:30", "Mon 2026-12-28 08:30", "Tue 2026-12-29 08:30"]
    assert store.advance_reminder(r, local("2026-12-24T08:30")) == local("2026-12-28T08:30")


def test_invalid_rules_are_rejected(store):
    for rule in ("FREQ=SOMETIMES", "FREQ=DAILY;UNTIL=20260101T000000"):
        with pytest.raises(ValueError):
            store.add_reminder("x", local("2026-11-17T09:00"), SEOUL, rule)


def test_event_notice_claimed_once(store):
    assert store.claim_event_notice("e1", "2026-11-17T09:00")
    assert not store.claim_event_notice("e1", "2026-11-17T09:00")
    assert store.claim_event_notice("e1", "2026-11-18T09:00")


# --- tasks and memories ----------------------------------------------------------

def test_task_lifecycle(store):
    t = store.add_task("보고서 마무리", local("2026-11-18T15:00"), local("2026-11-18T09:00"))
    assert store.tasks_needing_nudge(local("2026-11-18T08:59")) == []
    [nudge] = store.tasks_needing_nudge(local("2026-11-18T09:00"))
    store.mark_task_nudged(nudge.id)
    assert store.tasks_needing_nudge(local("2026-11-18T10:00")) == []
    [d] = store.tasks_now_due(local("2026-11-18T15:00"))
    store.mark_task_due_notified(d.id)
    assert store.tasks_now_due(local("2026-11-18T16:00")) == []
    store.update_task(t.id, due=local("2026-11-19T15:00"))  # a new deadline re-arms the due alert
    assert store.tasks_now_due(local("2026-11-19T15:00"))[0].id == t.id
    assert store.complete_task(t.id) and not store.complete_task(t.id)
    assert store.open_tasks() == [] and store.recent_done_tasks()[0].title == "보고서 마무리"


def test_default_task_reminder():
    now = local("2026-11-17T20:00")
    # Deadline tomorrow afternoon -> 09:00 that morning; tomorrow 10:00 -> one hour before.
    assert tu.default_task_reminder(local("2026-11-18T15:00"), SEOUL, now) == local("2026-11-18T09:00")
    assert tu.default_task_reminder(local("2026-11-18T10:00"), SEOUL, now) == local("2026-11-18T09:00")
    # Deadline in 30 minutes -> nothing (would be in the past).
    assert tu.default_task_reminder(now + timedelta(minutes=30), SEOUL, now) is None


def test_memory_dedupe_update_and_search(store):
    a, created = store.remember("Lives in Berlin")
    b, again = store.remember("  Lives in Berlin ")
    assert created and not again and a == b
    assert store.update_memory(a, "Lives in Seoul")
    for i in range(30):
        store.remember(f"note {i} about coffee")
    store.remember("엄마 생일은 3월 5일")
    store.remember("엄마는 전화 오후에 받는 걸 좋아함")
    assert store.memories("seoul")[0]["text"] == "Lives in Seoul"
    # Particles are stripped ("엄마한테" -> "엄마"); the memory matching both words comes first.
    assert store.memories("엄마한테 전화")[0]["text"] == "엄마는 전화 오후에 받는 걸 좋아함"
    assert len(store.memories("")) == 20


def test_migration_adds_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE reminders (id INTEGER PRIMARY KEY, text TEXT NOT NULL, next_at TEXT NOT NULL,"
        " start_local TEXT NOT NULL, tz TEXT NOT NULL, rrule TEXT, status TEXT NOT NULL DEFAULT 'active',"
        " created_at TEXT NOT NULL, last_sent_at TEXT);"
        "INSERT INTO reminders(text, next_at, start_local, tz, created_at) VALUES"
        " ('old', '2026-11-18T00:00:00+00:00', '2026-11-18T09:00:00', 'Asia/Seoul', 'x');"
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created_at TEXT NOT NULL);"
    )
    old.commit()
    old.close()
    s = Store(path)
    [r] = s.active_reminders()
    assert r.text == "old" and r.skip_dates == ()
    mid, _ = s.remember("fact")
    assert s.update_memory(mid, "fact 2")
    s.close()


# --- chat agent, secrets and settings --------------------------------------------

def test_parse_codex_events():
    r = parse_events([
        '{"type":"thread.started","thread_id":"abc"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":" done "}}',
        "not json",
    ])
    assert (r.thread_id, r.text, r.errors) == ("abc", "done", [])
    r = parse_events(['{"type":"turn.failed","error":{"message":"usage limit reached"}}'])
    assert not r.ok and r.errors == ["usage limit reached"]


def _overrides(args):
    return [args[i + 1] for i, a in enumerate(args) if a == "-c"]


def test_codex_args_sandbox_and_github(cfg):
    args = codex_args(cfg, "thread-1", [])
    assert args[1:4] == ["exec", "resume", "thread-1"] and args[-1] == "-"
    ov = _overrides(args)
    assert str(cfg.vault_dir) in next(o for o in ov if o.startswith("permissions="))  # the shell can't read it
    assert not any(o.startswith("mcp_servers.github") for o in ov)

    ov = _overrides(codex_args(replace(cfg, github_token_set=True), None, []))
    assert any("X-MCP-Readonly" in o for o in ov) and any(o.startswith("shell_environment_policy") for o in ov)
    ov = _overrides(codex_args(replace(cfg, github_token_set=True, github_readonly=False), None, []))
    assert not any("X-MCP-Readonly" in o for o in ov)
    # With only the coding (write) token, chat's GitHub access is always read-only.
    ov = _overrides(codex_args(replace(cfg, github_code_token="x", github_readonly=False), None, []))
    assert any("X-MCP-Readonly" in o for o in ov)


def test_secrets_are_refused_but_normal_text_is_not():
    for text in ("회사 비밀번호는 qwer1234", "card 4111 1111 1111 1111", "ghp_" + "a" * 36):
        assert looks_secret(text), text
    for text in ("Change your Netflix password", "Order number 1234567890123"):  # fails the card checksum
        assert not looks_secret(text), text


def test_tokens_kept_out_of_environ(env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GITHUB_CODE_TOKEN=github_pat_file\n")
    env.setenv("FRIDAY_ENV", str(env_file))
    env.setenv("TRANSCRIBE_API_KEY", " gsk_test ")
    c = load_config()
    assert (c.github_code_token, c.transcribe_api_key, c.transcribe_provider) == ("github_pat_file", "gsk_test", "groq")
    assert "GITHUB_CODE_TOKEN" not in os.environ and "TRANSCRIBE_API_KEY" not in os.environ
    assert "github_pat_file" not in repr(c) and "gsk_test" not in repr(c)


def test_renamed_settings(env, caplog):
    env.setenv("CODEX_TIMEOUT_S", "300")
    env.setenv("CODE_JOB_TIMEOUT_SECONDS", "900")
    env.setenv("CODE_JOB_TIMEOUT_S", "120")
    c = load_config()
    assert (c.codex_timeout_seconds, c.code_job_timeout_seconds) == (300, 900)  # old name works, new one wins
    assert "CODEX_TIMEOUT_S is the old name of CODEX_TIMEOUT_SECONDS" in caplog.text


def test_split_message():
    text = ("line\n" * 2000).strip()
    parts = split_message(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "\n".join(parts).replace("\n", "") == text.replace("\n", "")
