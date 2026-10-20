"""Pick an ASR implementation from the configuration."""

from __future__ import annotations

from ..config import ASRConfig
from .base import ASREngine, ASRError
from .local import FasterWhisperASR
from .openai import OpenAICompatibleASR

_REGISTRY: dict[str, type[ASREngine]] = {
    "local": FasterWhisperASR,
    "openai": OpenAICompatibleASR,
}


def create_asr(config: ASRConfig) -> ASREngine | None:
    """``engine="browser"`` needs no backend engine at all -> ``None``."""
    if config.engine == "browser":
        return None
    try:
        engine_class = _REGISTRY[config.engine]
    except KeyError as exc:  # pragma: no cover - config validates the name
        raise ASRError(f"Unknown ASR engine '{config.engine}'") from exc
    return engine_class(config)


def available_engines() -> list[str]:
    return sorted([*_REGISTRY, "browser"])


__all__ = ["available_engines", "create_asr"]
