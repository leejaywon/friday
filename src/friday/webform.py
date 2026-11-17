"""The secure form (site/login/index.html): a Telegram Mini App where the owner fills in what the
agent asked for with request_form (request_login and request_code are forms too).

The bot opens it from a reply-keyboard button with the form's spec in the URL fragment (never
sent to the web host): the request id, title, fields and this server's public key. The page
encrypts the values with a fresh AES-256-GCM key, wraps that key with RSA-OAEP-SHA256 and sends
{v, k, iv, ct} back through Telegram.WebApp.sendData, so Telegram only sees ciphertext. The
private key sits in the vault folder, which the agents' shells and coding jobs can't read.

Sensitive fields (passwords by default) go only to the vault, under the request's save_as name
and bound to its domain if it has one; the agent gets only the other values back.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# The fields a form may have (request_form). Sensitive values go only to the vault and never
# back to the agent; the others are returned to it.
FIELD_NAME = re.compile(r"^[\w.-]{1,40}$")
FIELD_TYPES = ("text", "password", "code", "number", "email", "url", "textarea", "select", "choice",
               "checkbox", "date", "time")
MAX_FIELDS = 12
MAX_OPTIONS = 20
MAX_SPEC_BYTES = 2000  # the spec rides in the button's URL, with the ~600-character public key
LOGIN_KEYS = ("username", "password")  # sensitive fields with these names make a vault entry a website login
OAEP = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_fields(raw: str | list, sensitive_default: bool = False) -> list[dict]:
    """Check a form's fields (JSON list or list of objects) and normalize them. Raises ValueError
    with a message the agent can act on."""
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"fields isn't valid JSON ({e})") from None
    if not isinstance(raw, list) or len(raw) > MAX_FIELDS:
        raise ValueError(f"fields must be a JSON list of at most {MAX_FIELDS} objects")
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('each field is an object like {"name": "account", "label": "계정", "type": "text"}')
        name, kind = str(item.get("name") or "").strip(), str(item.get("type") or "text").strip().lower()
        if not FIELD_NAME.match(name) or name in seen:
            raise ValueError(f"field name '{name}': letters, digits, '.', '_' or '-' (max 40), unique")
        if kind not in FIELD_TYPES:
            raise ValueError(f"field '{name}': type must be one of {', '.join(FIELD_TYPES)}")
        f = {"name": name, "label": _text(item.get("label") or name, 60), "type": kind,
             "required": bool(item.get("required", kind != "checkbox")),
             "sensitive": bool(item.get("sensitive", kind == "password" or sensitive_default))}
        for key, limit in (("placeholder", 60), ("help", 160)):
            if item.get(key):
                f[key] = _text(item[key], limit)
        if kind in ("select", "choice"):
            opts = item.get("options")
            if not isinstance(opts, list) or not 1 <= len(opts) <= MAX_OPTIONS:
                raise ValueError(f"field '{name}': options must be a list of 1 to {MAX_OPTIONS} choices")
            f["options"] = [{"value": _text(o.get("value"), 40), "label": _text(o.get("label") or o.get("value"), 40)}
                            if isinstance(o, dict) else {"value": _text(o, 40), "label": _text(o, 40)} for o in opts]
            if not all(o["value"] for o in f["options"]):
                raise ValueError(f"field '{name}': an option is empty")
        if item.get("default") not in (None, ""):
            f["default"] = bool(item["default"]) if kind == "checkbox" else _text(item["default"], 100)
        if re.fullmatch(r"[a-z -]{1,40}", str(item.get("autocomplete") or "")):
            f["autocomplete"] = item["autocomplete"]
        out.append(f)
        seen.add(name)
    return out


def make_spec(title: str, fields: list[dict], description: str = "", domain: str = "", save_as: str = "",
              submit: str = "") -> dict:
    """The form as the page renders it (the bot adds the request id and the public key)."""
    if not fields:
        raise ValueError("a form needs at least one field")
    spec = {"title": _text(title, 80) or "Friday", "description": _text(description, 300), "site": domain,
            "save_as": save_as if any(f["sensitive"] for f in fields) else "", "submit": _text(submit, 30),
            "fields": fields}
    if len(json.dumps(spec, ensure_ascii=False).encode()) > MAX_SPEC_BYTES:
        raise ValueError("the form is too big for Telegram's button: fewer fields or shorter labels, help or options")
    return spec


def login_spec(name: str, domain: str, reason: str = "", extra: list[dict] | None = None) -> dict:
    """request_login and /login: username and password (both kept in the vault) plus extra fields."""
    fields = [
        {"name": "username", "label": "아이디", "type": "text", "required": True, "sensitive": True,
         "autocomplete": "username"},
        {"name": "password", "label": "비밀번호", "type": "password", "required": True, "sensitive": True,
         "autocomplete": "current-password"},
        *(extra or []),
    ]
    # A short reason ("건국대학교 eCampus 로그인") makes the better title; the page shows the domain itself.
    reason = (reason or "").strip()
    if reason and len(reason) <= 40 and "\n" not in reason:
        return make_spec(reason, fields, "", domain, name, "저장")
    return make_spec(f"{domain} 로그인", fields, reason, domain, name, "저장")


def code_spec(reason: str, label: str = "인증번호") -> dict:
    """request_code: one code, returned to the agent."""
    field = {"name": "code", "label": _text(label, 60) or "인증번호", "type": "code", "required": True,
             "sensitive": False, "autocomplete": "one-time-code"}
    return make_spec(field["label"], [field], reason, submit="보내기")


TRUE = ("true", "1", "y", "yes", "on", "예", "네")


def check_values(fields: list[dict], values: dict) -> dict:
    """The submitted values, checked against the request's own fields (never the page's word for
    them). Returns {name: str | bool}; raises ValueError naming the fields to fix (labels only)."""
    out, bad = {}, []
    for f in fields:
        name, kind, raw = f["name"], f["type"], values.get(f["name"])
        if kind == "checkbox":
            out[name] = raw is True or str(raw).strip().lower() in TRUE
            if f["required"] and not out[name]:
                bad.append(f["label"])
            continue
        v = "" if raw is None or isinstance(raw, (dict, list)) else str(raw)
        v = v if kind == "password" else v.strip()
        ok = len(v) <= (1000 if kind == "textarea" else 256)
        if v and ok and kind in ("select", "choice"):
            match = [o["value"] for o in f["options"] if v in (o["value"], o["label"])]
            ok, v = bool(match), (match or [v])[0]
        elif v and ok and kind == "number":
            ok = bool(re.fullmatch(r"-?\d+(\.\d+)?", v))
        elif v and ok and kind in ("date", "time", "email", "url"):
            ok = bool(re.fullmatch({"date": r"\d{4}-\d{2}-\d{2}", "time": r"\d{2}:\d{2}(:\d{2})?",
                                    "email": r"[^@\s]+@[^@\s]+", "url": r"https?://\S+"}[kind], v))
        if not ok or (f["required"] and not v):
            bad.append(f["label"])
        out[name] = v
    if bad:
        raise ValueError("확인이 필요한 칸: " + ", ".join(bad))
    return out


class FormKey:
    """The server's RSA-3072 key pair for the form, created on first use (0600, never replaced)."""

    def __init__(self, vault_dir: Path):
        self.path = vault_dir / "form_key.pem"

    def _private(self) -> rsa.RSAPrivateKey:
        if not self.path.exists():
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            try:  # O_EXCL: two processes starting at once keep the first key
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(pem)
        key = serialization.load_pem_private_key(self.path.read_bytes(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        return key

    def public_b64(self) -> str:
        """SubjectPublicKeyInfo (DER), base64url: what WebCrypto's importKey('spki') takes."""
        der = self._private().public_key().public_bytes(serialization.Encoding.DER,
                                                        serialization.PublicFormat.SubjectPublicKeyInfo)
        return b64url(der)

    def decrypt(self, data: str) -> dict:
        """sendData's {v, k, iv, ct} -> {rid, values}. Raises ValueError on anything malformed."""
        try:
            msg = json.loads(data)
            if msg.get("v") != 1:
                raise ValueError("unknown version")
            aes = self._private().decrypt(unb64url(msg["k"]), OAEP)
            plain = AESGCM(aes).decrypt(unb64url(msg["iv"]), unb64url(msg["ct"]), None)
            out = json.loads(plain)
            if not isinstance(out.get("rid"), int) or not isinstance(out.get("values"), dict):
                raise ValueError("bad payload")
            return out
        except ValueError:
            raise
        except Exception as e:  # wrong key, tampered data, missing keys; never echo the content
            raise ValueError(type(e).__name__) from None


def form_url(base: str, spec: dict) -> str:
    """The Mini App URL: base + '#' + base64url(JSON spec)."""
    return base + "#" + b64url(json.dumps(spec, ensure_ascii=False, separators=(",", ":")).encode())


def encrypt_like_the_page(public_b64: str, payload: dict) -> str:
    """What site/login/index.html sends, for tests."""
    pub = serialization.load_der_public_key(unb64url(public_b64))
    aes, iv = os.urandom(32), os.urandom(12)
    ct = AESGCM(aes).encrypt(iv, json.dumps(payload).encode(), None)
    return json.dumps({"v": 1, "k": b64url(pub.encrypt(aes, OAEP)), "iv": b64url(iv), "ct": b64url(ct)})  # type: ignore[union-attr]
