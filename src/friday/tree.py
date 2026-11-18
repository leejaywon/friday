"""Render directory trees for Telegram within the roots selected by show_tree."""

from __future__ import annotations

import html
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from .jobs import github_slug
from .secrets_guard import looks_secret

JUNK = {"node_modules", "venv", "__pycache__", "dist", "build", "target", "coverage"}
CONFIG_DIRS = {"config", "configs", "configuration"}
PRIVATE = {"credential", "credentials", "secret", "secrets",
           "ssh", "vault", "env", "token", "tokens", "google_token", "google_client_secret",
           "id_rsa", "id_ed25519"}
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
MAX_ENTRIES = 15
MAX_LINES = 80


def _visible(name: str, *, is_dir: bool) -> bool:
    lower = name.lower()
    return (
        not name.startswith(".")
        and lower not in JUNK
        and (not is_dir or lower not in CONFIG_DIRS)
        and lower.split(".")[0] not in PRIVATE
        and not lower.endswith((".egg-info", ".pem", ".key", ".p12", ".pfx"))
        and not looks_secret(name)
        and all(c.isprintable() for c in name)
    )


def directory(root: Path, path: str) -> Path:
    """Resolve a relative directory without accepting hidden components or symlinks."""
    sub = Path(path)
    if sub.is_absolute() or any(not _visible(part, is_dir=True) for part in sub.parts):
        raise ValueError("Invalid folder")
    root = root.parent.resolve(strict=True) / root.name
    if root.is_symlink():
        raise ValueError("Invalid root")
    target = root
    for part in sub.parts:
        target /= part
        if target.is_symlink():
            raise ValueError("Invalid folder")
    resolved = target.resolve(strict=True)
    if resolved != target or not resolved.is_relative_to(root) or not resolved.is_dir():
        raise ValueError("Invalid folder")
    return resolved


def _git(path: Path, *args: str, input: bytes | None = None) -> bytes:
    r = subprocess.run(["git", "-C", str(path), "-c", "core.fsmonitor=false", *args],
                       capture_output=True, input=input, timeout=10)
    return r.stdout if r.returncode == 0 else b""


def _git_text(path: Path, *args: str) -> str:
    return _git(path, *args).decode(errors="replace").strip()


@contextmanager
def _open_directory(path: Path):
    """Open a validated absolute path without following a symlink at any component."""
    fd = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _scan(fd: int, root: Path, depth: int, repo: Path | None,
          tracked_dirs: set[Path]) -> dict:
    with os.scandir(fd) as entries:
        candidates = {}
        for entry in entries:
            is_dir = entry.is_dir(follow_symlinks=False)
            if _visible(entry.name, is_dir=is_dir) and (is_dir or entry.is_file(follow_symlinks=False)):
                candidates[entry.name] = is_dir
    if repo is not None and candidates:
        paths = {
            os.fsencode(root.joinpath(name).relative_to(repo)) + (b"/" if is_dir else b""): name
            for name, is_dir in candidates.items()
            if root / name not in tracked_dirs
        }
        if paths:
            ignored = _git(repo, "check-ignore", "-z", "--stdin", input=b"\0".join(paths) + b"\0")
            for path in ignored.split(b"\0"):
                if path in paths:
                    candidates.pop(paths[path])
    names = sorted(candidates, key=lambda name: (not candidates[name], name.lower(), name))
    node = {}
    for name in names[:MAX_ENTRIES]:
        if candidates[name]:
            child = {}
            if depth > 1:
                try:
                    child_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
                except OSError:
                    pass
                else:
                    try:
                        child = _scan(child_fd, root / name, depth - 1, repo, tracked_dirs)
                    except OSError:
                        pass
                    finally:
                        os.close(child_fd)
            node[name + "/"] = child
        else:
            node[name] = None
    if len(names) > MAX_ENTRIES:
        node[f"… {len(names) - MAX_ENTRIES} more"] = None
    return node


def _render(node: dict, prefix: str = "") -> list[str]:
    lines = []
    for i, (name, child) in enumerate(node.items()):
        last = i == len(node) - 1
        lines.append(prefix + ("└── " if last else "├── ") + name)
        if child:
            lines += _render(child, prefix + ("    " if last else "│   "))
    return lines


def summary(root: Path) -> str:
    parts = [_git_text(root, "rev-parse", "--abbrev-ref", "HEAD") or "no branch"]
    if count := _git_text(root, "rev-list", "--count", "HEAD"):
        parts.append(f"{count} commits")
    if last := _git_text(root, "log", "-1", "--format=%cs %s"):
        if not looks_secret(last):
            parts.append("last: " + last[:70])
    return " · ".join(part for part in parts if not looks_secret(part))


def show_roots(roots: dict[str, Path], depth: int, project_dir: Path | None = None) -> tuple[str, str]:
    """Return plain text for the agent and escaped HTML for Telegram."""
    depth = max(1, min(depth, 4))
    repo = project_dir if project_dir and not (project_dir / ".git").is_symlink() and (
        (project_dir / ".git").is_dir() or (project_dir / ".git").is_file()
    ) else None
    tracked_dirs: set[Path] = set()
    head = link = ""
    if repo:
        tracked_dirs = {
            repo / parent
            for path in _git(repo, "ls-files", "-z").split(b"\0") if path
            for parent in Path(os.fsdecode(path)).parents
        }
        head = summary(repo)
        slug = github_slug(_git_text(repo, "remote", "get-url", "origin"))
        if slug and not looks_secret(slug):
            sub = next(iter(roots.values())).relative_to(repo)
            link = f"https://github.com/{slug}"
            if sub.parts:
                link += f"/tree/HEAD/{quote(sub.as_posix())}"

    sections = []
    for title, root in roots.items():
        try:
            with _open_directory(root) as fd:
                lines = _render(_scan(fd, root, depth, repo, tracked_dirs))
        except OSError:
            lines = ["(unavailable)"]
        if len(lines) > MAX_LINES:
            lines = lines[:MAX_LINES] + [f"… {len(lines) - MAX_LINES} more lines (try a subfolder)"]
        sections.append("\n".join([title, *(lines or ["(empty)"])]))
    body = "\n\n".join(sections)
    plain = "\n".join(x for x in (head, body, link) if x)
    msg = "\n".join(x for x in (f"<b>{html.escape(head)}</b>" if head else "",
                                f"<pre>{html.escape(body)}</pre>", html.escape(link)) if x)
    return plain, msg


def show(project_dir: Path, sub: str = "", depth: int = 2) -> tuple[str, str]:
    """Render a project tree using the original positional arguments."""
    project_dir = directory(project_dir, "")
    target = directory(project_dir, sub)
    relative = target.relative_to(project_dir)
    title = "/".join((project_dir.name, *relative.parts)) + "/"
    return show_roots({title: target}, depth, project_dir)
