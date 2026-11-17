"""Website logins and other secrets, encrypted at rest.

Entries are added from the server's terminal (`friday vault add`) or through a form on Telegram
(request_form / request_login: its sensitive fields, forms.py, or a reply the bot deletes),
never by the agent.
Only the MCP server's fill_login and fill_login_field read the values, and they type them straight
into the browser: no agent ever sees them. A login is "trusted" on a domain once the user approved
a use there (or typed it in for that site); it is then filled there without asking, until /logins
revokes it.

The key and the encrypted file sit in their own 0700 folder, which the chat agent's shell, the
Playwright MCP server and coding jobs can't read (see browser.private_paths and jobs.agent_secrets).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet

NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


@dataclass(frozen=True)
class Login:
    name: str
    username: str
    password: str = field(repr=False)
    domains: tuple[str, ...]
    trusted: tuple[str, ...] = ()  # domains where it is filled without asking
    # Other sensitive fields from a form (request_form / request_login fields=...), e.g. a member
    # ID, a PIN or an API key: name -> value. Typed by fill_login_field, never shown.
    extra: dict[str, str] = field(default_factory=dict, repr=False)


def domain_allowed(host: str, domains: tuple[str, ...] | list[str]) -> bool:
    """The host is one of the domains or a subdomain of one (never a look-alike such as
    evil-portal.example.com or portal.example.com.evil.com)."""
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def check(name: str, domains: list[str]) -> list[str]:
    """Validate a login's name and domains (before asking for the password). Returns the domains cleaned."""
    domains = [d.strip().lower().rstrip(".") for d in domains if d.strip()]
    if not NAME.match(name):
        raise ValueError("name: use letters, digits, '.', '_' or '-' (max 40)")
    if not domains or not all(DOMAIN.match(d) for d in domains):
        raise ValueError("give at least one domain like portal.example.com (no https://, no path)")
    return domains


class Vault:
    def __init__(self, folder: Path):
        self.folder = folder
        self.key_path = folder / "key"
        self.path = folder / "logins"

    def _fernet(self) -> Fernet:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.key_path.exists():
            # O_EXCL: never overwrite a key (that would make the saved logins unreadable).
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(Fernet.generate_key())
        return Fernet(self.key_path.read_bytes())

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self._fernet().decrypt(self.path.read_bytes()))

    def _save(self, data: dict[str, dict]) -> None:
        token = self._fernet().encrypt(json.dumps(data).encode())
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(token)
        tmp.replace(self.path)

    def add(self, name: str, username: str, password: str, domains: list[str], trusted: bool = False,
            extra: dict[str, str] | None = None) -> None:
        """Add or replace a login. Replacing it resets its trust; trusted=True trusts all its domains."""
        domains = check(name, domains)
        if not username or not password:
            raise ValueError("username and password can't be empty")
        data = self._load()
        data[name] = {"username": username, "password": password, "domains": domains,
                      "trusted": domains if trusted else [], "extra": dict(extra or {})}
        self._save(data)

    def save(self, name: str, values: dict[str, str], domains: list[str], trusted: bool = False) -> None:
        """Store a form's sensitive values under name, replacing any entry of that name (never
        merged: a new form must not rebind an old password to another site). 'username' and
        'password' make it a website login; the rest are extra fields. No domains = a secret that
        isn't bound to a site, which the browser tools refuse to type."""
        if not NAME.match(name):
            raise ValueError("name: use letters, digits, '.', '_' or '-' (max 40)")
        domains = check(name, domains) if domains else []
        data = self._load()
        data[name] = {"username": values.get("username", ""), "password": values.get("password", ""),
                      "domains": domains, "trusted": domains if trusted else [],
                      "extra": {k: v for k, v in values.items() if k not in ("username", "password")}}
        self._save(data)

    def trust(self, name: str, host: str) -> bool:
        """Fill this login on host's domain without asking from now on (the user approved it there)."""
        data = self._load()
        e = data.get(name)
        match = [d for d in (e or {}).get("domains", []) if domain_allowed(host, [d])]
        if not match:
            return False
        e["trusted"] = sorted(set(e.get("trusted", [])) | set(match))
        self._save(data)
        return True

    def untrust(self, name: str) -> bool:
        """Ask again before the next use on every site."""
        data = self._load()
        if name not in data:
            return False
        data[name]["trusted"] = []
        self._save(data)
        return True

    def remove(self, name: str) -> bool:
        data = self._load()
        if data.pop(name, None) is None:
            return False
        self._save(data)
        return True

    def names(self) -> list[tuple[str, tuple[str, ...]]]:
        """Names and domains only: what the agent and `friday vault list` may see."""
        return [(n, tuple(e["domains"])) for n, e in sorted(self._load().items())]

    def entries(self) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
        """(name, domains, trusted domains), never usernames or passwords."""
        return [(n, tuple(e["domains"]), tuple(e.get("trusted", []))) for n, e in sorted(self._load().items())]

    def field_names(self) -> dict[str, list[str]]:
        """The extra fields each login has (names only, never values)."""
        return {n: sorted(e.get("extra", {})) for n, e in self._load().items()}

    def names_for(self, host: str) -> list[str]:
        """Logins allowed on this host, e.g. to pre-approve them for a scheduled run on that site."""
        return [n for n, domains in self.names() if domain_allowed(host, domains)]

    def get(self, name: str) -> Login | None:
        e = self._load().get(name)
        if not e:
            return None
        return Login(name, e["username"], e["password"], tuple(e["domains"]), tuple(e.get("trusted", [])),
                     dict(e.get("extra", {})))
