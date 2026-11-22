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
    first_has_browser, second_requested = asyncio.Event(), asyncio.Event()

    async def codex_turn(prompt, thread, images, toolsets, status=None, lane="1"):
        runs.append((lane, "browser" in toolsets, "browser is busy" in prompt))
        if "browser" not in toolsets:  # each request starts without reserving the browser
            assert "already attached" not in tools.use_tools("browser", ctx=SimpleNamespace(
                headers={"X-Friday-Chat": lane, "X-Friday-No-Browser": "1"}))
            if lane == "2":
                second_requested.set()
            return AgentResult("-", f"t{lane}", [])
        if lane == "1":
            first_has_browser.set()
            await second_requested.wait()
            await asyncio.sleep(0.05)  # the photo can steer this request while it is still working
        return AgentResult(f"done {lane}", f"t{lane}", [])

    async def nothing(*a, **k):
        pass

    async def light(prompt, timeout=45):  # the small model that routes messages
        return "1" if "the photo" in prompt else "new"

    steered, said = [], []

    async def steer(key, text, images):
        steered.append((key, text))
        return True

    async def say(text, chat_id=None):
        said.append(text)

    bot._codex_turn, bot.say, bot._keep_typing, bot.outbox_loop, bot._light = codex_turn, say, nothing, nothing, light
    bot.codex = SimpleNamespace(steer=steer)

    async def go():
        first = asyncio.create_task(bot.ask(1, "look this up on the website"))
        await first_has_browser.wait()
        await asyncio.gather(bot.ask(1, "and check that one too"), bot.ask(1, "here's the photo for it"))
        await first

    asyncio.run(go())
    # Both start without it; each gets it only on request, and the second waits for the first.
    assert runs == [("1", False, False), ("1", True, False), ("2", False, True), ("2", True, False)]
    assert bot.lanes["2"].thread == "t2" and not bot.browser_lock.locked()
    assert "done 2" in bot.store.get("meanwhile")  # the main conversation hears about it
    # The photo continues the first request: it goes into that turn, not a new one.
    assert [k for k, _ in steered] == ["1"] and "here's the photo for it" in steered[0][1]
    assert any(t.startswith("↪️") for t in said)
    bot.store.close()


def test_public_research_never_reserves_browser_and_browser_access_expires_after_turn(cfg, tools, monkeypatch):
    bot = Friday(replace(cfg, browser_enabled=True))
    monkeypatch.setattr(tools, "store", bot.store)
    runs = []

    async def turn(prompt, thread, images, toolsets, status=None, lane="1"):
        attached = "browser" in toolsets
        runs.append((attached, bot.browser_lock.locked()))
        if "my orders" in prompt:
            tools.use_tools("browser", ctx=SimpleNamespace(
                headers={"X-Friday-Chat": lane, "X-Friday-No-Browser": "1"}))
            return AgentResult("-", "t1")
        if not attached:
            assert "Browser tools are not attached" in prompt
        return AgentResult("done", "t1")

    async def nothing(*a, **k):
        pass

    bot._codex_turn, bot.say, bot._keep_typing, bot.outbox_loop = turn, nothing, nothing, nothing

    async def go():
        await bot.ask(1, "Research the latest Claude policy")
        await bot.ask(1, "Check my orders on the shop")
        await bot.ask(1, "Compare the public prices on that shop")

    asyncio.run(go())
    assert runs == [(False, False), (False, False), (True, True), (False, False)]
    assert not bot.browser_lock.locked() and "browser" not in bot._toolsets()
    bot.store.close()


