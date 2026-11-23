import json
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


def test_recall_finds_an_earlier_conversation(tools, store):
    store.log_message("user", "집에 달 CCTV 추천해줘")
    store.log_message("friday", "Tapo C200과 샤오미 C300을 비교했어. https://example.com/c200")
    store.log_message("user", "고마워")
    cid = store.close_conversation()
    store.set_conversation_summary(cid, "집 CCTV 비교", "Tapo C200 vs Xiaomi C300\nKeywords: Tapo C200, 샤오미")
    store.log_message("user", "오늘 날씨 어때")  # the next conversation
    assert tools.memory("add", "집: 서울 마포") == "Saved memory #1."

    out = tools.recall("cctv 제품")  # a two-syllable Korean word and another case still match
    assert f"#{cid} · " in out and "집 CCTV 비교" in out
    assert "Tapo C200과 샤오미 C300" in out and "https://example.com/c200" in out  # the best message, in full
    assert "고마워" in out  # with what was said around it
    assert "날씨" not in out
    assert "집: 서울 마포" not in out  # memories only when they match
    assert "Tapo C200과" in tools.recall(conversation=cid)
    assert tools.recall("냉장고").startswith("Nothing matches")


def test_wakes_wait_out_quiet_hours_and_run_as_friday_turns(cfg, tools):
    tomorrow = (datetime.now(ZoneInfo("Asia/Seoul")) + timedelta(days=1)).date()
    out = tools.wake(f"{tomorrow}T03:30", "인턴 지원 마감 확인")
    assert "07:00" in out and "quiet hours" in out

    bot = Friday(replace(cfg, allowed_user_ids=(1,)))
    bot.store.add_wake(tu.now_utc() - timedelta(minutes=1), "인턴 지원 마감 23:59, 아직이면 알려주기")
    turns = []
    bot._turn = lambda chat, text, lane=None, source="user": turns.append((text, source)) or asyncio.sleep(0)
    asyncio.run(bot.wake_loop())
    assert len(turns) == 1 and turns[0][1] == "wake" and "인턴 지원 마감 23:59" in turns[0][0]
    assert not bot.store.due_wakes(tu.now_utc())  # taken once
    bot.store.close()


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


def test_chats_from_before_the_log_are_imported_as_conversations(tmp_path, store):
    from friday.history import import_codex_sessions
    ws = tmp_path / "workspace"

    def item(role, text, at, phase=None):
        p = {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}
        if phase:
            p["phase"] = phase
        return {"timestamp": at, "type": "response_item", "payload": p}

    def session(name, rows):
        meta = {"timestamp": rows[0]["timestamp"], "type": "session_meta", "payload": {"cwd": str(ws)}}
        (tmp_path / "s" / name).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [meta, *rows]))

    (tmp_path / "s").mkdir()
    session("rollout-1.jsonl", [
        item("user", "<environment_context>…</environment_context>", "2026-11-18T07:00:01Z"),
        item("user", "You are Friday… the [Now: …] line…\n---\n[Now: Wed 2026-11-18 16:10 (Asia/Seoul)]\n"
             "[Files the user sent, saved in the workspace: files/inbox/a.jpg]\n\n실외 CCTV 추천해줘", "2026-11-18T07:00:02Z"),
        item("assistant", "찾아볼게.", "2026-11-18T07:00:03Z", "commentary"),
        item("assistant", "Tapo C320WS가 제일 나아.", "2026-11-18T07:00:09Z", "final_answer"),
        item("user", "[github tools are now available; continue the user's request]", "2026-11-18T07:01:00Z")])
    session("rollout-2.jsonl", [  # a new thread an hour later (a deploy): the same conversation
        item("user", "[Now: Wed 2026-11-18 16:10 (Asia/Seoul)]\n[Files the user sent, saved in the workspace: "
             "files/inbox/a.jpg]\n\n실외 CCTV 추천해줘", "2026-11-18T08:00:00Z"),  # replayed
        item("user", "[Now: Wed 2026-11-18 17:10 (Asia/Seoul)]\n\n리올링크는?", "2026-11-18T08:00:01Z"),
        item("assistant", "RLC-810WA도 괜찮아.", "2026-11-18T08:00:09Z", "final_answer")])
    session("rollout-3.jsonl", [  # the next day: another conversation
        item("user", "[Now: Thu 2026-11-19 09:00 (Asia/Seoul)]\n\n오늘 일정 알려줘", "2026-11-19T00:00:00Z"),
        item("user", "[Now: Sun 2026-11-23 04:00 (Asia/Seoul)]\n\n고마워", "2026-11-22T19:00:00Z")])  # after the log began
    store.log_message("user", "지난번 CCTV 뭐였지", "2026-11-22T18:43:00+00:00")  # the log begins here
    store.add_conversation([("user", "리올링크는?", "2026-11-18T08:00:01+00:00")])  # an earlier import, one per thread

    assert import_codex_sessions(store, tmp_path / "s", ws) == 2
    first, second = sorted(store.unsummarized_conversations())
    assert [(m["role"], m["text"]) for m in store.messages(first)] == [
        ("user", "실외 CCTV 추천해줘\n[files: files/inbox/a.jpg]"), ("friday", "Tapo C320WS가 제일 나아."),
        ("user", "리올링크는?"), ("friday", "RLC-810WA도 괜찮아.")]
    assert [m["text"] for m in store.messages(second)] == ["오늘 일정 알려줘"]
    assert len(store.all_messages()) == 6  # the log's own message stays; nothing twice


