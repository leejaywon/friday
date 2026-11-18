"""The app-server client (codex_server.py) against a fake `codex app-server` (fake_app_server.py)."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from friday import bot as botmod
from friday import codex_server
from friday.agent import AgentResult
from friday.codex_server import CodexServer, CodexServerError

FAKE = Path(__file__).with_name("fake_app_server.py")


@pytest.fixture
def server(cfg, tmp_path, monkeypatch):
    exe = tmp_path / "codex"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    exe.chmod(0o755)
    log = tmp_path / "rpc.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setattr(codex_server, "mcp_endpoints", lambda toolsets: (True, "browser" in toolsets))
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    s = CodexServer(replace(cfg, codex_bin=str(exe), codex_server=True))
    s.calls = lambda: [json.loads(line) for line in log.read_text().splitlines()]
    return s


def test_turn_result_errors_and_timeout(server, tmp_path):
    img = tmp_path / "a.png"

    async def go():
        r = await server.run("hi", None, [img], toolsets={"browser"})
        assert (r.text, r.thread_id, r.errors, r.mcp_servers, r.input_tokens) == (
            "reply: hi (1 images)", "t1", [], {"friday"}, 1234)
        assert server.version == "0.0.1"
        failed = await server.run("FAIL", "t1")
        assert not failed.ok and failed.errors == ["usage limit reached"]
        server.cfg = replace(server.cfg, codex_timeout_seconds=1)
        slow = await server.run("HANG", "t1")
        assert "longer than 1s" in slow.errors[0] and slow.thread_id == "t1"
        server.cfg = replace(server.cfg, codex_timeout_seconds=60)
        steps = []
        hung = asyncio.create_task(server.run("HANG", "t1", on_step=lambda i, label: steps.append(label), key="2"))
        while not server.stop_turn("2"):  # the [Stop] button, once the turn has started
            await asyncio.sleep(0.01)
        assert (await hung).errors == ["stopped"] and steps == ["browser: click"]
        ok = await server.run("again", "t1")  # the server is still usable
        assert await server.light("sum up", "small-model", "low") == "reply: sum up (0 images)"
        await server.close()
        return ok

    assert asyncio.run(go()).text == "reply: again (0 images)"
    methods = [c["method"] for c in server.calls()]
    assert "turn/interrupt" in methods and methods.count("initialize") == 1
    start = next(c["params"] for c in server.calls() if c["method"] == "turn/start")
    assert start["input"][0] == {"type": "localImage", "path": str(img)}


def test_crash_restarts_and_the_bot_falls_back_to_exec(server, cfg, monkeypatch):
    friday = botmod.Friday(cfg)
    friday.codex = server
    execs = []

    async def run_codex(cfg, prompt, thread, images, **kw):
        execs.append((prompt, thread))
        return AgentResult("from exec", thread, [])

    monkeypatch.setattr(botmod, "run_turn", run_codex)

    async def go():
        first = await server.run("hello", None)
        with pytest.raises(CodexServerError) as e:
            await server.run("CRASH", first.thread_id)
        assert e.value.partial  # it had started a tool call
        again = await server.run("hello again", first.thread_id)  # a new process resumes the thread
        fallback = await friday._codex_turn("CRASH now", "t1", [], set())
        await server.close()
        return again, fallback

    again, fallback = asyncio.run(go())
    friday.store.close()
    assert again.text == "reply: hello again (0 images)"
    assert fallback.text == "from exec" and execs == [(botmod.RETRY_NOTE + "CRASH now", "t1")]
    calls = server.calls()
    assert len({c["pid"] for c in calls if c["method"] == "initialize"}) == 2
    assert [c["params"]["threadId"] for c in calls if c["method"] == "thread/resume"] == ["t1"]


def test_new_toolset_reloads_the_thread(server):
    server.cfg = replace(server.cfg, github_token_set=True)

    async def go():
        r = await server.run("one", None, toolsets={"browser"})
        await server.run("two", r.thread_id, toolsets={"browser"})  # same config: no reload
        await server.run("three", r.thread_id, toolsets={"browser", "github"})
        await server.close()

    asyncio.run(go())
    calls = [c for c in server.calls() if c["method"].startswith("thread/")]
    assert [c["method"] for c in calls] == ["thread/start", "thread/unsubscribe", "thread/resume"]
    assert "mcp_servers.github.url" not in calls[0]["params"]["config"]
    assert calls[0]["params"]["config"]["mcp_servers.friday.http_headers"] == {"X-Friday-Chat": "1"}
    assert "mcp_servers.github.url" in calls[2]["params"]["config"]
