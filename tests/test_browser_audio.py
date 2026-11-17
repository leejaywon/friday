import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from friday import browser_audio as audio
from friday.transcribe import TranscriptionError


@pytest.fixture
def recorder(cfg, monkeypatch):
    cfg.workspace.mkdir(parents=True)
    monkeypatch.setattr(audio.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(audio.shutil, "disk_usage", lambda _: SimpleNamespace(free=10**9))
    monkeypatch.setattr(audio, "_running", lambda record: True)
    monkeypatch.setattr(audio, "_run", lambda *a, **k: "1\tfriday_browser.monitor\n")
    return cfg


def saved_recording(cfg, count=2):
    record = dict(id="a"*32, title="Lecture", status="saved", parts=[f"part-{i:04d}.m4a" for i in range(count)],
                  durations=[300.0]*count, part_seconds=300)
    directory = audio._directory(cfg, record)
    directory.mkdir(parents=True)
    for name in record["parts"]:
        (directory/name).write_bytes(b"test audio")
    audio._save(cfg, record)
    return record, directory


def test_recording_state_survives_calls_and_prevents_overlapping_capture(recorder):
    record = audio.start(recorder, "Lecture", 2)
    assert record["status"] == "recording"
    assert audio.status(recorder)["id"] == record["id"]
    with pytest.raises(audio.AudioError, match="Already recording"):
        audio.start(recorder)
    assert audio.list_recordings(recorder)[0]["id"] == record["id"]


def test_control_lock_refuses_concurrent_start(recorder):
    with audio._lock(recorder):
        with pytest.raises(audio.AudioError, match="busy"):
            audio.start(recorder)


def test_recorder_is_bounded_and_uses_only_private_monitor(recorder, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return "1\tfriday_browser.monitor\n"
    monkeypatch.setattr(audio, "_run", run)
    record = audio.start(recorder, "spaces; $() never execute", 2)
    command = calls[-1]
    assert "friday_browser.monitor" in command
    assert "--property=RuntimeMaxSec=150" in command
    assert "--property=KillSignal=SIGINT" in command
    assert not any(record["title"] in arg for arg in command)
    for invalid in (0, 121):
        with pytest.raises(audio.AudioError, match="between"):
            audio.start(recorder, max_minutes=invalid)


def test_automatic_finish_and_explicit_stop_are_idempotent(recorder, monkeypatch):
    record = audio.start(recorder)
    directory = audio._directory(recorder, record)
    (directory/"part-0000.m4a").write_bytes(b"test audio")
    monkeypatch.setattr(audio, "_running", lambda _: False)
    monkeypatch.setattr(audio, "_probe", lambda _: 12.5)
    finalized = []
    def run(args, **kwargs):
        finalized.append(args)
        (directory/"recording.m4a").write_bytes(b"combined audio")
        return ""
    monkeypatch.setattr(audio, "_run", run)
    done = audio.status(recorder)
    assert done["status"] == "saved" and done["duration_seconds"] == 12.5
    assert audio.stop(recorder)["path"] == done["path"]
    assert len(finalized) == 1


def test_start_failure_leaves_no_active_recording(recorder, monkeypatch):
    def run(args, **kwargs):
        if args[0] == "systemd-run":
            raise audio.AudioError("start failed")
        return "friday_browser.monitor"
    monkeypatch.setattr(audio, "_run", run)
    with pytest.raises(audio.AudioError, match="start failed"):
        audio.start(recorder)
    assert audio.list_recordings(recorder)[0]["status"] == "failed"


def test_unreadable_capture_does_not_leave_recording_stuck(recorder, monkeypatch):
    record = audio.start(recorder)
    (audio._directory(recorder, record)/"part-0000.m4a").write_bytes(b"incomplete MP4")
    monkeypatch.setattr(audio, "_running", lambda _: False)
    def broken(_):
        raise audio.AudioError("unreadable")
    monkeypatch.setattr(audio, "_probe", broken)
    result = audio.status(recorder)
    assert result["status"] == "failed" and "incomplete" in result["warning"]


def test_no_captured_audio_is_reported_as_failure(recorder, monkeypatch):
    audio.start(recorder)
    monkeypatch.setattr(audio, "_running", lambda _: False)
    assert audio.status(recorder)["status"] == "failed"


def test_recording_ids_and_symlinks_cannot_read_other_files(cfg, tmp_path):
    with pytest.raises(audio.AudioError, match="Invalid recording"):
        audio.status(cfg, "../../vault/key")
    record, directory = saved_recording(cfg)
    outside = tmp_path/"private.txt"
    outside.write_text("private")
    (directory/"transcript.txt").symlink_to(outside)
    with pytest.raises(audio.AudioError, match="symlinks"):
        audio.read_transcript(cfg, record["id"])
    (directory/"part-0000.m4a").unlink()
    (directory/"part-0000.m4a").symlink_to(outside)
    cfg = replace(cfg, transcribe_provider="groq", transcribe_api_key="test")
    with pytest.raises(audio.AudioError, match="symlinks"):
        asyncio.run(audio.transcribe_recording(cfg, record["id"]))


def test_transcription_resumes_after_failure_without_uploading_completed_parts(cfg, monkeypatch):
    cfg = replace(cfg, transcribe_provider="groq", transcribe_api_key="not-exposed")
    record, directory = saved_recording(cfg, 3)
    monkeypatch.setattr(audio, "_peak", lambda _: -10)
    calls, fail = [], [True]
    async def transcribe(path, **kwargs):
        calls.append(path.name)
        if path.name == "part-0001.m4a" and fail[0]:
            raise TranscriptionError("temporary outage")
        return f"Lecture text for {path.name}"
    monkeypatch.setattr(audio, "transcribe", transcribe)
    with pytest.raises(TranscriptionError):
        asyncio.run(audio.transcribe_recording(cfg, record["id"]))
    assert (directory/"part-0000.txt").exists()
    assert "Lecture text" in (directory/"transcript.txt").read_text()
    fail[0] = False
    result = asyncio.run(audio.transcribe_recording(cfg, record["id"], max_parts=1))
    assert result["completed_parts"] == 2 and result["remaining_parts"] == 1
    result = asyncio.run(audio.transcribe_recording(cfg, record["id"]))
    assert result["remaining_parts"] == 0
    assert calls.count("part-0000.m4a") == 1
    assert "[05:00]" in result["text"] and "[10:00]" in result["text"]
    page = audio.read_transcript(cfg, record["id"], offset=5, limit=12)
    assert len(page["text"]) == 12 and page["next_offset"] == 17


def test_silence_does_not_get_uploaded_or_hallucinated(cfg, monkeypatch):
    cfg = replace(cfg, transcribe_provider="groq", transcribe_api_key="test")
    record, _ = saved_recording(cfg, 1)
    monkeypatch.setattr(audio, "_peak", lambda _: -90)
    async def never(*args, **kwargs):
        raise AssertionError("silent audio should not be uploaded")
    monkeypatch.setattr(audio, "transcribe", never)
    result = asyncio.run(audio.transcribe_recording(cfg, record["id"]))
    assert "Silence" in result["text"] and result["remaining_parts"] == 0


def test_transcription_refuses_a_running_recording(recorder):
    recorder = replace(recorder, transcribe_provider="groq", transcribe_api_key="test")
    record = audio.start(recorder)
    with pytest.raises(audio.AudioError, match="Stop the recording"):
        asyncio.run(audio.transcribe_recording(recorder, record["id"]))


def test_mcp_audio_tools_and_watch_permissions(tools, monkeypatch, cfg):
    from friday import agent
    monkeypatch.setattr(audio, "start", lambda *a: {"id": "test", "status": "recording"})
    assert '"status": "recording"' in asyncio.run(tools.start_browser_recording())
    monkeypatch.setattr(audio, "stop", lambda *a: (_ for _ in ()).throw(audio.AudioError("not found")))
    assert "not found" in asyncio.run(tools.stop_browser_recording())
    overrides = agent.codex_overrides(cfg, toolsets=set())
    assert "start_browser_recording" in str(overrides)
    assert "start_browser_recording" in agent.WATCH_HIDDEN_TOOLS
    assert "transcribe_browser_recording" in agent.WATCH_HIDDEN_TOOLS