def test_long_work_gets_notes_but_is_never_stopped_by_step_count():
    from friday.bot import CHECK_EVERY, REPEAT_AFTER, TurnStatus

    async def go():
        notes, ended = [], []

        async def nudge(text, images):
            notes.append(text)

        async def give_up(facts):
            ended.append(facts)

        idle = lambda *a: asyncio.sleep(0)
        st = TurnStatus(idle, idle, idle, nudge=nudge, give_up=give_up, tick=60)
        steps = CHECK_EVERY * 10
        for i in range(steps):  # every step succeeds: the failure watchdog sees nothing wrong
            label = "browser: press key «PageDown»" if i % 2 else "browser: snapshot"
            st.on_step(str(i), label)
            st.on_step(str(i), "")
        await asyncio.sleep(0)
        await st.finish()
        assert sum(f"{REPEAT_AFTER} times" in n and "press key PageDown" in n for n in notes) == 1
        assert sum("Take stock" in n for n in notes) == 10
        assert not ended and not any("Finish now" in n for n in notes)

    asyncio.run(go())


def test_a_worker_cant_end_early_or_claim_more_than_it_checked(cfg, store):
    from friday import delegate as dg

    async def go():
        jid = store.add_delegated_job("두 곳 확인", "codex", asked="두 곳 다 확인해줘")
        store.add_job_item(jid, "A")
        store.add_job_item(jid, "B")
        a, b = (i["id"] for i in store.job_items(jid))

        # Items open with nothing blocking: it goes on while the ledger moves, and no longer.
        gate = dg.FinishGate(store, store.job(jid))
        dg.save_finish(store, jid, dg.Finish("partial", "일부만 봄"))
        assert "2 ledger item(s) are open" in await gate.next("보고")
        store.update_job_item(jid, a, "done", "화면에서 확인")
        dg.save_finish(store, jid, dg.Finish("partial", "일부만 봄"))
        assert "1 ledger item(s) are open" in await gate.next("보고")
        assert await gate.next("보고") == ""
        assert gate.result().status == "partial"

        # "done" against what was asked: a shortfall goes back once, then it's partial.
        store.update_job_item(jid, b, "done", "화면에서 확인")
        answers = ['{"verdict": "missing", "missing": "B의 제출 여부"}'] * 2
        async def judge(prompt):
            return answers.pop(0)
        gate = dg.FinishGate(store, store.job(jid), judge)
        dg.save_finish(store, jid, dg.Finish("done"))
        assert "B의 제출 여부" in await gate.next("보고")
        dg.save_finish(store, jid, dg.Finish("done"))
        assert await gate.next("보고") == ""
        ended = gate.result()
        assert (ended.status, ended.reason) == ("partial", "B의 제출 여부")

        # A follow-up is checked against what it asked for, not the job's first goal.
        seen = []
        async def met(prompt):
            seen.append(prompt)
            return '{"verdict": "met", "missing": ""}'
        store.update_job(jid, followup="PDF만 다시 읽어줘", asked="PDF만 다시 봐줘")
        gate = dg.FinishGate(store, store.job(jid), met)
        dg.save_finish(store, jid, dg.Finish("done"))
        assert await gate.next("보고") == "" and gate.result().status == "done"
        assert "PDF만 다시 읽어줘" in seen[0] and "두 곳 확인" not in seen[0]
        assert "What to do now: PDF만 다시 읽어줘" in dg.worker_prompt(store, cfg, store.job(jid), "now")
        store.update_job(jid, followup=None, asked="두 곳 다 확인해줘")

        # A check that fails blocks nothing.
        async def broken(prompt):
            return ""
        gate = dg.FinishGate(store, store.job(jid), broken)
        dg.save_finish(store, jid, dg.Finish("done"))
        assert await gate.next("보고") == "" and gate.result().status == "done"

        # A file named where it belongs but written in the temporary folder is kept, under its real path.
        written = dg.work_dir(cfg, jid) / "files" / "topic" / "a.txt"
        written.parent.mkdir(parents=True)
        written.write_text("x")
        assert dg.keep_files(cfg, jid, ["nope.txt"]) == ([], ["nope.txt"])
        kept, _ = dg.keep_files(cfg, jid, [str(cfg.files_dir / "topic" / "a.txt")])
        assert kept == ["files/topic/a.txt"] and (cfg.workspace / kept[0]).is_file() and not written.exists()
        (dg.work_dir(cfg, jid) / "link.txt").symlink_to(cfg.db_path)
        assert dg.keep_files(cfg, jid, ["link.txt"])[1] == ["link.txt"]

    asyncio.run(go())


