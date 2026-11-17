from dataclasses import replace

import pytest

from friday.config import load_config
from friday.db import Store


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A clean environment for load_config(): no .env file, data under tmp_path."""
    monkeypatch.setenv("FRIDAY_ENV", str(tmp_path / "none.env"))
    monkeypatch.setenv("FRIDAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FRIDAY_TZ", "Asia/Seoul")
    monkeypatch.setenv("FRIDAY_CODEX_SERVER", "false")  # tests that want it use a fake (test_codex_server.py)
    for k in ("GITHUB_TOKEN", "GITHUB_CODE_TOKEN", "TRANSCRIBE_API_KEY", "TRANSCRIBE_PROVIDER",
              "CODEX_TIMEOUT_SECONDS", "CODEX_TIMEOUT_S", "CODE_JOB_TIMEOUT_SECONDS", "CODE_JOB_TIMEOUT_S"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


@pytest.fixture
def cfg(env, tmp_path):
    return replace(load_config(), code_dir=tmp_path / "code")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def tools(cfg, store, monkeypatch):
    """The MCP tool module, using this test's config and database."""
    import friday.mcp_server as m  # opens a database at import time, under the tmp data dir

    monkeypatch.setattr(m, "cfg", cfg)
    monkeypatch.setattr(m, "store", store)
    return m
