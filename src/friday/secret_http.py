"""HTTP requests that carry a vault secret in a header (mcp_server.http_request).

The agent names the secret ('example_api' or 'example_api.api_key') and writes a header template
such as 'Authorization: Bearer {secret}'; the value is read from the vault here, at execution
time, and never passes through the agent: not in the tool's arguments, its result, its errors or
the log. Anything the server echoes back is redacted too.

A secret only goes to its own hosts: the domains its vault entry is bound to (the domain of the
request_form that saved it), over https. A URL anywhere else is refused before the secret is
read into a header. Redirects are never followed,
so the header can't be carried to another host either.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

from .vault import Login, domain_allowed

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
READ_ONLY = ("GET", "HEAD")
PLACEHOLDER = "{secret}"
TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 1_000_000  # read at most this much of a response
MAX_BODY_CHARS = 20_000  # and return at most this much of it to the agent
MAX_REQUEST_BODY = 200_000
REDACTED = "[redacted]"
# Response headers worth returning (all others are dropped: cookies, for one, are never shown).
SHOWN_HEADERS = ("content-type", "content-length", "location", "retry-after", "www-authenticate",
                 "x-request-id", "request-id", "x-ratelimit-limit", "x-ratelimit-remaining",
                 "x-ratelimit-reset", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset")
# Request headers the agent may not set: they would change where or how the request goes.
FORBIDDEN_HEADERS = ("host", "content-length", "transfer-encoding", "connection", "cookie", "proxy-authorization")
HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")


class Refused(ValueError):
    """A request that was not sent; the message is safe to return (never holds the secret)."""


@dataclass(frozen=True)
class Prepared:
    method: str
    url: str
    host: str
    header_name: str
    header_value_template: str  # holds {secret}
    headers: dict[str, str]
    body: bytes | None


def split_ref(ref: str) -> tuple[str, str]:
    """'name' or 'name.field' -> (name, field). Vault names may hold dots, so the caller tries
    the whole string as a name first (see pick_secret)."""
    ref = (ref or "").strip()
    name, _, field = ref.rpartition(".")
    return (name, field) if name else (ref, "")


def pick_secret(login: Login, field: str) -> str:
    """The value to send: the named field, else the password, else the entry's only field."""
    if field:
        if field == "password" and login.password:
            return login.password
        if field in login.extra:
            return login.extra[field]
        raise Refused(f"'{login.name}' has no field '{field}' (its fields: {_fields(login)}).")
    if login.password:
        return login.password
    if len(login.extra) == 1:
        return next(iter(login.extra.values()))
    raise Refused(f"'{login.name}' has several fields; name one as {login.name}.<field> "
                  f"(its fields: {_fields(login)}).")


def _fields(login: Login) -> str:
    return ", ".join((["password"] if login.password else []) + sorted(login.extra)) or "none"