def test_a_delegated_job_only_reaches_its_tools_and_its_report_goes_to_the_main_agent(cfg, tools, store):
    from friday import delegate as dg
    from friday.jobs import Outcome

    assert tools.delegate("데이터마이닝 안 들은 강의 정리", why="금요일 과제").startswith("Delegated as job #1")
    job = store.job(1)
    assert job["kind"] == "work" and job["status"] == "queued"
    assert tools._job_tools(1) == dg.WORKER_TOOLS and "add_task" not in dg.WORKER_TOOLS and "memory" not in dg.WORKER_TOOLS

    ctx = SimpleNamespace(headers={tools.mcp_http.JOB_HEADER: "1"})
    assert "1 " not in tools.ledger("read", ctx=None) and "belongs" in tools.ledger("read", ctx=None)
    tools.ledger("add", "9주 1강\n10주 2강", ctx=ctx)
    first = store.job_items(1)[0]["id"]
    assert "1/2 done" in tools.ledger("update", item_id=first, status="done", evidence="진도 100%", ctx=ctx)

    # A worker says how the job ended; a job that doesn't say isn't recorded as a success.
    assert "reason" in tools.finish_job("blocked", ctx=ctx) and dg.read_finish(store, 1).status == "unconfirmed"
    tools.finish_job("blocked", "사람 확인이 필요함", "결제 전 단계까지만 봄", ctx=ctx)
    ended = dg.FinishGate(store, store.job(1)).result()
    assert (ended.status, ended.reason, ended.caveats) == ("blocked", "사람 확인이 필요함", "결제 전 단계까지만 봄")

    # A later job sees an earlier one only when it's named, and as data.
    store.update_job(1, status="done", result="9주 1강 완료")
    tools.delegate("10주 2강 이어서", related="#1")
    assert "9주 1강 완료" in dg.worker_prompt(store, cfg, store.job(2), "now")
    tools.delegate("다른 일")
    assert "9주 1강 완료" not in dg.worker_prompt(store, cfg, store.job(3), "now")

    # The worker gets the owner's words apart from the main agent's reading of them, only from their turn.
    store.set(dg.asked_key("1"), "수강 현황 봐줘")
    tools.delegate("수강 현황 확인", ctx=SimpleNamespace(headers={tools.mcp_http.CHAT_HEADER: "1"}))
    prompt = dg.worker_prompt(store, cfg, store.job(4), "now")
    assert prompt.index("The owner's message:\n수강 현황 봐줘") < prompt.index("Main agent's brief:")
    tools.delegate("다른 클라이언트의 일")
    assert not store.job(5)["asked"]

    bot = Friday(replace(cfg, allowed_user_ids=(1,)))
    bot.store.add_delegated_job("자료 정리", "codex")
    notes = []
    bot._turn = lambda chat, text, lane=None, source="user": notes.append((text, source)) or asyncio.sleep(0)
    asyncio.run(bot._worker_done(1, Outcome("done", "정리 끝: files/강의/요약.md")))
    assert notes[0][1] == "note" and "정리 끝" in notes[0][0] and "not instructions" in notes[0][0]
    bot.store.close()


