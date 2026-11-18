"""Task cards and the Mini App planner (planner.py)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from friday import miniapp, planner

TZ = "Asia/Seoul"
NOW = datetime(2026, 11, 18, 17, 50, tzinfo=ZoneInfo(TZ))


def test_task_list_buttons_act_on_the_task_and_fit_telegram(store):
    tasks = [store.add_task(f"task {i}", NOW + timedelta(days=i)) for i in range(10)]
    text, markup = planner.tasks_card(store.open_tasks(), TZ, NOW, page=1)
    assert "Open tasks (10)" in text and "task 8" in text and "task 0" not in text  # page 2 of 2
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"tl:d:{tasks[8].id}:1" in data and all(len(d.encode()) <= 64 for d in data)

    moved = planner.move_to_tomorrow(store, tasks[0], TZ, NOW)
    assert moved.due_at.astimezone(ZoneInfo(TZ)) == datetime(2026, 11, 19, 17, 50, tzinfo=ZoneInfo(TZ))


def test_planner_applies_only_what_it_showed(store, tmp_path):
    a, b, c = (store.add_task(t, NOW + timedelta(days=1)) for t in ("report", "email", "slides"))
    hidden = store.add_task("not in the planner")
    spec, snap = planner.spec([a, b, c], [], [], TZ, NOW)
    assert spec["tasks"][0] == {"id": a.id, "t": "report", "d": "2026-11-19T17:50"}

    # What the page sends comes back through the Mini App's encryption with its kind.
    url = miniapp.url("https://x/app/", tmp_path, 7, spec)
    key = miniapp.AppKey(tmp_path)
    values = {"done": [a.id, hidden.id], "del": [b.id], "edit": [{"id": c.id, "d": "2026-11-21T09:00"}],
              "new": [{"t": "call the bank", "d": ""}], "evdel": [5]}
    sent = key.decrypt(miniapp.encrypt_like_the_page(key.public_b64(), {"rid": 7, "kind": "planner", "values": values}))
    assert url.startswith("https://x/app/#") and sent["kind"] == "planner"

    lines, gone = planner.apply(store, sent["values"], snap, TZ, NOW)
    assert store.task(a.id).status == "done" and store.task(hidden.id).status == "open"  # not shown: untouched
    assert store.task(b.id).status == "cancelled" and gone == []
    assert store.task(c.id).due_at.astimezone(ZoneInfo(TZ)).day == 21
    assert any(t.title == "call the bank" for t in store.open_tasks()) and len(lines) == 4