def test_public_research_finishes_while_another_request_holds_browser(cfg):
    bot = Friday(replace(cfg, browser_enabled=True))

    async def turn(prompt, thread, images, toolsets, status=None, lane="1"):
        assert "browser" not in toolsets and bot.browser_lock.locked()
        assert "browser is busy" in prompt
        return AgentResult("research done", "t1")

    async def nothing(*a, **k):
        pass

    bot._codex_turn, bot.say, bot._keep_typing, bot.outbox_loop = turn, nothing, nothing, nothing

    async def go():
        await bot.browser_lock.acquire()
        try:
            await asyncio.wait_for(bot.ask(1, "Research the latest Claude policy"), 1)
            assert bot.browser_lock.locked()  # the other request still owns it
        finally:
            bot.browser_lock.release()

    asyncio.run(go())
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

        async def summarize(progress):
            assert progress["steps"] == ["browser: click"] and progress["notes"][-1] == "페이지를 여는 중이야."
            return "로그인은 끝났고\n목록을 여는 중이에요."

        status = TurnStatus(send, edit, lambda m: asyncio.sleep(0), alert=lambda t: alerts.append(t) or asyncio.sleep(0),
                            summarize=summarize if not step_done else None, note_after=0.05, status_after=0.1,
                            every=10, stuck_after=0.15, tick=0.01)
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
    assert any("💬 로그인은 끝났고 목록을 여는 중이에요.\n⏳" in text for text, _ in shown)  # the small model's line
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


def test_tree_of_a_workspace_folder_is_sent_as_a_tree(tools, cfg, store):
    """Not only projects: a workspace folder too, as one monospace message (not a hand-written tree)."""
    for f in ("konkuk/보안/과제.pdf", "konkuk/다이어트/기말과제.md"):
        (cfg.workspace / f).parent.mkdir(parents=True, exist_ok=True)
        (cfg.workspace / f).write_text("x")
    assert "기말과제.md" in tools.show_tree("konkuk")
    sent = store.conn.execute("SELECT text FROM outbox WHERE kind = 'html'").fetchone()["text"]
    assert sent.startswith("<pre>konkuk/\n") and "기말과제.md" in sent
    assert tools.show_tree("../..").startswith("No project or workspace folder")


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


def test_progress_updates_use_the_small_fast_model(tmp_path):
    import json

    from friday.models import light_model

    cache = tmp_path / "models_cache.json"
    cache.write_text(json.dumps({"models": [
        {"slug": "gpt-6.2-sol", "visibility": "list", "supported_reasoning_levels": [{"effort": "low"}]},
        {"slug": "gpt-6-luna", "visibility": "list", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
    ]}))
    assert light_model("codex", path=cache) == ("gpt-6-luna", "low")
    assert light_model("codex", "gpt-6.2-sol", path=cache) == ("gpt-6.2-sol", "low")
    assert light_model("claude") == ("haiku", "")


def test_watchdog_rethinks_then_stops_a_turn_that_makes_no_progress():
    """Failed steps with nothing succeeding after: advice at 20s, an order to stop at 30s, and at
    50s Friday stops the turn itself. A step that gets somewhere resets it."""
    from friday.bot import TurnStatus

    async def main():
        nudges, ended = [], []
        async def nudge(text, images):
            nudges.append(text)
        async def advise(facts):
            return "Open the video's transcript instead."
        async def give_up(facts):
            ended.append(facts["errors"][-1])
        st = TurnStatus(lambda *a: asyncio.sleep(0), lambda *a: asyncio.sleep(0), lambda m: asyncio.sleep(0),
                        nudge=nudge, advise=advise, give_up=give_up, tick=3600)
        st.on_step("1", "browser: click «Play»")
        st.on_step("1", "", "Timeout 5000ms exceeded")
        t0 = st.trouble_since
        for at in (5, 21, 31, 51):
            await st._watch(t0 + at)
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert nudges[0].startswith("[Friday supervisor] Open the video's transcript") and "Stop trying now" in nudges[1]
        assert ended == ["browser: click «Play»: Timeout 5000ms exceeded"]
        st.on_step("2", "browser: snapshot")
        st.on_step("2", "")
        assert st.trouble_since == t0  # looking isn't progress
        st.on_step("3", "browser: navigate «youtube.com/watch»")
        st.on_step("3", "")
        assert st.trouble_since is None and st.stage == 0
        await st.finish()

    asyncio.run(main())
