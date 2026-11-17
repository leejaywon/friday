"""Forms: what the agent asks the owner for with request_form (logins, API keys, any input) and
request_code. The owner fills one in in the Mini App (miniapp.py, the "form" component of
site/app/) or replies to the request message, one field per line; the bot deletes the reply.

The agent lists exactly the fields it needs (an email; a username and password; one API key; a
public and a secret key...). Sensitive fields (passwords by default) are never returned to the
agent; with save_as, the answer is kept in the vault under that name, bound to the form's domain,
so fill_login and fill_login_field can type it. The owner can mark any field "not needed" when
the agent asked for something the site doesn't use; the agent is told which.
"""

from __future__ import annotations

import json
import re

# The fields a form may have (request_form). Sensitive values go only to the vault and never
# back to the agent; the others are returned to it.
FIELD_NAME = re.compile(r"^[A-Za-z0-9][\w.-]{0,39}$")
FIELD_TYPES = ("text", "password", "code", "number", "email", "url", "textarea", "select", "choice",
               "checkbox", "date", "time")
MAX_FIELDS = 12
MAX_OPTIONS = 20
MAX_SPEC_BYTES = 2000  # the spec rides in the Mini App's URL, with the ~600-character public key
LOGIN_KEYS = ("username", "password")  # sensitive fields with these names make a vault entry a website login


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
            raise ValueError('each field is an object like {"name": "account", "label": "Account", "type": "text"}')
        name, kind = str(item.get("name") or "").strip(), str(item.get("type") or "text").strip().lower()
        if not FIELD_NAME.match(name) or name in seen:
            raise ValueError(f"field name '{name}': letters, digits, '.', '_' or '-' (max 40, starting with a letter or digit), unique")
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
        out.append(f)
        seen.add(name)
    return out


def make_spec(title: str, fields: list[dict], description: str = "", domain: str = "", save_as: str = "",
              submit: str = "") -> dict:
    """The form as stored with the request: shown in its message and by the Mini App."""
    if not fields:
        raise ValueError("a form needs at least one field")
    spec = {"kind": "form", "title": _text(title, 80) or "Friday", "description": _text(description, 300),
            "site": domain, "save_as": save_as,
            "submit": _text(submit, 30), "fields": fields}
    if len(json.dumps(spec, ensure_ascii=False).encode()) > MAX_SPEC_BYTES:
        raise ValueError("the form is too big for Telegram's button: fewer fields or shorter labels, help or options")
    return spec


def login_spec(name: str, domain: str, reason: str = "") -> dict:
    """/login: a username or email and a password; either can be marked not needed."""
    fields = [
        {"name": "username", "label": "Username or email", "type": "text", "required": True, "sensitive": False},
        {"name": "password", "label": "Password", "type": "password", "required": True, "sensitive": True},
    ]
    return make_spec(f"{domain} login", fields, (reason or "").strip(), domain, name, "Save")


def code_spec(reason: str, label: str = "Code") -> dict:
    """request_code: one code, returned to the agent."""
    field = {"name": "code", "label": _text(label, 60) or "Code", "type": "code", "required": True,
             "sensitive": False}
    return make_spec(field["label"], [field], reason, submit="Send")


TRUE = ("true", "1", "y", "yes", "on", "예", "네")


SKIP = "-"  # a reply line that marks its field "not needed"
SKIPPED_KEY = "_skipped"  # in a request's stored result: the fields marked not needed (not a valid field name)


def check_values(fields: list[dict], values: dict, skipped: list | tuple = ()) -> tuple[dict, list[str]]:
    """The submitted values, checked against the request's own fields, and the names of the fields
    the owner marked not needed (left empty, never required). Returns ({name: str | bool},
    skipped); raises ValueError naming the fields to fix (labels only)."""
    names = {f["name"] for f in fields}
    skipped = [n for n in dict.fromkeys(skipped) if n in names]
    out, bad = {}, []
    for f in fields:
        name, kind, raw = f["name"], f["type"], values.get(f["name"])
        if name in skipped:
            out[name] = False if kind == "checkbox" else ""
            continue
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
        raise ValueError("Check these fields: " + ", ".join(bad))
    return out, skipped


def vault_values(fields: list[dict], clean: dict, skipped: list[str]) -> dict[str, str]:
    """What a form with save_as keeps in the vault: every value the owner gave, sensitive or not,
    so fill_login and fill_login_field can type the username or email too."""
    return {f["name"]: str(clean[f["name"]]) for f in fields
            if f["name"] not in skipped and isinstance(clean[f["name"]], str) and clean[f["name"]]}


def outcome(spec: dict, name: str, values: dict, skipped: list[str]) -> str:
    """What the agent learns from an answered form: the non-sensitive values, the sensitive ones
    only by name, the fields marked not needed, and how to use a saved login."""
    out = dict(values)
    out.update({f["name"]: f"stored as {name}.{f['name']}" for f in spec["fields"]
                if f["sensitive"] and f["name"] not in skipped})
    text = "Submitted: " + json.dumps(out, ensure_ascii=False)
    if skipped:
        labels = [f["label"] for f in spec["fields"] if f["name"] in skipped]
        text += ". The user marked these as not needed: " + ", ".join(labels)
    if name and spec.get("site"):
        text += (f". Saved as login '{name}' for {spec['site']}: fill_login types its username (or email) "
                 "and password, fill_login_field any of its fields; it won't ask again on that site")
    return text
