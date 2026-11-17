"""The Codex models this account can use, read from Codex's own model cache."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Model:
    slug: str
    name: str
    default_effort: str
    efforts: tuple[str, ...]


def _cache_path() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json"


def available_models(path: Path | None = None) -> list[Model]:
    """Models Codex lists for this account; empty if Codex hasn't fetched the list yet."""
    try:
        data = json.loads((path or _cache_path()).read_text())
    except (OSError, ValueError):
        return []
    out = []
    for m in data.get("models", []):
        if m.get("visibility") != "list" or not m.get("slug"):
            continue
        out.append(
            Model(
                slug=m["slug"],
                name=m.get("display_name") or m["slug"],
                default_effort=m.get("default_reasoning_level") or "",
                efforts=tuple(e["effort"] for e in m.get("supported_reasoning_levels", []) if e.get("effort")),
            )
        )
    return out


def find(models: list[Model], slug: str) -> Model | None:
    slug = slug.lower()
    return next((m for m in models if m.slug.lower() == slug), None)


def all_efforts(models: list[Model]) -> set[str]:
    return {e for m in models for e in m.efforts} or {"minimal", "low", "medium", "high", "xhigh", "max"}
