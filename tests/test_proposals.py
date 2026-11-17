"""Items read from an image are proposed, then added only once the user taps "add all"."""

import asyncio
import json

from friday import bot as botmod

ITEMS = [
    {"kind": "task", "title": "분기 보고서", "due": "2099-11-20T23:59",
     "submission": {"site": "portal.example.com", "what": "PDF 보고서"}},
    {"kind": "reminder", "title": "발표 준비", "when": "2099-11-19T09:00"},
    {"kind": "event", "title": "팀 회의", "start": "2099-11-21T14:00"},
]


def test_propose_stores_and_queues(tools):
    assert tools.propose_items(json.dumps(ITEMS), "팀 공지").startswith("Proposed 3 item(s) as #P1")
    p = tools.store.proposal(1)
    assert p["status"] == "pending" and p["note"] == "팀 공지"
    assert p["items"][0]["due"] == "2099-11-20T23:59+09:00"
    assert p["items"][2]["end"] == "2099-11-21T15:00+09:00"  # default: one hour
    [row] = tools.store.pending_outbox()
    assert (row["kind"], row["text"]) == ("proposal", "1")
    assert tools.store.open_tasks() == []  # nothing added before the tap


def test_propose_refuses_bad_items(tools):
    past = [{"kind": "task", "title": "x", "due": "2020-01-01T09:00"}]
    assert "in the past" in tools.propose_items(json.dumps(past))
    secret = [{"kind": "task", "title": "비밀번호는 hunter22"}]
    assert "Refused" in tools.propose_items(json.dumps(secret, ensure_ascii=False))
    assert tools.store.pending_outbox() == []


def test_accept_adds_items_exactly_once(cfg, tools):
    f = botmod.Friday(cfg)
    pid = f.store.add_proposal(tools.proposal_items(json.dumps(ITEMS), "Asia/Seoul"))

    async def go():
        return await f._accept_proposal(pid), await f._accept_proposal(pid)

    first, second = asyncio.run(go())
    assert len(first) == 3 and second is None  # a double tap adds nothing
    titles = sorted(t.title for t in f.store.open_tasks())
    assert titles == ["분기 보고서", "팀 회의"]  # no calendar connected: the event became a task
    assert "portal.example.com" in next(t.notes for t in f.store.open_tasks() if t.title == "분기 보고서")
    assert [r.text for r in f.store.active_reminders()] == ["발표 준비"]
    assert f.store.proposal(pid)["status"] == "accepted"
    f.store.close()
