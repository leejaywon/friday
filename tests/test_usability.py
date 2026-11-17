import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from friday import timeutil as tu
from friday.agent import codex_args
from friday.bot import Friday, plain_text


def test_series_starting_now_is_refused_and_fired_reminder_rearmed(tools, store):
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    day = "MO TU WE TH FR SA SU".split()[now.weekday()]
    out = tools.add_reminder("운동", now.strftime("%Y-%m-%dT%H:%M"), f"FREQ=WEEKLY;BYDAY={day}")
    assert out.startswith("Not scheduled: the first alert would be now") and not store.active_reminders()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%dT09:00")
    assert "Next 3 alerts" in tools.add_reminder("운동", tomorrow, "FREQ=WEEKLY;BYDAY=TU,TH")

    # A one-off that already fired is re-armed by reschedule ("30분 미뤄줘").
    r = store.add_reminder("물", tu.now_utc() + timedelta(minutes=1), "Asia/Seoul")
    store.advance_reminder(r, tu.now_utc())
    assert store.reminder(r.id).status == "done"
    assert tools.reschedule_reminder(r.id, (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M")).startswith(
        f"Rescheduled #{r.id}")
    assert store.reminder(r.id).status == "active"


def test_recall_returns_all_or_newest(tools, store):
    for i in range(40):
        store.remember(f"메모 {i}")
    out = tools.recall("커피")  # few memories: all of them, whatever the query
    assert out.count("\n") == 39 and out.startswith("#40 메모 39")
    for i in range(40, 60):
        store.remember(f"메모 {i}")
    out = tools.recall("커피")  # many, no match: the newest 20
    assert out.startswith("No match for '커피'") and "#60 메모 59" in out and "(20 of 60 memories shown)" in out


def test_markdown_becomes_plain_text():
    assert plain_text("**영업시간**: 07:00~22:00 [네이버 지도](https://map.naver.com/p/x) "
                      "[https://a.kr](https://a.kr)") == "영업시간: 07:00~22:00 네이버 지도 https://map.naver.com/p/x https://a.kr"


def test_chat_turns_always_have_the_browser(cfg, tools, store):
    bot = Friday(replace(cfg, browser_enabled=True))
    assert bot._toolsets() == {"browser"}
    ov = codex_args(bot.cfg, None, [], toolsets=bot._toolsets(), chat=True)
    assert any(o.startswith("mcp_servers.playwright.url=") for o in ov)
    assert "already attached" in tools.use_tools("browser") and store.get("toolsets") is None
    assert not Friday(replace(cfg, browser_enabled=False))._toolsets()


def test_new_instructions_start_a_fresh_thread_with_the_last_exchange(cfg, store):
    bot = Friday(cfg)
    bot._wrap("안녕", [], True)
    bot.store.set("thread_id", "t1")
    bot._note_exchange("안녕", "안녕!")
    turns = []
    bot._turn = lambda chat, text, files, images: turns.append(bot._current_thread()) or asyncio.sleep(0)
    asyncio.run(bot.ask(1, "다음"))
    cfg.instructions_path.write_text(cfg.instructions_path.read_text() + "\n- new rule")
    asyncio.run(bot.ask(1, "다음"))
    assert turns == ["t1", None] and "안녕!" in bot._wrap("다음", [], True)


def test_progress_line_only_when_the_turn_runs_long():
    from friday.bot import ProgressNote

    async def turn(seconds):
        sent = []
        note = ProgressNote(lambda t: sent.append(t) or asyncio.sleep(0), after=0.05)
        note("확인해볼게.")
        note("페이지를 여는 중이야.")
        await asyncio.sleep(seconds)
        note.cancel()
        return sent

    assert asyncio.run(turn(0.01)) == []
    assert asyncio.run(turn(0.1)) == ["페이지를 여는 중이야."]