def _parse_headers(raw: str) -> dict[str, str]:
    """Extra headers: a JSON object or 'Name: value' lines."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise Refused(f"headers: not valid JSON ({e.msg})") from None
        if not isinstance(obj, dict):
            raise Refused("headers: give a JSON object or 'Name: value' lines")
        items = [(str(k), str(v)) for k, v in obj.items()]
    else:
        items = []
        for line in raw.splitlines():
            if line.strip():
                name, sep, value = line.partition(":")
                if not sep:
                    raise Refused(f"headers: '{line.strip()[:40]}' isn't 'Name: value'")
                items.append((name.strip(), value.strip()))
    out: dict[str, str] = {}
    for name, value in items:
        if not HEADER_NAME.match(name) or "\r" in value or "\n" in value:
            raise Refused(f"headers: bad header '{name[:40]}'")
        if name.lower() in FORBIDDEN_HEADERS:
            raise Refused(f"headers: '{name}' can't be set")
        if PLACEHOLDER in value:
            raise Refused(f"headers: {PLACEHOLDER} only goes in the header template")
        out[name] = value
    return out


def prepare(method: str, url: str, header: str, headers: str = "", body: str = "",
            content_type: str = "", allow_http_loopback: bool = False) -> Prepared:
    """Check everything the agent gave, before the vault is read."""
    method = (method or "").strip().upper()
    if method not in METHODS:
        raise Refused(f"method: one of {', '.join(METHODS)}")
    if PLACEHOLDER in url:
        raise Refused(f"url: the secret never goes in a URL; put {PLACEHOLDER} in the header template")
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower().rstrip(".")
        _ = parts.port  # raises on a bad port
    except ValueError:
        raise Refused("url: not a valid URL") from None
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not (parts.scheme == "https" or (allow_http_loopback and loopback and parts.scheme == "http")):
        raise Refused("url: only https:// URLs")
    if not host or parts.username or parts.password:
        raise Refused("url: give a plain https://host/path URL (no user:password@)")
    name, sep, template = (header or "").partition(":")
    name, template = name.strip(), template.strip()
    if not sep or not HEADER_NAME.match(name) or "\r" in template or "\n" in template:
        raise Refused(f"header: write it as 'Name: value with {PLACEHOLDER}', "
                      f"e.g. 'Authorization: Bearer {PLACEHOLDER}'")
    if name.lower() in FORBIDDEN_HEADERS:
        raise Refused(f"header: '{name}' can't carry the secret")
    if template.count(PLACEHOLDER) != 1:
        raise Refused(f"header: the value must hold {PLACEHOLDER} exactly once")
    extra = _parse_headers(headers)
    if any(k.lower() == name.lower() for k in extra):
        raise Refused(f"headers: '{name}' is already the secret's header")
    if PLACEHOLDER in (body or ""):
        raise Refused("body: the secret never goes in the body; only in the header template")
    data = None
    if body:
        if method in READ_ONLY:
            raise Refused(f"body: a {method} request has no body")
        if len(body) > MAX_REQUEST_BODY:
            raise Refused(f"body: at most {MAX_REQUEST_BODY} characters")
        data = body.encode()
        if not any(k.lower() == "content-type" for k in extra):
            if content_type:
                extra["Content-Type"] = content_type.strip()
            else:
                try:
                    json.loads(body)
                    extra["Content-Type"] = "application/json"
                except json.JSONDecodeError:
                    extra["Content-Type"] = "text/plain; charset=utf-8"
    return Prepared(method, parts.geturl(), host, name, template, extra, data)


def check_host(req: Prepared, login: Login) -> None:
    if not login.domains:
        raise Refused(f"'{login.name}' isn't bound to a site, so it can't be sent anywhere. Send "
                      f"request_form again with save_as '{login.name}' and the API's domain.")
    if not domain_allowed(req.host, login.domains):
        raise Refused(f"Wrong host: '{login.name}' may only be sent to {', '.join(login.domains)}, "
                      f"not {req.host}.")


class Redactor:
    """Removes a secret, and the usual encodings of it, from text."""

    def __init__(self, secret: str):
        forms = {secret, quote(secret, safe=""), base64.b64encode(secret.encode()).decode(),
                 base64.urlsafe_b64encode(secret.encode()).decode().rstrip("=")}
        try:
            forms.add(json.dumps(secret)[1:-1])
        except (TypeError, ValueError):
            pass
        self.secret = secret
        self.forms = sorted((f for f in forms if len(f) >= 4), key=len, reverse=True)

    def __call__(self, text: str, cut: bool = False) -> str:
        for f in self.forms:
            text = text.replace(f, REDACTED)
        if cut:  # a response cut off mid-secret: drop a trailing start of it
            for n in range(min(len(self.secret) - 1, len(text)), 3, -1):
                if text.endswith(self.secret[:n]):
                    return text[:-n] + REDACTED
        return text


async def send(req: Prepared, secret: str, transport: httpx.AsyncBaseTransport | None = None,
               timeout: float = TIMEOUT_SECONDS) -> str:
    """Send it with the secret in its header. Returns the status, the useful headers and the body,
    redacted; errors come back as text too, never raised (so no traceback can hold the header)."""
    redact = Redactor(secret)
    if not secret or "\r" in secret or "\n" in secret:
        return "Not sent: the stored secret is empty or isn't a valid header value."
    headers = dict(req.headers)
    headers[req.header_name] = req.header_value_template.replace(PLACEHOLDER, secret)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False,
                                     trust_env=False) as client:
            async with client.stream(req.method, req.url, headers=headers, content=req.body) as resp:
                raw, cut = bytearray(), False
                async for chunk in resp.aiter_bytes():
                    raw += chunk
                    if len(raw) > MAX_RESPONSE_BYTES:
                        del raw[MAX_RESPONSE_BYTES:]
                        cut = True
                        break
                status, reason = resp.status_code, resp.reason_phrase
                shown = [(k, v) for k, v in resp.headers.items() if k.lower() in SHOWN_HEADERS]
                charset = resp.charset_encoding or "utf-8"
    except httpx.TimeoutException:
        return f"Not answered: {req.method} {req.url} timed out after {timeout:g}s."
    except httpx.HTTPError as e:  # only the type and a redacted message
        return f"Request failed ({type(e).__name__}): {redact(str(e))[:300]}"
    except Exception as e:  # never str(e): it could hold the headers
        return f"Request failed ({type(e).__name__})."
    text = bytes(raw).decode(charset, errors="replace")
    body = redact(text, cut=cut)
    if len(body) > MAX_BODY_CHARS:
        body, cut = redact(body[:MAX_BODY_CHARS], cut=True), True
    lines = [f"HTTP {status} {reason}".rstrip()]
    lines += [f"{k}: {redact(v)}" for k, v in shown]
    if status in (301, 302, 303, 307, 308):
        lines.append("(Redirects aren't followed, so the secret stays on its host.)")
    lines.append("")
    lines.append(body if body else "(empty body)")
    if cut:
        lines.append(f"\n[response cut off: showing the first {MAX_BODY_CHARS} characters]")
    return "\n".join(lines)
