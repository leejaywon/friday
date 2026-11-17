"""Speech to text for voice messages and audio files, through an OpenAI-compatible API.

Codex can't listen to audio, so Friday transcribes a recording when it arrives and gives the
agent the text. Groq (Whisper, fast and cheap) and OpenAI are supported; both accept Telegram's
OGG/Opus voice notes directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

@dataclass(frozen=True)
class Provider:
    url: str
    default_model: str


PROVIDERS = {
    "groq": Provider("https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3-turbo"),
    "openai": Provider("https://api.openai.com/v1/audio/transcriptions", "gpt-4o-mini-transcribe"),
}

MIME_TYPES = {
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".wav": "audio/wav", ".webm": "audio/webm",
    ".flac": "audio/flac",
}


class TranscriptionError(Exception):
    pass


def model_for(provider: str, model: str = "") -> str:
    return model or PROVIDERS[provider].default_model


async def transcribe(
    path: Path,
    *,
    provider: str,
    api_key: str,
    model: str = "",
    language: str = "",
    timeout_seconds: float = 120.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """The text spoken in `path`. Raises TranscriptionError with a short, key-free reason."""
    if provider not in PROVIDERS:
        raise TranscriptionError(f"unknown provider {provider!r} (use {' or '.join(PROVIDERS)})")
    if not api_key:
        raise TranscriptionError("no API key")
    data = {"model": model_for(provider, model), "response_format": "json"}
    if language:
        data["language"] = language
    mime = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
            with path.open("rb") as f:
                resp = await client.post(
                    PROVIDERS[provider].url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    data=data,
                    files={"file": (path.name, f, mime)},
                )
    except Exception as e:  # httpx errors, and h11's (not httpx's) when the upload goes wrong
        raise TranscriptionError(f"{type(e).__name__}: {e}"[:300]) from e
    if resp.status_code != 200:
        raise TranscriptionError(f"{provider} returned HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        text = resp.json()["text"]
    except (ValueError, KeyError, TypeError) as e:
        raise TranscriptionError(f"unexpected response from {provider}: {resp.text[:300]}") from e
    return str(text).strip()
