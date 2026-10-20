"""Pick a TTS implementation from the configuration."""

from __future__ import annotations

from ..config import TTSConfig
from .base import TTSEngine, TTSError
from .browser import BrowserTTS
from .edge import EdgeTTS
from .openai import OpenAITTS

_REGISTRY: dict[str, type[TTSEngine]] = {
    "edge": EdgeTTS,
    "openai": OpenAITTS,
    "browser": BrowserTTS,
}


def create_tts(config: TTSConfig) -> TTSEngine:
    try:
        engine_class = _REGISTRY[config.engine]
    except KeyError as exc:  # pragma: no cover - config validates the name
        raise TTSError(f"Unknown TTS engine '{config.engine}'") from exc
    return engine_class(config)


def available_engines() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["available_engines", "create_tts"]
