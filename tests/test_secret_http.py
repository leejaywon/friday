"""http_request: a vault secret goes into a header, reaches only its own host, and never comes back."""

import asyncio
import base64
import functools
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from friday import secret_http
from friday.vault import Vault

SECRET = "tok_test_0123456789abcdefSECRET"  # a made-up value, not a real token


class _Echo(BaseHTTPRequestHandler):
    """Answers with what it received: method, path, headers and body (as a careless API might)."""

    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        out = json.dumps({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        self.server.seen.append(dict(self.headers))
        data = out.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Echo-Auth", self.headers.get("Authorization", ""))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _answer

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture
def echo():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    srv.seen = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _run(coro):
    return asyncio.run(coro)


def test_a_real_request_carries_the_secret_and_the_output_never_does(echo):
    port = echo.server_address[1]
    req = secret_http.prepare("POST", f"http://127.0.0.1:{port}/v1/me", "Authorization: Bearer {secret}",
                              headers='{"Accept": "application/json"}', body='{"ping": 1}',
                              allow_http_loopback=True)
    out = _run(secret_http.send(req, SECRET))
    # The server got the header with the real value...
    assert echo.seen[0]["Authorization"] == f"Bearer {SECRET}"
    assert echo.seen[0]["Content-Type"] == "application/json"
    # ...and the agent's result has the status and body, with every echo of it redacted.
    assert out.startswith("HTTP 200 OK")
    assert SECRET not in out
    assert "Bearer [redacted]" in out
    assert '"ping": 1' in out or '\\"ping\\": 1' in out
    assert "X-Echo-Auth" not in out  # only the useful headers are returned


def test_plain_http_is_refused_outside_tests():
    with pytest.raises(secret_http.Refused):
        secret_http.prepare("GET", "http://api.example.com/v1/me", "Authorization: Bearer {secret}")


@pytest.mark.parametrize("url,header,why", [
    ("https://api.example.com/x?t={secret}", "Authorization: Bearer {secret}", "URL"),
    ("https://user:pw@api.example.com/x", "Authorization: Bearer {secret}", "plain"),
    ("ftp://api.example.com/x", "Authorization: Bearer {secret}", "https"),
    ("https://api.example.com/x", "Authorization: Bearer", "exactly once"),
    ("https://api.example.com/x", "Authorization: {secret} {secret}", "exactly once"),
    ("https://api.example.com/x", "Host: {secret}", "can't carry"),
    ("https://api.example.com/x", "Bad Name: {secret}", "header"),
])
def test_bad_requests_are_refused_before_the_vault_is_read(url, header, why):
    with pytest.raises(secret_http.Refused, match=why):
        secret_http.prepare("GET", url, header)


def test_secret_can_only_go_in_the_header_template():
    with pytest.raises(secret_http.Refused, match="body"):
        secret_http.prepare("POST", "https://a.example.com/", "X-Key: {secret}", body='{"k": "{secret}"}')
    with pytest.raises(secret_http.Refused, match="template"):
        secret_http.prepare("GET", "https://a.example.com/", "X-Key: {secret}", headers="X-Other: {secret}")
    with pytest.raises(secret_http.Refused, match="can't be set"):
        secret_http.prepare("GET", "https://a.example.com/", "X-Key: {secret}", headers="Host: evil.com")


def test_redaction_covers_encodings_and_a_cut_off_tail():
    r = secret_http.Redactor(SECRET)
    b64 = base64.b64encode(SECRET.encode()).decode()
    assert SECRET not in r(f"a {SECRET} b") and b64 not in r(f"basic {b64}")
    assert r("tail " + SECRET[:12], cut=True) == "tail [redacted]"


# --- through the MCP tool, with the vault --------------------------------------------------

@pytest.fixture
def mock(tools, monkeypatch):
    """Route the tool's requests to a fake API; returns the list of requests it got."""
    got = []

    def handler(request: httpx.Request):
        got.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "https://evil.example.net/steal"})
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * (secret_http.MAX_RESPONSE_BYTES + 10) + SECRET.encode())
        if request.url.path == "/denied":
            return httpx.Response(401, json={"error": "invalid token"}, headers={"WWW-Authenticate": "Bearer"})
        return httpx.Response(200, json={"you_sent": request.headers.get("authorization"),
                                         "b64": base64.b64encode(SECRET.encode()).decode()},
                              headers={"Set-Cookie": "s=1", "X-Request-Id": "abc"})

    monkeypatch.setattr(tools.secret_http, "send",
                        functools.partial(secret_http.send, transport=httpx.MockTransport(handler)))
    return got


