import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

from friday import bot as botmod
from friday.forms import login_spec, parse_fields
from friday.miniapp import AppKey, encrypt_like_the_page
from friday.vault import Vault


def _bot(cfg):
    """A bot whose Telegram calls are recorded: (sent texts, edited texts)."""
    f, sent, edited = botmod.Friday(cfg), [], []

    async def send_message(chat, text, **k):
        sent.append(text)
        return SimpleNamespace(message_id=len(sent))

    async def edit_message_text(text, **k):
        edited.append(text)

    f.app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message, edit_message_text=edit_message_text))
    return f, sent, edited


async def _reply(f, rid, *lines):
    """The owner replies to request rid's message, one value per line."""
    async def delete():
        return True

    await f._login_reply(SimpleNamespace(text="\n".join(lines), chat_id=1, delete=delete), f.store.login_request(rid))


def test_login_reply_goes_to_the_vault_only(cfg, caplog):
    f, sent, edited = _bot(replace(cfg, allowed_user_ids=(1,)))
    extra = parse_fields('[{"name": "member_id", "label": "Member ID"}]', sensitive_default=True)
    rid = f.store.add_login_request("portal", "portal.example.com", "monthly report", spec=login_spec(
        "portal", "portal.example.com", "monthly report", extra))
    asyncio.run(f._send_login_request(rid))
    assert "Member ID" in sent[-1] and "portal.example.com" in sent[-1]

    asyncio.run(_reply(f, rid, "alex", "pw secret 1"))  # a line short: nothing saved
    assert Vault(cfg.vault_dir).get("portal") is None and f.store.login_request(rid)["status"] == "pending"

    asyncio.run(_reply(f, rid, " alex ", "pw secret 1", "2024123"))
    login = Vault(cfg.vault_dir).get("portal")
    assert (login.username, login.password, login.extra, login.trusted) == (
        "alex", "pw secret 1", {"member_id": "2024123"}, ("portal.example.com",))
    assert f.store.login_request(rid)["status"] == "saved"
    assert f.store.take_result(rid) == {}  # nothing for the agent
    assert "pw secret" not in caplog.text and "2024123" not in caplog.text
    f.store.close()


def test_mini_app_answer_is_decrypted_and_routed_like_a_reply(cfg, caplog):
    f, sent, edited = _bot(replace(cfg, allowed_user_ids=(1,)))
    rid = f.store.add_login_request("portal", "portal.example.com", "", spec=login_spec("portal", "portal.example.com"))
    asyncio.run(f._send_login_request(rid))
    assert f.store.login_request(rid)["form_message_id"]  # sent with the 📝 Open form keyboard

    def answer(payload):
        async def delete():
            return True
        data = encrypt_like_the_page(AppKey(cfg.vault_dir).public_b64(), payload)
        msg = SimpleNamespace(web_app_data=SimpleNamespace(data=data), chat_id=1, delete=delete)
        asyncio.run(f.on_app_data(SimpleNamespace(message=msg), None))

    values = {"username": "alex", "password": "pw secret 1"}
    answer({"rid": rid + 1, "values": values})  # not a pending request: nothing saved
    assert Vault(cfg.vault_dir).get("portal") is None
    answer({"rid": rid, "values": values})
    assert Vault(cfg.vault_dir).get("portal").password == "pw secret 1"
    assert f.store.login_request(rid)["status"] == "saved" and sent[-1] == "✅ Saved portal"
    assert (cfg.vault_dir / "form_key.pem").stat().st_mode & 0o777 == 0o600 and "pw secret" not in caplog.text
    f.store.close()


FIELDS = json.dumps([
    {"name": "api_key", "label": "API key", "type": "password"},
    {"name": "account", "label": "Account", "type": "choice",
     "options": [{"value": "work", "label": "Work"}, {"value": "home", "label": "Home"}]},
    {"name": "notify", "label": "Notify me", "type": "checkbox"},
])


def test_request_form_returns_plain_values_and_only_stores_sensitive_ones(cfg, tools, monkeypatch):
    f, sent, edited = _bot(cfg)
    monkeypatch.setattr(tools, "store", f.store)  # the tool and the bot share one database
    real_sleep = asyncio.sleep

    async def user_answers(_):  # instead of the tool's 2-second poll: the owner replies
        await real_sleep(0)
        rid = f.store.conn.execute("SELECT max(id) FROM login_requests").fetchone()[0]
        if f.store.login_request(rid)["status"] == "pending":
            await _reply(f, rid, "sk-very-secret", "Work", "yes")

    monkeypatch.setattr(tools.asyncio, "sleep", user_answers)
    assert asyncio.run(tools.request_form("OpenAI", FIELDS)).startswith("Not requested: save_as is required")
    out = asyncio.run(tools.request_form("OpenAI", FIELDS, "key for summaries", save_as="openai"))
    assert "sk-very-secret" not in out
    assert json.loads(out.removeprefix("Submitted: ")) == {
        "account": "work", "notify": True, "api_key": "stored as openai.api_key"}
    secret = Vault(cfg.vault_dir).get("openai")
    assert secret.extra == {"api_key": "sk-very-secret"} and secret.domains == () and not secret.password
    row = f.store.conn.execute("SELECT * FROM login_requests").fetchone()
    assert row["status"] == "saved" and row["result"] is None  # erased once the tool read it
    assert "sk-very" not in json.dumps(dict(row)) and "sk-very" not in " ".join(sent + edited)
    f.store.close()


def test_chat_request_returns_at_once_and_the_answer_continues_the_thread(cfg, tools, monkeypatch):
    f, sent, edited = _bot(replace(cfg, allowed_user_ids=(1,)))
    monkeypatch.setattr(tools, "store", f.store)
    f.store.set("thread_id", "thread-1")
    notes = []
    monkeypatch.setattr(f, "_turn", lambda chat, note: notes.append(note) or asyncio.sleep(0))
    chat = SimpleNamespace(headers={"X-Friday-Chat": "1"})  # what the agent sends for a chat turn
    fields = json.dumps([{"name": "api_key", "label": "API key", "type": "password"},
                         {"name": "account", "label": "Account", "type": "text"}])

    out = asyncio.run(tools.request_form("OpenAI", fields, "key for summaries", save_as="openai", ctx=chat))
    assert out.startswith("Sent to the user") and not notes  # returned without waiting
    rid = f.store.conn.execute("SELECT max(id) FROM login_requests").fetchone()[0]
    assert [r["kind"] for r in f.store.pending_outbox()] == ["login_request"]

    asyncio.run(_reply(f, rid, "sk-very-secret", "work account"))
    assert len(notes) == 1 and "sk-very-secret" not in notes[0]
    assert '"account": "work account"' in notes[0] and "stored as openai.api_key" in notes[0]
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
