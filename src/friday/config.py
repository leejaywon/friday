"""Settings, read from a .env file and the process environment."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def env_file() -> Path:
    """The .env file in use. FRIDAY_ENV overrides the project-root default."""
    return Path(os.environ.get("FRIDAY_ENV") or PROJECT_ROOT / ".env").resolve()


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: tuple[int, ...]
    data_dir: Path
    default_tz: str
    codex_bin: str
    codex_model: str
    codex_effort: str
    codex_timeout_seconds: int
    web_search: str  # live | cached | disabled
    github_token_set: bool
    github_readonly: bool
    meeting_lead_min: int
    morning_brief_at: str  # "HH:MM" in the user's timezone, or "" to turn it off
    # The agent behind chat, browser runs and watch checks: codex (a ChatGPT plan) or claude (a
    # Claude plan, through Claude Code). Env: CHAT_ENGINE, CLAUDE_MODEL (claude's default model)
    chat_engine: str = "codex"
    claude_model: str = ""
    # Coding jobs (/code): agents change projects under code_dir.
    code_dir: Path = Path.home() / "code"
    code_engine: str = "claude"  # claude | codex
    claude_bin: str = "claude"
    code_job_timeout_seconds: int = 3600
    # Write token for pushing branches and opening PRs. Kept out of os.environ (see load_config)
    # so neither the chat agent nor the coding agents can see it.
    github_code_token: str = field(default="", repr=False)
    # Speech to text for voice messages (see transcribe.py). Empty provider = off.
    transcribe_provider: str = ""  # groq | openai
    transcribe_model: str = ""  # empty = the provider's default
    transcribe_language: str = ""  # ISO-639-1 like "ko"; empty = detect
    # Kept out of os.environ like github_code_token, so no agent sees it (and Codex never mistakes
    # it for its own OPENAI_API_KEY).
    transcribe_api_key: str = field(default="", repr=False)
    # How long a file sent without a caption waits for a message about it before it is answered
    # on its own. Env: FILE_FOLLOWUP_WAIT_SECONDS
    file_followup_wait_seconds: float = 4.0
    # How long a message waits for a file sent just before it that is still downloading, so the
    # two are answered together. Env: FILE_DOWNLOAD_WAIT_SECONDS
    file_download_wait_seconds: float = 10.0
    # Speech-to-text request timeout. Env: TRANSCRIBE_TIMEOUT_SECONDS
    transcribe_timeout_seconds: float = 120.0
    # The shared browser (deploy/friday-browser.service) for the chat agent. Env: FRIDAY_BROWSER
    browser_enabled: bool = True
    # Chat turns in one long-lived `codex app-server` (codex_server.py) rather than a `codex exec`
    # per message; exec stays the fallback. Env: FRIDAY_CODEX_SERVER
    codex_server: bool = True
    # From my.telegram.org: set = the bot talks to the local Bot API server (botapi.py,
    # deploy/friday-botapi.service), for files up to 2 GB. Env: TELEGRAM_API_ID, TELEGRAM_API_HASH
    telegram_api_id: str = ""
    telegram_api_hash: str = field(default="", repr=False)  # kept out of os.environ, like the keys above

    @property
    def default_model(self) -> str:
        return self.claude_model if self.chat_engine == "claude" else self.codex_model

    @property
    def default_effort(self) -> str:
        return "" if self.chat_engine == "claude" else self.codex_effort

    @property
    def local_botapi(self) -> bool:
        return bool(self.telegram_api_id)

    @property
    def botapi_dir(self) -> Path:
        """The local Bot API server's files and session. Outside the workspace, hidden from the agents."""
        return self.data_dir / "botapi"

    @property
    def transcription_on(self) -> bool:
        return bool(self.transcribe_provider and self.transcribe_api_key)

    @property
    def owner_chat_id(self) -> int | None:
        # In a private Telegram chat, the chat id equals the user id.
        return self.allowed_user_ids[0] if self.allowed_user_ids else None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "friday.db"

    @property
    def workspace(self) -> Path:
        """Codex's working directory. Everything the agent may write lives here."""
        return self.data_dir / "workspace"

    @property
    def files_dir(self) -> Path:
        return self.workspace / "files"

    @property
    def instructions_path(self) -> Path:
        return self.workspace / "FRIDAY.md"

    @property
    def mcp_config(self) -> Path:
        """Extra MCP servers. Outside the workspace, so the agent can't edit it."""
        return self.data_dir / "mcp.toml"

    @property
    def vault_dir(self) -> Path:
        """Encrypted website logins (vault.py). Outside the workspace, hidden from the agents."""
        return self.data_dir / "vault"

    @property
    def claude_mcp_dir(self) -> Path:
        """Per-run MCP configs for Claude (agent.run_claude): they hold Friday's MCP token."""
        return self.data_dir / "claude-mcp"

    @property
    def browser_profile(self) -> Path:
        """The shared browser's profile (cookies keep its logins); see deploy/friday-browser.service."""
        return self.data_dir / "browser"

    @property
    def google_client_secret(self) -> Path:
        return self.data_dir / "google_client_secret.json"

    @property
    def google_token(self) -> Path:
        return self.data_dir / "google_token.json"


