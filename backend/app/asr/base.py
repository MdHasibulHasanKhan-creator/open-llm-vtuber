"""Provider agnostic speech recognition interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import ASRConfig
from ..utils.audio import DecodedAudio


class ASRError(RuntimeError):
    """User actionable transcription failure (shown in the UI)."""


class ASREngine(ABC):
    """Turns a recorded utterance into text."""

    name: str = "base"

    def __init__(self, config: ASRConfig) -> None:
        self.config = config

    @abstractmethod
    async def transcribe(self, audio: DecodedAudio, *, language: str | None = None) -> str:
        """Transcribe mono float32 audio. Raise :class:`ASRError` on failure."""
        raise NotImplementedError

    async def health_check(self) -> tuple[bool, str]:
        return True, "ok"

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ #
    #  Helpers shared by implementations
    # ------------------------------------------------------------------ #
    def _too_short(self, audio: DecodedAudio) -> bool:
        return audio.duration < self.config.min_speech_seconds
