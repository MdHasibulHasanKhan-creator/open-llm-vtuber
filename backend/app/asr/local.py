"""Local, offline speech recognition with ``faster-whisper``.

* The model is loaded lazily on the first utterance and kept in memory.
* Loading and decoding happen in worker threads, so the event loop (and the
  3D client's WebSocket) never stalls.
* A lock serialises transcriptions: Whisper is not re-entrant, and two parallel
  decodes on one GPU would only make things slower.

Install with ``pip install -r requirements-local.txt`` (this engine is imported
lazily, so the project also runs without it).
"""

from __future__ import annotations

import asyncio

import numpy as np
from loguru import logger

from ..config import BACKEND_DIR, ASRConfig
from ..utils.audio import DecodedAudio
from .base import ASREngine, ASRError

#: Models are cached inside the project (git-ignored) instead of ~/.cache.
MODEL_DIR = BACKEND_DIR / "models"


class FasterWhisperASR(ASREngine):
    name = "local"

    def __init__(self, config: ASRConfig) -> None:
        super().__init__(config)
        self._model = None
        self._load_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  Model lifecycle
    # ------------------------------------------------------------------ #
    def _load_model(self):
        from faster_whisper import WhisperModel  # imported lazily: heavy

        device = (self.config.device or "auto").lower()
        compute_type = self.config.compute_type or "int8"
        if device == "cpu" and compute_type in ("float16", "int8_float16"):
            logger.warning("compute_type {} is not usable on CPU, using int8", compute_type)
            compute_type = "int8"

        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(
            "loading faster-whisper model '{}' (device={}, compute_type={}) - "
            "the first run downloads it",
            self.config.model, device, compute_type,
        )
        return WhisperModel(
            self.config.model,
            device=device,
            compute_type=compute_type,
            download_root=str(MODEL_DIR),
        )

    async def _ensure_model(self):
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                try:
                    self._model = await asyncio.to_thread(self._load_model)
                except ImportError as exc:
                    raise ASRError(
                        "faster-whisper is not installed. Run "
                        "`pip install -r requirements-local.txt`, or set "
                        "asr.engine to 'openai' / 'browser'."
                    ) from exc
                except Exception as exc:
                    raise ASRError(
                        f"Could not load the Whisper model '{self.config.model}': {exc}. "
                        "Try another model name (tiny.en, base.en, small, medium, large-v3) "
                        "or a smaller compute_type."
                    ) from exc
        return self._model

    # ------------------------------------------------------------------ #
    #  Transcription
    # ------------------------------------------------------------------ #
    def _transcribe_sync(self, samples: np.ndarray, language: str | None) -> str:
        assert self._model is not None
        segments, info = self._model.transcribe(
            samples.astype(np.float32),
            language=language,
            beam_size=max(1, self.config.beam_size),
            # Built-in VAD: trims silence and, importantly, prevents Whisper
            # from hallucinating text out of background noise.
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 120},
            initial_prompt=self.config.prompt or None,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        logger.debug(
            "whisper: {:.2f}s audio, lang={} (p={:.2f}) -> {!r}",
            getattr(info, "duration", 0.0), getattr(info, "language", "?"),
            getattr(info, "language_probability", 0.0), text,
        )
        return text

    async def transcribe(self, audio: DecodedAudio, *, language: str | None = None) -> str:
        if self._too_short(audio):
            logger.trace("skipping {:.2f}s utterance (too short)", audio.duration)
            return ""

        await self._ensure_model()

        target_language = (language or self.config.language or "").strip() or None
        if target_language and target_language.lower() in ("auto", "none"):
            target_language = None

        async with self._run_lock:
            try:
                text = await asyncio.to_thread(self._transcribe_sync, audio.samples, target_language)
            except Exception as exc:
                raise ASRError(f"Whisper failed: {exc}") from exc
        return text

    async def health_check(self) -> tuple[bool, str]:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False, "faster-whisper is not installed"
        # A full model load takes seconds: do not do it during startup.
        return True, f"ready (model '{self.config.model}' loads on first use)"

    async def aclose(self) -> None:
        self._model = None


__all__ = ["MODEL_DIR", "FasterWhisperASR"]
