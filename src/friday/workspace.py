"""Files in the agent's workspace, for /files and the list_files tool. Every path is resolved
(symlinks followed) and must stay inside the workspace."""

from __future__ import annotations

import heapq
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SKIP_DIRS = {"node_modules", "__pycache__"}  # besides hidden ones: never what the user is after


@dataclass(frozen=True)
class Entry:
    rel: str  # relative to the workspace, "/"-separated
    name: str
    is_dir: bool
    size: int
    mtime: float


def inside(root: Path, rel: str | Path) -> Path | None:
    """root/rel with symlinks resolved, or None if that leads outside root."""
    base = root.resolve()
    try:
        path = (base / rel).resolve()
    except (OSError, RuntimeError, ValueError):  # a symlink loop, a NUL byte
        return None
    return path if path.is_relative_to(base) else None


def _entry(base: Path, path: Path) -> Entry | None:
    real = inside(base, path)
    if real is None:
        return None
    try:
        st = real.stat()
    except OSError:
        return None
    is_dir = stat.S_ISDIR(st.st_mode)
    if not is_dir and not stat.S_ISREG(st.st_mode):
        return None
    return Entry(path.relative_to(base).as_posix(), path.name, is_dir, st.st_size, st.st_mtime)


def entries(root: Path, folder: str = ".") -> list[Entry] | None:
    """A folder's entries: folders first (by name), then files newest first. Hidden names and
    links leading outside root are left out. None if folder isn't a folder inside root."""
    base, d = root.resolve(), inside(root, folder or ".")
    if d is None or not d.is_dir():
        return None
    found = [e for child in d.iterdir() if not child.name.startswith(".") and (e := _entry(base, child))]
    return (sorted((e for e in found if e.is_dir), key=lambda e: e.name.lower())
            + sorted((e for e in found if not e.is_dir), key=lambda e: -e.mtime))


def recent(root: Path, limit: int = 10, max_files: int = 20000) -> list[Entry]:
    """The newest files anywhere under root, skipping hidden folders, symlinks and SKIP_DIRS."""
    base = root.resolve()
    found: list[Entry] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [n for n in dirnames if not n.startswith(".") and n not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath, name)
            if name.startswith(".") or path.is_symlink():
                continue
            if (e := _entry(base, path)) and not e.is_dir:
                found.append(e)
        if len(found) >= max_files:
            break
    return heapq.nlargest(limit, found, key=lambda e: e.mtime)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def local_time(mtime: float, tz: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(mtime, ZoneInfo(tz)).strftime(fmt)