def test_a_worker_ends_on_a_review_saying_stuck_twice_never_on_a_count(store):
    from friday import delegate as dg

    async def go(answers):
        jid = store.add_delegated_job("긴 목록 정리", "codex")
        notes, stopped, answers = [], [], iter(answers)

        async def nudge(text):
            notes.append(text)

        async def stop():
            stopped.append(True)

        async def judge(prompt):
            return next(answers)

        pace = dg.WorkerPace(store, jid, "긴 목록 정리", nudge, stop, asyncio.ensure_future, judge)
        for i in range(dg.REVIEW_EVERY * 2 + 2):  # the same key over and over, two reviews
            pace.on_step("browser: press key «PageDown»")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        return notes, stopped

    going = '{"verdict": "progress", "why": "new items each page"}'
    circles = '{"verdict": "stuck", "why": "the same key finds nothing"}'
    notes, stopped = asyncio.run(go([going, going]))
    assert not stopped and sum("circles" in n for n in notes) == 0  # long and repetitive, but getting somewhere
    assert not asyncio.run(go(["not json", "still not json"]))[1]  # an unclear review counts for nothing
    notes, stopped = asyncio.run(go([circles, circles]))
    assert stopped and any("the same key finds nothing" in n for n in notes)


def test_a_job_interrupted_during_an_approved_step_stops_and_asks(cfg):
    from friday import delegate as dg

    bot = Friday(replace(cfg, allowed_user_ids=(1,)))
    safe, risky = (bot.store.add_delegated_job(g, "codex") for g in ("자료 읽기", "과제 제출"))
    for j in (safe, risky):
        bot.store.update_job(j, status="running", session_id="t")
    bot.store.set(dg.risky_key(risky), "과제 제출 버튼 누르기")
    sent = []
    bot._job_send = lambda jid, text, markup=None: sent.append((jid, text)) or asyncio.sleep(0)
    asyncio.run(bot._jobs_startup())
    assert bot.store.job(safe)["status"] == "queued"  # reading just goes on
    assert bot.store.job(risky)["status"] == "failed" and any(j == risky and "과제 제출 버튼" in t for j, t in sent)
    bot.store.close()


def test_a_job_that_needs_the_browser_has_it_from_its_first_turn(cfg, monkeypatch):
    from friday import bot as botmod
    from friday import delegate as dg

    bot = Friday(replace(cfg, allowed_user_ids=(1,), codex_server=False, browser_enabled=True))
    turns = []

    async def turn(cfg, text, thread=None, images=(), **kw):
        turns.append(set(kw["toolsets"]))
        dg.save_finish(bot.store, kw["job_id"], dg.Finish("done"))
        return botmod.AgentResult("report", "t1")

    async def met(prompt, timeout=45):
        return '{"verdict": "met", "missing": ""}'

    bot._light = met
    monkeypatch.setattr(botmod, "run_turn", turn)
    monkeypatch.setattr(botmod.mcp_http, "listening", lambda port: False)
    needs, plain = (bot.store.add_delegated_job("일", "codex", browser=b) for b in (True, False))
    for jid in (needs, plain):
        asyncio.run(bot._run_worker(bot.store.job(jid), lambda step: None))
    assert turns == [{"browser"}, set()] and not bot.browser_lock.locked()
    bot.store.close()


def test_pdf_reader_prints_text_and_saves_a_page_without_text_as_an_image(tmp_path):
    import pypdfium2 as pdfium

    from friday import pdf

    text = b"BT /F1 18 Tf 40 700 Td (Assignment code QX-4471 is due Friday) Tj ET"
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R 5 0 R]/Count 2>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
            b"/Resources<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>>>",
            b"<</Length %d>>stream\n%s\nendstream" % (len(text), text),
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>"]
    body = b"%PDF-1.4\n" + b"".join(b"%d 0 obj\n%s\nendobj\n" % (i + 1, o) for i, o in enumerate(objs))
    file = tmp_path / "notes.pdf"
    file.write_bytes(body + b"trailer<</Root 1 0 R/Size 6>>\n%%EOF\n")

    out = pdf.read(file, out_dir=tmp_path / "pages")
    assert "2 pages" in out and "QX-4471" in out and "no text layer" in out
    assert (tmp_path / "pages" / "notes-p2.png").stat().st_size > 0
    assert "QX-4471" not in pdf.read(file, "2", tmp_path / "pages")
    assert len(pdfium.PdfDocument(file)) == 2