def _save(tools, name, values, domains):
    Vault(tools.cfg.vault_dir).save(name, values, domains, trusted=bool(domains))


def test_tool_sends_a_token_only_to_its_own_host(tools, mock):
    _save(tools, "example_api", {"api_key": SECRET}, ["api.example.com"])
    out = _run(tools.http_request("GET", "https://api.example.com/v1/me", "example_api"))
    assert mock[0].headers["authorization"] == f"Bearer {SECRET}"
    assert out.startswith("HTTP 200") and SECRET not in out and "[redacted]" in out
    assert "x-request-id: abc" in out and "set-cookie" not in out.lower()

    # A prompt-injected URL elsewhere (or a look-alike) is refused and nothing is sent.
    for url in ("https://evil.example.net/collect", "https://api.example.com.evil.net/",
                "https://notapi.example.com.evil/"):
        out = _run(tools.http_request("GET", url, "example_api"))
        assert out.startswith("Not sent: Wrong host") and SECRET not in out
    assert len(mock) == 1


def test_tool_never_follows_a_redirect(tools, mock):
    _save(tools, "example_api", {"api_key": SECRET}, ["api.example.com"])
    out = _run(tools.http_request("GET", "https://api.example.com/redirect", "example_api"))
    assert out.startswith("HTTP 302") and "aren't followed" in out
    assert [str(r.url.host) for r in mock] == ["api.example.com"]


def test_tool_reports_401_and_caps_large_responses(tools, mock):
    _save(tools, "example_api", {"api_key": SECRET}, ["api.example.com"])
    out = _run(tools.http_request("GET", "https://api.example.com/denied", "example_api"))
    assert out.startswith("HTTP 401") and "www-authenticate: Bearer" in out
    out = _run(tools.http_request("GET", "https://api.example.com/big", "example_api"))
    assert "cut off" in out and SECRET not in out and len(out) < secret_http.MAX_BODY_CHARS + 500


def test_tool_refuses_unbound_unknown_and_ambiguous_secrets(tools, mock):
    _save(tools, "loose_key", {"api_key": SECRET}, [])
    out = _run(tools.http_request("GET", "https://api.example.com/x", "loose_key"))
    assert "isn't bound" in out and "request_form" in out
    out = _run(tools.http_request("GET", "https://api.example.com/x", "nope"))
    assert "no secret named 'nope'" in out and "loose_key" in out
    _save(tools, "pair", {"public_key": "pub", "secret_key": SECRET}, ["api.example.com"])
    out = _run(tools.http_request("GET", "https://api.example.com/x", "pair"))
    assert "name one as pair.<field>" in out
    out = _run(tools.http_request("GET", "https://api.example.com/x", "pair.secret_key", header="X-Key: {secret}"))
    assert mock[-1].headers["x-key"] == SECRET and SECRET not in out


def test_a_secret_also_goes_to_subdomains_of_its_domain(tools, mock):
    _save(tools, "example_api", {"api_key": SECRET}, ["api.example.com"])
    out = _run(tools.http_request("GET", "https://v2.api.example.com/v1/me", "example_api"))
    assert out.startswith("HTTP 200") and SECRET not in out


def test_errors_never_hold_the_secret(tools, monkeypatch):
    _save(tools, "example_api", {"api_key": SECRET}, ["api.example.com"])

    def boom(request):
        raise httpx.ConnectError(f"failed with {request.headers['authorization']}")

    monkeypatch.setattr(tools.secret_http, "send",
                        functools.partial(secret_http.send, transport=httpx.MockTransport(boom)))
    out = _run(tools.http_request("GET", "https://api.example.com/x", "example_api"))
    assert out.startswith("Request failed (ConnectError)") and SECRET not in out
