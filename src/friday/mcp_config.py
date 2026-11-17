"""Extra MCP servers from ~/friday-data/mcp.toml, turned into Codex config overrides.

The file is read on every Codex run, so editing it needs no restart. It lives outside the
workspace, so Friday itself can't change it.

    [servers.time]
    command = "uvx"
    args = ["mcp-server-time"]

    [servers.notion]
    url = "https://mcp.notion.com/mcp"
    bearer_token_env_var = "NOTION_TOKEN"   # the token goes in .env.server

Friday-specific keys (everything else is passed to Codex unchanged):
    enabled  = false            skip this server
    approve  = false            make Codex ask before each tool call; since nobody can answer,
                                this effectively blocks the server's tools (default: true)
    env_from = ["NOTION_KEY"]   copy these variables from Friday's environment (.env.server)
                                into a stdio server's env, so secrets stay out of mcp.toml
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

RESERVED = {"friday", "github", "playwright"}
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_OWN_KEYS = {"enabled", "approve", "env_from"}


@dataclass
class ExtraServers:
    values: dict[str, object] = field(default_factory=dict)  # dotted config key -> value
    names: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def overrides(self) -> list[str]:
        """As `codex -c` arguments."""
        return [f"{k}={toml_value(v)}" for k, v in self.values.items()]


def toml_value(v) -> str:
    """Serialize a TOML value inline (enough for MCP server settings)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {toml_value(x)}" for k, x in v.items()) + "}"
    raise ValueError(f"unsupported value {v!r}")


def load(path: Path) -> ExtraServers:
    out = ExtraServers()
    if not path.exists():
        return out
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        out.problems.append(f"{path.name}: {e}")
        return out

    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        out.problems.append(f"{path.name}: [servers] must be a table")
        return out

    for name, spec in servers.items():
        if not isinstance(spec, dict):
            out.problems.append(f"{name}: not a table")
            continue
        if not _NAME.match(name) or name in RESERVED:
            out.problems.append(f"{name}: invalid or reserved name")
            continue
        if not spec.get("enabled", True):
            continue
        if not spec.get("command") and not spec.get("url"):
            out.problems.append(f"{name}: needs `command` or `url`")
            continue

        settings = {k: v for k, v in spec.items() if k not in _OWN_KEYS}
        env = dict(settings.get("env") or {})
        for var in spec.get("env_from", []):
            if var in os.environ:
                env[var] = os.environ[var]
            else:
                out.problems.append(f"{name}: {var} is not set in .env.server")
        if env:
            settings["env"] = env
        if spec.get("approve", True):
            settings["default_tools_approval_mode"] = "approve"

        try:
            for v in settings.values():
                toml_value(v)  # fails on values TOML can't hold
        except ValueError as e:
            out.problems.append(f"{name}: {e}")
            continue
        out.values.update({f"mcp_servers.{name}.{k}": v for k, v in settings.items()})
        out.names.append(name)
    return out
