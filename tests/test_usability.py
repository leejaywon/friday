import asyncio
from dataclasses import replace
from types import SimpleNamespace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from friday import timeutil as tu
from friday.agent import AgentResult
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


def test_messages_run_at_once_and_one_turn_has_the_browser(cfg, tools, monkeypatch):
    bot = Friday(replace(cfg, browser_enabled=True))
    monkeypatch.setattr(tools, "store", bot.store)
    runs = []

    async def codex_turn(prompt, thread, images, toolsets, status=None, lane="1"):
        runs.append((lane, "browser" in toolsets, "browser is busy" in prompt))
        if lane == "2" and len(runs) == 2:  # the second message asks for the browser, which the first has
            assert "already attached" not in tools.use_tools("browser", ctx=SimpleNamespace(
                headers={"X-Friday-Chat": "2", "X-Friday-No-Browser": "1"}))
            return AgentResult("-", f"t{lane}", [])
        await asyncio.sleep(0.05)
        return AgentResult(f"done {lane}", f"t{lane}", [])

    async def nothing(*a, **k):
        pass

    bot._codex_turn, bot.say, bot._keep_typing, bot.outbox_loop = codex_turn, nothing, nothing, nothing

    async def go():
        await asyncio.gather(bot.ask(1, "look this up on the website"), bot.ask(1, "and check that one too"))

    asyncio.run(go())
    # Both started at once; the second waited for the browser only after asking for it.
    assert runs[:2] == [("1", True, False), ("2", False, True)] and runs[2] == ("2", True, False)
    assert bot.lanes["2"].thread == "t2" and not bot.browser_lock.locked()
    assert "done 2" in bot.store.get("meanwhile")  # the main conversation hears about it
    bot.store.close()


def test_new_instructions_start_a_fresh_thread_with_the_last_exchange(cfg, store):
    bot = Friday(cfg)
    bot._wrap("안녕", [], True)
    bot.store.set("thread_id", "t1")
    bot._note_exchange("안녕", "안녕!")
    turns = []
    bot._turn = lambda chat, text, files, images, lane: turns.append(bot._current_thread()) or asyncio.sleep(0)
    asyncio.run(bot.ask(1, "다음"))
    cfg.instructions_path.write_text(cfg.instructions_path.read_text() + "\n- new rule")
    asyncio.run(bot.ask(1, "다음"))
    assert turns == ["t1", None] and "안녕!" in bot._wrap("다음", [], True)


def test_status_message_grows_a_stop_button_and_shrinks_back():
    from friday.bot import TurnStatus, stop_button

    async def turn(seconds, stopped=False, step_done=True):
        shown, alerts = [], []

        async def send(text, stop):
            shown.append((text, stop))
            return 7

        async def edit(msg_id, text, stop):
            shown.append((text, stop))

        status = TurnStatus(send, edit, lambda m: asyncio.sleep(0), alert=lambda t: alerts.append(t) or asyncio.sleep(0),
                            note_after=0.05, status_after=0.1, every=10, stuck_after=0.15, tick=0.01)
        status.note("확인해볼게.")
        status.note("페이지를 여는 중이야.")
        status.on_step("i1", "browser: click")
        if step_done:
            status.on_step("i1", "")
        await asyncio.sleep(seconds)
        await status.finish(stopped)
        return shown, alerts

    assert asyncio.run(turn(0.01)) == ([], [])  # quick answers stay one message
    shown, alerts = asyncio.run(turn(0.2))
    assert shown == [("페이지를 여는 중이야.", False), ("페이지를 여는 중이야.\n\n⏳ 0:00 · browser: click", True),
                     ("페이지를 여는 중이야.", False)] and not alerts
    shown, alerts = asyncio.run(turn(0.2, stopped=True, step_done=False))  # a click that never answers
    assert "⚠️ browser: click: no answer" in shown[-2][0] and len(alerts) == 1 and "browser: click" in alerts[0]
    assert shown[-1] == ("페이지를 여는 중이야.\n\n⏹ Stopped after 0:00", False)
    assert stop_button("2").inline_keyboard[0][0].callback_data == "turn:stop:2"


def test_tree_leaves_out_ignored_files_and_folds_deep_folders(tmp_path):
    import subprocess

    from friday.tree import show

    root = tmp_path / "app"
    for f in ("src/app.py", "src/utils/a.py", "src/utils/deep/b.py", "README.md", "node_modules/x/index.js"):
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text("x")
    (root / ".gitignore").write_text("node_modules/\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    plain, msg = show(root, depth=2)
    assert "node_modules" not in plain and "utils/  (2 files)" in plain and "README.md" in plain
    assert msg.startswith("<b>") and "<pre>app/\n" in msg


def test_phone_book(tools):
    out = tools.add_contact("김민수", ["휴대폰: 010-1234-5678", "회사 02-123-4567"], ["Minsu Kim", "민수형"],
                            notes="고등학교 친구")
    assert "휴대폰 010-1234-5678 · 회사 02-123-4567" in out and "(Minsu Kim, 민수형)" in out
    tools.add_contact("John Park", ["+1 415-555-0100 (US)"])
    assert tools.find_contacts("minsu").startswith("#1 김민수") and "John" not in tools.find_contacts("민수형")
    assert tools.find_contacts("5678").startswith("#1 김민수")
    assert tools.add_contact("민수", ["01012345678"]).startswith("Not saved: this looks like an existing contact")
    out = tools.update_contact(1, phones=["010-1234-5678", "+44 20 7946 0958"])
    assert "010-1234-5678 · +442079460958" in out and "고등학교 친구" in out
