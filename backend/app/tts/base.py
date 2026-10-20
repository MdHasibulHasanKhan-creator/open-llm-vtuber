"""Provider agnostic TTS interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..config import TTSConfig


class TTSError(RuntimeError):
    """User actionable speech synthesis failure (shown in the UI)."""


@dataclass(slots=True)
class WordTiming:
    """When a word is spoken, relative to the start of the utterance."""

    offset: float
    duration: float
    text: str


@dataclass(slots=True)
class SynthesisResult:
    """Everything the pipeline needs to emit one spoken sentence."""

    #: Encoded audio bytes (``mp3`` / ``pcm_s16le``), or ``None`` when the
    #: client speaks the text itself (``browser`` engine).
    audio: bytes | None
    audio_format: str | None
    #: Sample rate of ``audio`` when it is raw PCM (0 = unknown / encoded).
    sample_rate: int
    #: Engine provided word timings (may be empty).
    word_timings: list[WordTiming] = field(default_factory=list)
    #: Used for lip-sync when there is no audio to analyse.
    estimated_duration: float = 0.0
    voice: str = ""

    @property
    def speaks_in_browser(self) -> bool:
        return not self.audio

    def __post_init__(self) -> None:
        if self.estimated_duration < 0:
            self.estimated_duration = 0.0
        if self.sample_rate < 0:
            self.sample_rate = 0


class TTSEngine(ABC):
    """Streaming-friendly speech synthesis backend."""

    name: str = "base"

    def __init__(self, config: TTSConfig) -> None:
        self.config = config

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        rate: str | None = None,
        pitch: str | None = None,
    ) -> SynthesisResult:
        """Synthesise ``text`` and return encoded audio.

        Must raise :class:`TTSError` with a human readable message on failure.
        """
        raise NotImplementedError

    async def list_voices(self) -> list[dict[str, str]]:
        """Optional: voices available for the settings UI."""
        return []

    def voice_for_language(self, language: str) -> str:
        """A voice of *this* engine that speaks ``language``, or ``""``.

        Only engines where the voice *is* the language need to answer (Edge -
        see :mod:`app.tts.voices`). OpenAI voices are styles and the browser
        picks its own from the locale we send, so both keep the configured one.
        """
        return ""

    async def health_check(self) -> tuple[bool, str]:
        return True, "ok"

    async def aclose(self) -> None:
        return None
