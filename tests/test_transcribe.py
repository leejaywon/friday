import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from friday import bot as botmod
from friday.transcribe import TranscriptionError, transcribe


def run(path, handler, **kw):
    kw = {"provider": "groq", "api_key": "k", **kw}
    return asyncio.run(transcribe(path, transport=httpx.MockTransport(handler), **kw))


@pytest.fixture
def voice(tmp_path):
    p = tmp_path / "180930_voice.ogg"
    p.write_bytes(b"OggS fake opus")
    return p


def test_sends_file_model_and_key(voice):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], req.read()
        return httpx.Response(200, json={"text": "  내일 아침 9시에 알려줘 \n"})

    assert run(voice, handler, language="ko") == "내일 아침 9시에 알려줘"
    assert seen["url"].startswith("https://api.groq.com/") and seen["auth"] == "Bearer k"
    assert b"whisper-large-v3-turbo" in seen["body"] and b"OggS fake opus" in seen["body"]


def test_http_error_is_reported_without_the_key(voice):
    def handler(req):
        return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})

    with pytest.raises(TranscriptionError) as e:
        run(voice, handler, api_key="secret-key")
    assert "401" in str(e.value) and "secret-key" not in str(e.value)


# --- the bot's handling of a recording ----------------------------------------

class FakeMsg(SimpleNamespace):
    async def reply_text(self, text):
        self.replies.append(text)


def make_bot(cfg, monkeypatch, transcript):
    f = botmod.Friday(replace(cfg, transcribe_provider="groq", transcribe_api_key="k"))
    f.asked = []

    async def ask(chat_id, text, files=None, images=None):
        f.asked.append((text, files))

    async def nothing(*a, **k):
        pass

    async def fake_transcribe(path, **kw):
        if isinstance(transcript, Exception):
            raise transcript
        return transcript

    f.ask, f.say, f._keep_typing = ask, nothing, nothing
    monkeypatch.setattr(botmod, "transcribe", fake_transcribe)
    dest = f.cfg.files_dir / "inbox" / "180930_voice.ogg"
    dest.write_bytes(b"OggS")
    return f, dest, str(dest.relative_to(f.cfg.workspace))


def msg(**kw):
    return FakeMsg(**{"voice": object(), "audio": None, "caption": None, "chat_id": 1, "replies": [], **kw})


def test_voice_message_goes_to_the_agent_as_text(cfg, monkeypatch):
    f, dest, rel = make_bot(cfg, monkeypatch, "remind me at 9")
    asyncio.run(f._on_recording(msg(), dest, rel))
    [(text, files)] = f.asked
    assert text.endswith("remind me at 9") and "voice message" in text and not files
    assert (dest.parent / "180930_voice.ogg.txt").read_text().strip() == "remind me at 9"
    f.store.close()


def test_failed_transcription_keeps_the_file(cfg, monkeypatch):
    f, dest, rel = make_bot(cfg, monkeypatch, TranscriptionError("groq returned HTTP 401"))
    m = msg()
    asyncio.run(f._on_recording(m, dest, rel))
    assert not f.asked and "HTTP 401" in m.replies[0]
    assert f._take_pending_files() == [rel]
    f.store.close()
