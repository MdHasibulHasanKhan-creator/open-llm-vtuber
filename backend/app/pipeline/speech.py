"""Turn a :class:`SynthesisResult` into something the browser can play *and*
animate.

This is where the backend's lip-sync data is produced:

1. decode the engine output to mono float32 (unless it is already raw PCM)
2. trim leading/trailing silence, normalise the peak
3. measure a normalised RMS envelope (``volumes``, one value per ``slice_ms``)
4. build the viseme timeline, snapped to the engine's word boundaries when we
   have them (edge-tts does), otherwise distributed proportionally
5. re-encode/trim for transport

The result is fully self-describing: the client needs nothing but these fields
to play audio and move a face in perfect sync.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field

from loguru import logger

from ..config import LipsyncConfig, TTSConfig
from ..lipsync import VisemeFrame, build_visemes
from ..lipsync.timeline import WordBoundary
from ..tts import SynthesisResult
from ..utils.audio import (
    decode_audio,
    from_pcm16,
    peak_normalise,
    resample,
    rms_envelope,
    to_pcm16,
    trim_silence,
)

#: Anything shorter than this is a click or a stray "uh" - still sent, but we
#: do not bother analysing it.
_MIN_SPEECH_SECONDS = 0.18


@dataclass(slots=True)
class PreparedSpeech:
    """One spoken sentence, ready for the wire."""

    audio: bytes = b""
    #: ``pcm_s16le`` | ``mp3`` | ``browser``
    transport: str = "pcm_s16le"
    sample_rate: int = 24000
    duration: float = 0.0
    volumes: list[float] = field(default_factory=list)
    visemes: list[VisemeFrame] = field(default_factory=list)
    voice: str = ""
    text: str = ""
    #: Bytes trimmed from the front of the audio (diagnostics only).
    silence_trimmed: float = 0.0

    @property
    def viseme_payload(self) -> list[list[float | str]]:
        return [frame.as_list() for frame in self.visemes]

    def audio_chunks(self, chunk_ms: int) -> Iterator[bytes]:
        """Split the payload into WebSocket sized frames (PCM only)."""
        if self.transport != "pcm_s16le" or not self.audio:
            return
        samples_per_chunk = max(1, int(self.sample_rate * chunk_ms / 1000.0))
        bytes_per_chunk = samples_per_chunk * 2  # 16 bit mono
        for start in range(0, len(self.audio), bytes_per_chunk):
            yield self.audio[start : start + bytes_per_chunk]


def _prepare_sync(
    result: SynthesisResult,
    text: str,
    *,
    tts: TTSConfig,
    lipsync: LipsyncConfig,
    language: str | None,
    gain: float,
) -> PreparedSpeech:
    #: In `mouth` mode the client wants one number per slice, not mouth shapes,
    #: so the whole grapheme-to-viseme pass is skipped.
    want_visemes = lipsync.enabled and lipsync.mode == "visemes"

    # ------------------------------------------------------------------ #
    #  1. No server audio at all -> the client speaks it.
    # ------------------------------------------------------------------ #
    if result.speaks_in_browser:
        duration = max(result.estimated_duration, _MIN_SPEECH_SECONDS)
        frames = (
            build_visemes(
                text,
                duration,
                language=language,
                gain=lipsync.viseme_gain * gain,
            )
            if want_visemes
            else []
        )
        return PreparedSpeech(
            audio=b"",
            transport="browser",
            sample_rate=0,
            duration=duration,
            volumes=[],
            visemes=frames,
            voice=result.voice,
            text=text,
        )

    assert result.audio is not None

    # ------------------------------------------------------------------ #
    #  2. Get float samples.
    # ------------------------------------------------------------------ #
    if result.audio_format == "pcm_s16le" and result.sample_rate > 0:
        samples = from_pcm16(result.audio)
        sample_rate = result.sample_rate
        encoded = None
    else:
        decoded = decode_audio(result.audio, fallback_rate=tts.sample_rate or 24000)
        samples = decoded.samples
        sample_rate = decoded.sample_rate
        encoded = result.audio

    # ------------------------------------------------------------------ #
    #  3. Choose the transport.
    #     Raw PCM lets us trim + re-gain, and gives the client instant
    #     playable buffers. mp3 passthrough keeps the original bytes but must
    #     not be trimmed (its bytes and the timeline would drift apart).
    # ------------------------------------------------------------------ #
    transport = tts.format
    if transport == "mp3" and encoded is None:
        logger.debug("requested mp3 transport but the engine produced raw PCM")
        transport = "pcm_s16le"

    trimmed_seconds = 0.0
    if transport == "pcm_s16le":
        samples, trimmed_seconds = trim_silence(samples, sample_rate)
        if not lipsync.enabled:
            gain = 1.0
        samples = peak_normalise(samples)

    duration = float(len(samples)) / float(sample_rate) if sample_rate else 0.0
    if duration < _MIN_SPEECH_SECONDS:
        logger.debug("skipping {:.3f}s of audio (too short)", duration)

    # ------------------------------------------------------------------ #
    #  4. Lip-sync data (always computed on the audio we are about to send).
    # ------------------------------------------------------------------ #
    volumes: list[float] = []
    visemes: list[VisemeFrame] = []
    if lipsync.enabled and duration > 0:
        volumes = rms_envelope(samples, sample_rate, lipsync.slice_ms)
        if want_visemes:
            # Word boundaries are relative to the *untrimmed* audio.
            boundaries = [
                WordBoundary(word.offset - trimmed_seconds, word.duration, word.text)
                for word in result.word_timings
                if word.offset + word.duration > trimmed_seconds
            ]
            visemes = build_visemes(
                text,
                duration,
                language=language,
                word_boundaries=boundaries or None,
                gain=lipsync.viseme_gain * gain,
            )

    # ------------------------------------------------------------------ #
    #  5. Encode for the wire.
    # ------------------------------------------------------------------ #
    if transport == "pcm_s16le":
        if tts.sample_rate and tts.sample_rate != sample_rate:
            samples = resample(samples, sample_rate, tts.sample_rate)
            sample_rate = tts.sample_rate
        payload = to_pcm16(samples)
    else:
        payload = encoded or b""

    logger.debug(
        "prepared speech: {:.2f}s, {} bytes as {}, {} volumes, {} visemes "
        "(trimmed {:.3f}s of silence)",
        duration, len(payload), transport, len(volumes), len(visemes), trimmed_seconds,
    )

    return PreparedSpeech(
        audio=payload,
        transport=transport,
        sample_rate=sample_rate,
        duration=duration,
        volumes=volumes,
        visemes=visemes,
        voice=result.voice,
        text=text,
        silence_trimmed=trimmed_seconds,
    )


async def prepare_speech(
    result: SynthesisResult,
    text: str,
    *,
    tts: TTSConfig,
    lipsync: LipsyncConfig,
    language: str | None = None,
    gain: float = 1.0,
) -> PreparedSpeech:
    """Async wrapper - decoding is CPU bound, keep it off the event loop."""
    return await asyncio.to_thread(
        _prepare_sync,
        result,
        text,
        tts=tts,
        lipsync=lipsync,
        language=language,
        gain=gain,
    )


__all__ = ["PreparedSpeech", "prepare_speech"]
