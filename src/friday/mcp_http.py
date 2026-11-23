"""Friday's MCP servers as long-lived local HTTP services, so a chat turn doesn't start them.

deploy/friday-mcp.service runs Friday's own tools (mcp_server.py) and
deploy/friday-playwright-mcp.service the Playwright MCP server, both on 127.0.0.1 only. Codex
connects to them by URL. Friday's server requires a bearer token: random, created on first use,
kept 0600 in the data dir (hidden from the agents' shells, see browser.private_paths). Codex reads
it from TOKEN_VAR, which only the codex process gets.

Per-run context travels as HTTP headers Codex adds to every request (agent.codex_args):
RUN_HEADER for a scheduled browser run, CHAT_HEADER for a Telegram chat turn.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import socket
from pathlib import Path

from .config import Config

TOKEN_VAR = "FRIDAY_MCP_TOKEN"  # matches the "*TOKEN*" shell_environment_policy exclusion
RUN_HEADER = "X-Friday-Run"
WATCH_HEADER = "X-Friday-Watch"  # a watch check (watches.py)
CHAT_HEADER = "X-Friday-Chat"  # a Telegram chat turn; the value is its lane (bot.Friday lanes)
NO_BROWSER_HEADER = "X-Friday-No-Browser"  # a chat turn started while another one had the browser
JOB_HEADER = "X-Friday-Job"  # a delegated job (delegate.py): only its tools work


def toolsets_key(lane: str) -> str:
    """The kv key for the toolsets a chat lane turned on (mcp_server.use_tools); lane "1" is the
    main conversation."""
    return "toolsets" if lane in ("", "1") else f"toolsets:{lane}"


def friday_port() -> int:
    return int(os.environ.get("FRIDAY_MCP_PORT") or 8765)


def playwright_port() -> int:
    return int(os.environ.get("FRIDAY_PLAYWRIGHT_MCP_PORT") or 8931)


def url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"


def listening(port: int, timeout: float = 0.2) -> bool:
    """Is something accepting connections on 127.0.0.1:port? (Takes well under a millisecond.)"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def token_path(cfg: Config) -> Path:
    return cfg.data_dir / "mcp_token"


def token(cfg: Config) -> str:
    """The bearer token for Friday's MCP server, created (0600) if there is none yet."""
    path = token_path(cfg)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_urlsafe(32))
    return path.read_text().strip()


class BearerAuth:
    """ASGI wrapper: HTTP requests without `Authorization: Bearer <token>` get 401. job_for_token
    (a token's SHA-256 -> running job id) also admits a project job's own token: that request
    counts as that job whatever headers it sends, so it only reaches the job's tools."""

    def __init__(self, app, token: str, job_for_token=None):
        if not token:
            raise ValueError("empty MCP token")
        self.app, self.expected, self.job_for_token = app, f"Bearer {token}".encode(), job_for_token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = next((v for k, v in scope.get("headers", []) if k == b"authorization"), b"")
            if not hmac.compare_digest(got, self.expected):
                jid = None
                if self.job_for_token and got.startswith(b"Bearer "):
                    jid = self.job_for_token(hashlib.sha256(got[7:]).hexdigest())
                if not jid:
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"text/plain")]})
                    await send({"type": "http.response.body", "body": b"unauthorized"})
                    return
                headers = [(k, v) for k, v in scope["headers"] if not k.startswith(b"x-friday-")]
                scope = {**scope, "headers": [*headers, (JOB_HEADER.lower().encode(), str(jid).encode())]}
        await self.app(scope, receive, send)
