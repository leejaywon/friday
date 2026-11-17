import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from friday import bot as botmod
from friday import browser
from friday.vault import Vault


def test_vault_round_trip_and_listing_hides_secrets(tmp_path):
    v = Vault(tmp_path / "vault")
    v.add("school", "student", "hunter2-secret", ["lms.example.edu"])
    assert (v.key_path.stat().st_mode & 0o777) == 0o600
    assert b"hunter2" not in v.path.read_bytes()  # encrypted at rest
    again = Vault(tmp_path / "vault").get("school")
    assert (again.username, again.password) == ("student", "hunter2-secret")
    assert "hunter2" not in repr(again) and "hunter2" not in str(v.names())
    assert v.names_for("www.lms.example.edu") == ["school"] and v.names_for("evil-lms.example.edu") == []
    with pytest.raises(ValueError):
        v.add("x", "u", "p", ["https://lms.example.edu/login"])
    assert v.remove("school") and v.get("school") is None


def test_fill_login_refuses_wrong_site_without_asking(tools, monkeypatch):
    Vault(tools.cfg.vault_dir).add("school", "student", "pw", ["lms.example.edu"])
    assert browser._allowed("https://sso.lms.example.edu/login", ("lms.example.edu",))
    for url in ("http://lms.example.edu/", "https://lms.example.edu.evil.com/", "https://xlms.example.edu/"):
        assert not browser._allowed(url, ("lms.example.edu",))

    async def wrong_site(domains):
        return False, "evil.example"

    async def never(*a):
        raise AssertionError("must not fill")

    monkeypatch.setattr(browser, "check_site", wrong_site)
    monkeypatch.setattr(browser, "fill_login", never)
    assert asyncio.run(tools.fill_login("school")).startswith("Wrong site: evil.example")
    assert tools.store.pending_outbox() == []  # no approval was asked for


def test_trust_once_per_site_but_never_on_a_wrong_site(tools, monkeypatch):
    Vault(tools.cfg.vault_dir).add("school", "student", "pw", ["lms.example.edu"])
    site, asked, filled = [(True, "sso.lms.example.edu")], [], []

    async def check_site(domains):
        return site[0]

    async def ask(kind, text, screenshot=None):
        asked.append(text)
        return "approved"

    async def fill(login):
        filled.append(login.name)
        return "Filled and submitted"

    monkeypatch.setattr(browser, "check_site", check_site)
    monkeypatch.setattr(browser, "fill_login", fill)
    monkeypatch.setattr(tools, "_ask", ask)
    assert asyncio.run(tools.fill_login("school")) == "Filled and submitted" and len(asked) == 1
    assert asyncio.run(tools.fill_login("school")) == "Filled and submitted" and len(asked) == 1  # trusted now
    site[0] = (False, "evil.example")
    assert asyncio.run(tools.fill_login("school")).startswith("Wrong site") and filled == ["school", "school"]


def test_approval_is_decided_once(store):
    aid = store.add_approval("login", "use school?")
    assert store.decide_approval(aid, "expired")
    assert not store.decide_approval(aid, "approved")  # a late tap can't revive it
    assert store.approval(aid)["status"] == "expired"


def test_scheduled_runs_due_and_preapproved_logins(store):
    now = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
    later = store.add_scheduled_run(now + timedelta(hours=1), "submit hw", "auto", ["school"])
    soon = store.add_scheduled_run(now - timedelta(minutes=1), "check grades", "approve")
    assert store.next_due_run(now)["id"] == soon
    assert store.set_run_status(soon, "cancelled", only_from="pending")
    assert store.next_due_run(now) is None and store.next_due_run(now + timedelta(hours=1))["id"] == later
    assert not store.run_allows_login(later, "school")  # only while it runs
    assert store.set_run_status(later, "running", only_from="pending")
    assert store.run_allows_login(later, "school") and not store.run_allows_login(later, "naver")


def test_login_reply_goes_to_the_vault_not_the_agent(cfg, caplog):
    f = botmod.Friday(cfg)
    asked, edits, deleted = [], [], []

    async def ask(*a, **k):
        asked.append(a)

    async def edit(text, chat_id, message_id):
        edits.append((text, message_id))

    async def delete():
        deleted.append(True)
        return True

    f.ask, f.app = ask, SimpleNamespace(bot=SimpleNamespace(edit_message_text=edit))
    rid = f.store.add_login_request("school", "lms.example.edu", "과제 제출")
    f.store.set_login_request_message(rid, 77)
    msg = SimpleNamespace(text="student\npass word 1", chat_id=1, delete=delete,
                          reply_to_message=SimpleNamespace(message_id=77))
    asyncio.run(f.on_text(SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=1)), None))
    login = Vault(f.cfg.vault_dir).get("school")
    assert (login.username, login.password, login.trusted) == ("student", "pass word 1", ("lms.example.edu",))
    assert not asked and deleted and len(edits) == 1
    assert f.store.login_request(rid)["status"] == "saved" and "pass word" not in caplog.text
    f.store.close()
