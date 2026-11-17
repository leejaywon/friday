"""Refuse to persist text that looks like a credential. A backstop for the prompt rule, not a scanner."""

from __future__ import annotations

import re

_TOKEN_PATTERNS = [
    r"\bsk-[A-Za-z0-9_-]{20,}",                 # OpenAI / Anthropic style keys
    r"\bgh[pousr]_[A-Za-z0-9]{30,}",            # GitHub tokens
    r"\bgithub_pat_[A-Za-z0-9_]{30,}",
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}",          # Slack
    r"\bAKIA[0-9A-Z]{16}\b",                    # AWS access key id
    r"\b\d{8,10}:[A-Za-z0-9_-]{30,}",           # Telegram bot token
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b\d{6}-[1-4]\d{6}\b",                    # Korean resident registration number
]
# A secret word followed by a value: "password: x", "pin is 1234", "비밀번호는 abcd".
_LABELLED = re.compile(
    r"(password|passwd|passcode|pass\s*code|\bpin\b|api[\s_-]?key|secret|recovery\s+code|seed\s+phrase"
    r"|비밀번호|비번|패스워드|암호|인증번호)"
    r"\s*(?:[:=]|\bis\b|는|은)\s*\S{3,}",
    re.IGNORECASE,
)
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def looks_secret(text: str) -> bool:
    if any(re.search(p, text) for p in _TOKEN_PATTERNS):
        return True
    if _LABELLED.search(text):
        return True
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return False


REFUSAL = (
    "Refused: this looks like a password, key, card or ID number, which can't go into memory, "
    "reminders, tasks or files. To keep it, use request_form with a password-type field and "
    "save_as: it goes to the encrypted vault."
)
