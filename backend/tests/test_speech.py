"""Speech preparation tests: the bridge between TTS output and lip-sync data."""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from app.config import LipsyncConfig, TTSConfig
from app.pipeline.speech import prepare_speech
from app.tts.base import SynthesisResult, WordTiming
from app.utils.audio import to_pcm16

SAMPLE_RATE = 24000


def _spoken_audio(duration: float = 2.4, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """A "voice": a tone whose amplitude pulses like syllables."""
    t = np.arange(int(duration * sample_rate)) / sample_rate
    carrier = np.sin(2 * np.pi * 180 * t)
    syllable = 0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * t)  # 3.5 syllables / second
    return (0.6 * syllable * carrier).astype(np.float32)


def _result(duration: float = 2.4, *, boundaries: bool = True) -> SynthesisResult:
    samples = _spoken_audio(duration)
    timings = (
        [
            WordTiming(0.10, 0.45, "hello"),
            WordTiming(0.70, 0.55, "there"),
            WordTiming(1.40, 0.70, "friend"),
        ]
        if boundaries
        else []
    )
    return SynthesisResult(
        audio=to_pcm16(samples),
        audio_format="pcm_s16le",
        sample_rate=SAMPLE_RATE,
        word_timings=timings,
        estimated_duration=duration,
        voice="test-voice",
    )


async def test_prepare_speech_defaults_to_mouth_mode():
    """The default mode ships an envelope and *no* viseme timeline.

    All the client needs to open one blendshape is one number per slice, so the
    whole grapheme-to-viseme pass is skipped.
    """
    prepared = await prepare_speech(
        _result(),
        "Hello there, friend!",
        tts=TTSConfig(format="pcm_s16le", sample_rate=SAMPLE_RATE, chunk_ms=200),
        lipsync=LipsyncConfig(),
        language="en",
    )

    assert prepared.transport == "pcm_s16le"
    assert prepared.sample_rate == SAMPLE_RATE
    assert 0.5 < prepared.duration <= 2.4
    assert prepared.volumes, "an RMS envelope must be provided for amplitude modulation"
    assert all(0.0 <= v <= 1.0 for v in prepared.volumes)
    assert prepared.visemes == [], "mouth mode does not build a viseme timeline"
    assert prepared.voice == "test-voice"


async def test_viseme_mode_still_produces_a_timeline():
    prepared = await prepare_speech(
        _result(),
        "Hello there, friend!",
        tts=TTSConfig(format="pcm_s16le", sample_rate=SAMPLE_RATE, chunk_ms=200),
        lipsync=LipsyncConfig(mode="visemes"),
        language="en",
    )
    assert prepared.volumes
    assert prepared.visemes, "a viseme timeline must be provided in visemes mode"


async def test_audio_chunks_cover_the_whole_payload():
    prepared = await prepare_speech(
        _result(),
        "Hello there, friend!",
        tts=TTSConfig(format="pcm_s16le", sample_rate=SAMPLE_RATE, chunk_ms=200),
        lipsync=LipsyncConfig(),
    )
    chunks = list(prepared.audio_chunks(200))
    assert len(chunks) > 1
    # 200 ms of 24 kHz mono 16 bit
    assert all(len(chunk) <= 200 * SAMPLE_RATE // 1000 * 2 for chunk in chunks)
    assert sum(len(chunk) for chunk in chunks) == len(prepared.audio)


async def test_viseme_timeline_matches_the_delivered_audio():
    prepared = await prepare_speech(
        _result(),
        "Hello there, friend!",
        tts=TTSConfig(format="pcm_s16le", sample_rate=SAMPLE_RATE),
        lipsync=LipsyncConfig(mode="visemes"),
    )
    assert prepared.visemes
    # Word snapping pins the first shape to the first word boundary (0.10 s in
    # this fixture) instead of blindly starting at zero.
    assert 0.0 <= prepared.visemes[0].start <= 0.2
    assert prepared.visemes[-1].end == pytest.approx(prepared.duration, abs=0.06)
    # Monotonic, non-overlapping-ish, and every frame is visible.
    for previous, current in itertools.pairwise(prepared.visemes):
        assert current.start >= previous.start
        assert previous.end - previous.start > 0.02


async def test_browser_engine_needs_no_audio():
    result = SynthesisResult(
        audio=None,
        audio_format=None,
        sample_rate=0,
        word_timings=[],
        estimated_duration=1.8,
        voice="browser",
    )
    prepared = await prepare_speech(
        result,
        "The browser will speak this sentence.",
        tts=TTSConfig(engine="browser"),
        lipsync=LipsyncConfig(mode="visemes"),
    )
    assert prepared.transport == "browser"
    assert prepared.audio == b""
    assert prepared.duration == pytest.approx(1.8, abs=0.01)
    assert prepared.visemes, "the browser path still needs visemes in visemes mode"
    assert list(prepared.audio_chunks(200)) == []


async def test_browser_engine_in_mouth_mode_ships_nothing_to_animate():
    """The client drives browser-speech mouths from a synthetic envelope, so the
    server has nothing useful to send."""
    result = SynthesisResult(
        audio=None,
        audio_format=None,
        sample_rate=0,
        word_timings=[],
        estimated_duration=1.8,
        voice="browser",
    )
    prepared = await prepare_speech(
        result,
        "The browser will speak this sentence.",
        tts=TTSConfig(engine="browser"),
        lipsync=LipsyncConfig(),
    )
    assert prepared.transport == "browser"
    assert prepared.visemes == []
    assert prepared.volumes == []


async def test_word_boundaries_shift_with_silence_trimming():
    """Trimming the TTS lead-in must move the timeline with the audio."""
    padded = np.concatenate(
        [np.zeros(SAMPLE_RATE // 2, np.float32), _spoken_audio(1.5)]  # 0.5 s of silence
    )
    result = SynthesisResult(
        audio=to_pcm16(padded),
        audio_format="pcm_s16le",
        sample_rate=SAMPLE_RATE,
        word_timings=[WordTiming(0.55, 0.40, "hello")],
        estimated_duration=2.0,
    )
    prepared = await prepare_speech(
        result,
        "Hello",
        tts=TTSConfig(format="pcm_s16le", sample_rate=SAMPLE_RATE),
        lipsync=LipsyncConfig(mode="visemes"),
    )
    assert prepared.silence_trimmed > 0.3
    # The word is now ~0.05 s into the delivered audio, not 0.55 s.
    assert prepared.visemes[0].start < 0.2


async def test_lipsync_can_be_disabled():
    prepared = await prepare_speech(
        _result(),
        "Hello there!",
        tts=TTSConfig(format="pcm_s16le", sample_rate=SAMPLE_RATE),
        lipsync=LipsyncConfig(enabled=False),
    )
    assert prepared.visemes == []
    assert prepared.volumes == []
    assert prepared.audio, "audio is still delivered when lip-sync is off"
