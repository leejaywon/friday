"""The phone book: parsing and showing phone numbers and contacts (the contact tools in
mcp_server, stored in the contacts table). Numbers are kept in international form (+82...), Korean
ones shown the Korean way (010-1234-5678)."""

from __future__ import annotations

import json
import re
import sqlite3

NUMBER = re.compile(r"\+?\d[\d\s().-]{2,}\d")


def normalize(number: str) -> str:
    """'010-1234-5678' -> '+821012345678', '0044 20 7946 0958' -> '+442079460958'. Short or
    service numbers without a leading 0 (1588-1234, 112) stay as their digits."""
    digits = re.sub(r"\D", "", number)
    if number.strip().startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return "+82" + digits[1:]
    return digits


def display(number: str) -> str:
    """Korean numbers the Korean way (010-1234-5678, 02-123-4567); others as stored."""
    if not number.startswith("+82"):
        return number
    d = "0" + number[3:]
    if d.startswith("02"):
        return f"02-{d[2:-4]}-{d[-4:]}" if len(d) in (9, 10) else d
    if len(d) in (10, 11):
        return f"{d[:3]}-{d[3:-4]}-{d[-4:]}"
    return d


def parse_phones(items: list[str] | str) -> list[dict]:
    """["휴대폰: 010-1234-5678", "+1 415 555 0100 (US)"] -> [{"number": "+8210...", "label": "휴대폰"}].
    Raises ValueError when an item has no number."""
    if isinstance(items, str):
        items = [x for x in re.split(r"[,\n;]", items) if x.strip()]
    out = []
    for item in items:
        m = NUMBER.search(str(item))
        if not m or len(re.sub(r"\D", "", m.group())) < 3:
            raise ValueError(f"no phone number in '{item}'")
        label = (str(item)[: m.start()] + " " + str(item)[m.end():]).strip(" :()-·,")
        number = normalize(m.group())
        if all(p["number"] != number for p in out):
            out.append({"number": number, "label": label[:30]})
    return out


def split_list(value: list[str] | str) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[,\n;]", value or "")
    out: list[str] = []
    for x in items:
        x = " ".join(str(x).split())
        if x and x not in out:
            out.append(x)
    return out


def number_matches(query: str, phones: list[dict]) -> bool:
    """A query of 4+ digits matches a number containing them, in either form (+8210... or 010...)."""
    q = re.sub(r"\D", "", query)
    if len(q) < 4:
        return False
    return any(q in p["number"] or q in re.sub(r"\D", "", display(p["number"])) for p in phones)


def show(row: sqlite3.Row) -> str:
    """'#3 김민수 (Minsu Kim, 민수형)' then phones, emails and notes on their own lines."""
    aliases = json.loads(row["aliases"])
    lines = [f"#{row['id']} {row['name']}" + (f" ({', '.join(aliases)})" if aliases else "")]
    phones = json.loads(row["phones"])
    if phones:
        lines.append("  " + " · ".join((p["label"] + " " if p["label"] else "") + display(p["number"]) for p in phones))
    if emails := json.loads(row["emails"]):
        lines.append("  " + " · ".join(emails))
    if row["notes"]:
        lines.append("  메모: " + row["notes"].replace("\n", "\n  "))
    return "\n".join(lines)