# Renamed settings (new name: old name). The old name still works, with a warning in the log, so
# an older .env keeps its value.
RENAMED_SETTINGS = {
    "CODEX_TIMEOUT_SECONDS": "CODEX_TIMEOUT_S",
    "CODE_JOB_TIMEOUT_SECONDS": "CODE_JOB_TIMEOUT_S",
}


def _setting(name: str, default: str) -> str:
    """The value of setting `name`, falling back to its old name (see RENAMED_SETTINGS)."""
    if name in os.environ:
        return os.environ[name]
    old = RENAMED_SETTINGS.get(name)
    if old and old in os.environ:
        logging.getLogger(__name__).warning("%s is the old name of %s; please rename it in .env", old, name)
        return os.environ[old]
    return default


def _int_list(raw: str) -> tuple[int, ...]:
    return tuple(int(p) for p in raw.replace(" ", "").split(",") if p)


def load_config() -> Config:
    load_dotenv(env_file(), override=False)
    tz = (os.environ.get("FRIDAY_TZ") or "UTC").strip()
    chat_engine = (os.environ.get("CHAT_ENGINE") or "codex").strip().lower()
    if chat_engine not in ("codex", "claude"):
        raise ValueError(f"CHAT_ENGINE must be codex or claude, not {chat_engine!r}")
    ZoneInfo(tz)  # fail fast on a typo
    data_dir = Path(os.environ.get("FRIDAY_DATA_DIR") or PROJECT_ROOT / "data").expanduser().resolve()
    transcribe_key = os.environ.pop("TRANSCRIBE_API_KEY", "").strip()
    transcribe_provider = os.environ.get("TRANSCRIBE_PROVIDER", "").strip().lower()
    if transcribe_key and not transcribe_provider:
        transcribe_provider = "groq"
    return Config(
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        allowed_user_ids=_int_list(os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")),
        data_dir=data_dir,
        default_tz=tz,
        chat_engine=chat_engine,
        claude_model=os.environ.get("CLAUDE_MODEL", "").strip(),
        codex_bin=os.environ.get("CODEX_BIN", "codex"),
        codex_model=os.environ.get("CODEX_MODEL", "").strip(),
        codex_effort=os.environ.get("CODEX_EFFORT", "").strip(),
        codex_timeout_seconds=int(_setting("CODEX_TIMEOUT_SECONDS", "600")),
        web_search=os.environ.get("FRIDAY_WEB_SEARCH", "live").strip() or "disabled",
        # The token itself stays in the environment; Codex reads it from GITHUB_TOKEN.
        github_token_set=bool(os.environ.get("GITHUB_TOKEN", "").strip()),
        github_readonly=os.environ.get("GITHUB_READONLY", "true").strip().lower() not in ("0", "false", "no"),
        meeting_lead_min=int(os.environ.get("MEETING_LEAD_MIN", "10")),
        morning_brief_at=os.environ.get("MORNING_BRIEF_AT", "08:00").strip(),
        code_dir=Path(os.environ.get("CODE_DIR") or Path.home() / "code").expanduser(),
        code_engine=(os.environ.get("CODE_ENGINE", "claude").strip().lower() or "claude"),
        claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
        code_job_timeout_seconds=int(_setting("CODE_JOB_TIMEOUT_SECONDS", "3600")),
        github_code_token=os.environ.pop("GITHUB_CODE_TOKEN", "").strip(),
        transcribe_provider=transcribe_provider,
        transcribe_model=os.environ.get("TRANSCRIBE_MODEL", "").strip(),
        transcribe_language=os.environ.get("TRANSCRIBE_LANGUAGE", "").strip().lower(),
        transcribe_api_key=transcribe_key,
        file_followup_wait_seconds=float(os.environ.get("FILE_FOLLOWUP_WAIT_SECONDS", "4")),
        file_download_wait_seconds=float(os.environ.get("FILE_DOWNLOAD_WAIT_SECONDS", "10")),
        transcribe_timeout_seconds=float(os.environ.get("TRANSCRIBE_TIMEOUT_SECONDS", "120")),
        browser_enabled=os.environ.get("FRIDAY_BROWSER", "true").strip().lower() not in ("0", "false", "no", "off"),
        codex_server=os.environ.get("FRIDAY_CODEX_SERVER", "true").strip().lower() not in ("0", "false", "no", "off"),
        telegram_api_id=os.environ.get("TELEGRAM_API_ID", "").strip(),
        telegram_api_hash=os.environ.pop("TELEGRAM_API_HASH", "").strip(),
    )


def ensure_dirs(cfg: Config) -> None:
    for d in (cfg.data_dir, cfg.workspace, cfg.files_dir / "inbox"):
        d.mkdir(parents=True, exist_ok=True)
    cfg.vault_dir.mkdir(mode=0o700, exist_ok=True)  # exists early, so the sandboxes can hide it
    if cfg.local_botapi:  # likewise: it holds the bot's Telegram session
        cfg.botapi_dir.mkdir(mode=0o700, exist_ok=True)
