"""Files sent without a message are held briefly, then answered; images reach Codex as images."""

import asyncio
from dataclasses import replace

import pytest

from friday import bot as botmod
from friday.agent import AgentResult


@pytest.fixture
def friday(cfg, monkeypatch):
    f = botmod.Friday(replace(cfg, file_followup_wait_seconds=0.05))
    f.runs = []

    async def run_codex(cfg, prompt, thread, images, model="", effort="", **kw):
        f.runs.append((prompt, list(images or [])))
        return AgentResult("ok", "thread-1", [])

    async def nothing(*a, **k):
        pass

    monkeypatch.setattr(botmod, "run_turn", run_codex)
    f.say, f._keep_typing, f.outbox_loop = nothing, nothing, nothing
    yield f
    f.store.close()


def put(f, name):
    path = f.cfg.files_dir / "inbox" / name
    path.write_bytes(b"x")
    return str(path.relative_to(f.cfg.workspace))


def test_image_without_caption_gets_one_answer(friday):
    a, b = put(friday, "photo_1.jpg"), put(friday, "b.pdf")

    async def go():
        friday._queue_files(1, [a])
        friday._queue_files(1, [b])
        assert not friday.runs  # not yet: a message may follow
        await asyncio.sleep(0.2)

    asyncio.run(go())
    [(prompt, images)] = friday.runs
    assert botmod.IMAGES_ONLY in prompt and a in prompt and b in prompt
    assert images == [friday.cfg.workspace / a]  # the PDF is a path, not an image


def test_message_typed_during_the_wait_takes_the_files(friday):
    rel = put(friday, "photo_1.jpg")

    async def go():
        friday._queue_files(1, [rel])
        await friday.ask(1, "what do you think about that?")
        await asyncio.sleep(0.2)

    asyncio.run(go())
    [(prompt, images)] = friday.runs  # one reply, not two
    assert "what do you think about that?" in prompt and rel in prompt
    assert images == [friday.cfg.workspace / rel]
