"""A project's folder tree for Telegram (show_tree): what git tracks or would track (so .gitignore'd
build output, caches and dependencies stay out), folded below a depth, with a one-line git summary."""

from __future__ import annotations

import html
import os
import subprocess
from pathlib import Path

from .jobs import github_slug

# Skipped in folders that aren't git repos (git repos use their .gitignore).
JUNK = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".next", ".turbo", ".cache", "dist", "build", "target", "coverage", ".idea", ".vscode", ".DS_Store"}
MAX_ENTRIES = 15  # per folder; the rest become "… 외 N개"
MAX_LINES = 80


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(path), "-c", "core.fsmonitor=false", *args],
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""


def files(root: Path) -> list[str]:
    """Paths under root, relative to it."""
    listed = _git(root, "ls-files", "-co", "--exclude-standard", "--", ".")
    if listed or (root / ".git").exists():
        return [p for p in listed.splitlines() if p]
    out = []
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in JUNK and not x.endswith(".egg-info"))
        out += [os.path.relpath(os.path.join(d, n), root) for n in names if n not in JUNK]
    return out


def _nest(paths: list[str]) -> dict:
    tree: dict = {}
    for p in paths:
        node = tree
        *dirs, name = p.split("/")
        for d in dirs:
            node = node.setdefault(d + "/", {})
        node[name] = None
    return tree


def _count(node: dict) -> int:
    return sum(1 if v is None else _count(v) for v in node.values())


def _render(node: dict, depth: int, prefix: str = "") -> list[str]:
    names = sorted(node, key=lambda n: (node[n] is None, n.lower()))
    shown, rest = names[:MAX_ENTRIES], len(names) - MAX_ENTRIES
    lines = []
    for i, name in enumerate(shown):
        last = i == len(shown) - 1 and rest <= 0
        child = node[name]
        label = name
        if child is not None and depth <= 1:
            label += f"  (파일 {_count(child)}개)"
        lines.append(prefix + ("└── " if last else "├── ") + label)
        if child is not None and depth > 1:
            lines += _render(child, depth - 1, prefix + ("    " if last else "│   "))
    if rest > 0:
        lines.append(prefix + f"└── … 외 {rest}개")
    return lines


def summary(root: Path) -> str:
    """'main · 커밋 12개 · 수정 중 2개 · 최근: 2026-11-17 Fix login' (empty if not a git repo)."""
    if not (root / ".git").exists():
        return ""
    parts = [_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "no branch"]
    if count := _git(root, "rev-list", "--count", "HEAD"):
        parts.append(f"커밋 {count}개")
    if changed := len(_git(root, "status", "--porcelain").splitlines()):
        parts.append(f"수정 중 {changed}개")
    if last := _git(root, "log", "-1", "--format=%cs %s"):
        parts.append("최근: " + last[:70])
    return " · ".join(parts)


def show(project_dir: Path, sub: str = "", depth: int = 2) -> tuple[str, str]:
    """(plain text for the agent, HTML message for Telegram)."""
    root = project_dir / sub if sub else project_dir
    paths = files(root)
    lines = _render(_nest(paths), max(1, min(depth, 4)))
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"… {len(lines) - MAX_LINES}줄 더 (하위 폴더를 지정해 보세요)"]
    title = project_dir.name + "/" + (sub.strip("/") + "/" if sub else "")
    head = summary(project_dir)
    body = "\n".join([title, *lines]) if paths else title + "\n(비어 있음)"
    slug = github_slug(_git(project_dir, "remote", "get-url", "origin"))
    link = f"https://github.com/{slug}" + (f"/tree/HEAD/{sub.strip('/')}" if sub else "") if slug else ""
    plain = "\n".join(x for x in (head, body, link) if x)
    msg = "\n".join(x for x in (f"<b>{html.escape(head)}</b>" if head else "",
                                f"<pre>{html.escape(body)}</pre>", html.escape(link)) if x)
    return plain, msg
