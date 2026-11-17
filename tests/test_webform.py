import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

from friday import bot as botmod
from friday.vault import Vault
from friday.webform import FormKey, encrypt_like_the_page, login_spec, parse_fields


def _bot(cfg):
    """A bot whose Telegram calls are recorded: (sent texts, deleted message count)."""
    f, sent, deleted = botmod.Friday(cfg), [], []

    async def send_message(chat, text, **k):
        sent.append(text)

    async def noop(*a, **k):
        deleted.append(True)

    f.app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message, delete_message=noop, edit_message_text=noop))
    return f, sent, deleted


def _form_answer(f, payload, deleted):
    """The service message Telegram delivers after the page's sendData."""
    async def delete():
        deleted.append(True)

    data = encrypt_like_the_page(FormKey(f.cfg.vault_dir).public_b64(), payload)
    return SimpleNamespace(message=SimpleNamespace(web_app_data=SimpleNamespace(data=data), chat_id=1, delete=delete))


def test_login_form_answer_goes_to_the_vault_only_for_a_pending_request(cfg, caplog):
    f, sent, deleted = _bot(cfg)
    extra = parse_fields('[{"name": "student_id", "label": "학번"}]', sensitive_default=True)
    rid = f.store.add_login_request("school", "lms.example.edu", "과제 제출", spec=login_spec(
        "school", "lms.example.edu", "과제 제출", extra))
    f.store.set_login_request_form_message(rid, 78)
    values = {"username": " student ", "password": "pw secret 1", "student_id": "2024123"}

    asyncio.run(f.on_form_data(_form_answer(f, {"rid": rid + 1, "values": values}, deleted), None))
    assert Vault(cfg.vault_dir).get("school") is None and "없는 요청" in sent[-1]

    asyncio.run(f.on_form_data(_form_answer(f, {"rid": rid, "values": values}, deleted), None))
    login = Vault(cfg.vault_dir).get("school")
    assert (login.username, login.password, login.extra, login.trusted) == (
        "student", "pw secret 1", {"student_id": "2024123"}, ("lms.example.edu",))
    assert f.store.login_request(rid)["status"] == "saved" and sent[-1] == "✅ school 저장됨"
    assert f.store.take_result(rid) == {}  # nothing for the agent
    assert (cfg.vault_dir / "form_key.pem").stat().st_mode & 0o777 == 0o600
    assert "pw secret" not in caplog.text and "2024123" not in caplog.text
    f.store.close()


def test_request_form_returns_plain_values_and_only_stores_sensitive_ones(cfg, tools, monkeypatch):
    f, sent, deleted = _bot(cfg)
    monkeypatch.setattr(tools, "store", f.store)  # the tool and the bot share one database
    real_sleep = asyncio.sleep

    async def user_answers(_):  # instead of the tool's 2-second poll: the owner fills in the form
        await real_sleep(0)
        rid = f.store.conn.execute("SELECT max(id) FROM login_requests").fetchone()[0]
        if f.store.login_request(rid)["status"] == "pending":
            f.store.set_login_request_form_message(rid, 5)
            values = {"api_key": "sk-very-secret", "account": "업무용", "notify": True}
            await f.on_form_data(_form_answer(f, {"rid": rid, "values": values}, deleted), None)

    monkeypatch.setattr(tools.asyncio, "sleep", user_answers)
    fields = json.dumps([
        {"name": "api_key", "label": "API 키", "type": "password"},
        {"name": "account", "label": "계정", "type": "choice",
         "options": [{"value": "work", "label": "업무용"}, {"value": "home", "label": "개인용"}]},
        {"name": "notify", "label": "알림 받기", "type": "checkbox"},
    ])
    assert asyncio.run(tools.request_form("OpenAI", fields)).startswith("Not requested: save_as is required")
    out = asyncio.run(tools.request_form("OpenAI", fields, "요약에 쓸 키", save_as="openai"))
    assert "sk-very-secret" not in out
    assert json.loads(out.removeprefix("Submitted: ")) == {
        "account": "work", "notify": True, "api_key": "stored as openai.api_key"}
    secret = Vault(cfg.vault_dir).get("openai")
    assert secret.extra == {"api_key": "sk-very-secret"} and secret.domains == () and not secret.password
    row = f.store.conn.execute("SELECT * FROM login_requests").fetchone()
    assert row["status"] == "saved" and row["result"] is None  # erased once the tool read it
    assert "sk-very" not in json.dumps(dict(row)) and sent[-1] == "✅ openai 저장됨 (나머지는 전달함)"
    f.store.close()


def test_chat_request_returns_at_once_and_the_answer_continues_the_thread(cfg, tools, monkeypatch):
    f, sent, deleted = _bot(replace(cfg, allowed_user_ids=(1,)))
    monkeypatch.setattr(tools, "store", f.store)
    f.store.set("thread_id", "thread-1")
    notes = []
    monkeypatch.setattr(f, "_turn", lambda chat, note: notes.append(note) or asyncio.sleep(0))
    chat = SimpleNamespace(headers={"X-Friday-Chat": "1"})  # what Codex sends for a chat turn
    fields = json.dumps([{"name": "api_key", "label": "API 키", "type": "password"},
                         {"name": "account", "label": "계정", "type": "text"}])

    out = asyncio.run(tools.request_form("OpenAI", fields, "요약에 쓸 키", save_as="openai", ctx=chat))
    assert out.startswith("Sent to the user") and not notes  # returned without waiting
    rid = f.store.conn.execute("SELECT max(id) FROM login_requests").fetchone()[0]
    assert [r["kind"] for r in f.store.pending_outbox()] == ["login_request"]

    f.store.set_login_request_form_message(rid, 5)
    values = {"api_key": "sk-very-secret", "account": "업무용"}
    asyncio.run(f.on_form_data(_form_answer(f, {"rid": rid, "values": values}, deleted), None))
    assert len(notes) == 1 and "sk-very-secret" not in notes[0]
    assert '"account": "업무용"' in notes[0] and "stored as openai.api_key" in notes[0]
    assert f.store.get(f"followup:login:{rid}") is None
    f.store.close()


def test_mcp_http_requires_the_bearer_token(cfg, tools):
    from starlette.testclient import TestClient

    from friday import mcp_http

    token = mcp_http.token(cfg)
    assert mcp_http.token(cfg) == token and mcp_http.token_path(cfg).stat().st_mode & 0o777 == 0o600
    app = mcp_http.BearerAuth(tools.server.streamable_http_app(host="127.0.0.1"), token)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    head = {"Accept": "application/json, text/event-stream", "Host": "127.0.0.1:8765"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        assert c.post("/mcp", json=init, headers=head).status_code == 401
        assert c.post("/mcp", json=init, headers={**head, "Authorization": "Bearer wrong"}).status_code == 401
        ok = c.post("/mcp", json=init, headers={**head, "Authorization": f"Bearer {token}"})
        assert ok.status_code == 200, ok.text
