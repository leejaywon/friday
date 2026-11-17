"""Forms: what the agent asks the owner for with request_form (request_login and request_code are
forms too). The owner answers by replying to the request message, one field per line; the bot
deletes the reply from the chat.

Sensitive fields (passwords by default) go only to the vault, under the request's save_as name
and bound to its domain if it has one; the agent gets only the other values back.
"""

from __future__ import annotations

import json
import re

# The fields a form may have (request_form). Sensitive values go only to the vault and never
# back to the agent; the others are returned to it.
FIELD_NAME = re.compile(r"^[\w.-]{1,40}$")
FIELD_TYPES = ("text", "password", "code", "number", "email", "url", "textarea", "select", "choice",
               "checkbox", "date", "time")
MAX_FIELDS = 12
MAX_OPTIONS = 20
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
            raise ValueError(f"field name '{name}': letters, digits, '.', '_' or '-' (max 40), unique")
        if kind not in FIELD_TYPES:
            raise ValueError(f"field '{name}': type must be one of {', '.join(FIELD_TYPES)}")
        f = {"name": name, "label": _text(item.get("label") or name, 60), "type": kind,
             "required": bool(item.get("required", kind != "checkbox")),
             "sensitive": bool(item.get("sensitive", kind == "password" or sensitive_default))}
        if item.get("help"):
            f["help"] = _text(item["help"], 160)
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


def make_spec(title: str, fields: list[dict], description: str = "", domain: str = "", save_as: str = "") -> dict:
    """The form as stored with the request and shown in its message."""
    if not fields:
        raise ValueError("a form needs at least one field")
    spec = {"title": _text(title, 80) or "Friday", "description": _text(description, 300), "site": domain,
            "save_as": save_as if any(f["sensitive"] for f in fields) else "", "fields": fields}
    return spec


def login_spec(name: str, domain: str, reason: str = "", extra: list[dict] | None = None) -> dict:
    """request_login and /login: username and password (both kept in the vault) plus extra fields."""
    fields = [
        {"name": "username", "label": "Username", "type": "text", "required": True, "sensitive": True},
        {"name": "password", "label": "Password", "type": "password", "required": True, "sensitive": True},
        *(extra or []),
    ]
    return make_spec(f"{domain} login", fields, (reason or "").strip(), domain, name)


def code_spec(reason: str, label: str = "Code") -> dict:
    """request_code: one code, returned to the agent."""
    field = {"name": "code", "label": _text(label, 60) or "Code", "type": "code", "required": True,
             "sensitive": False}
    return make_spec(field["label"], [field], reason)


TRUE = ("true", "1", "y", "yes", "on", "예", "네")


def check_values(fields: list[dict], values: dict) -> dict:
    """The submitted values, checked against the request's own fields. Returns {name: str | bool};
    raises ValueError naming the fields to fix (labels only)."""
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
        raise ValueError("Check these fields: " + ", ".join(bad))
    return out
