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


def summary(store, default_model: str, default_effort: str) -> str:
    model = store.get("model") or default_model or "Codex default"
    effort = store.get("effort") or default_effort or "model default"
    return f"Model: {model}, effort: {effort}"


def model_list(models: list[Model]) -> str:
    if not models:
        return "Usage: /model <name> [effort], /model <effort>, /model default"
    lines = ["Available models (efforts, default*):"]
    for m in models:
        levels = ", ".join(e + ("*" if e == m.default_effort else "") for e in m.efforts)
        lines.append(f"• {m.slug} ({levels})")
    lines.append("\nUsage: /model gpt-6-sol high, /model low, /model default")
    return "\n".join(lines)


def switch(store, default_model: str, args: list[str], models: list[Model]) -> str:
    """Apply '/model' arguments (a model, an effort, both, or 'default') to the stored choice.

    Returns a note to append to the summary; raises ValueError with a message for the user."""
    efforts = all_efforts(models)
    if args and args[0].lower() in ("default", "reset"):
        store.delete("model")
        store.delete("effort")
        return ""
    model_arg = next((a for a in args if a.lower() not in efforts), None)
    effort_arg = next((a.lower() for a in args if a.lower() in efforts), None)
    if model_arg:
        m = find(models, model_arg)
        if models and not m:
            raise ValueError(f"Unknown model '{model_arg}'.\n\n{model_list(models)}")
    current = find(models, (m.slug if model_arg and m else model_arg) or store.get("model") or default_model)
    if effort_arg and current and effort_arg not in current.efforts:
        raise ValueError(f"{current.slug} supports: {', '.join(current.efforts)}")
    if model_arg:
        store.set("model", m.slug if m else model_arg)
    if effort_arg:
        store.set("effort", effort_arg)
    elif current and (store.get("effort") or "") not in ("", *current.efforts):
        store.delete("effort")
        return "\n(Reset effort: the new model doesn't support the previous one.)"
    return ""
