"""Lip-sync unit tests: phoneme extraction, timelines and audio analysis."""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from app.lipsync import build_visemes, text_to_phonemes
from app.lipsync.timeline import MIN_FRAME_SECONDS, WordBoundary, build_timeline
from app.lipsync.visemes import CLOSED, kana_to_romaji
from app.utils.audio import rms_envelope, to_pcm16, trim_silence


# --------------------------------------------------------------------------- #
#  Phonemes
# --------------------------------------------------------------------------- #
def test_vowels_map_to_vrm_presets():
    phonemes = text_to_phonemes("a i u e o", include_consonants=False)
    assert [p.viseme for p in phonemes] == ["aa", "ih", "ou", "ee", "oh"]


def test_consonants_close_the_mouth():
    phonemes = text_to_phonemes("mama", consonant_weight=0.4)
    assert phonemes[0].viseme != CLOSED  # never starts closed
    assert {p.viseme for p in phonemes} == {"aa", CLOSED}


def test_digraph_diphthongs_produce_two_frames():
    assert [p.viseme for p in text_to_phonemes("see you", include_consonants=False)] == ["ih", "aa", "ou"]
    assert [p.viseme for p in text_to_phonemes("rain", include_consonants=False)] == ["ee", "ih"]


def test_kana_transliteration_handles_youon_and_sokuon():
    assert kana_to_romaji("きゃ") == "kya"
    assert kana_to_romaji("しゃしん") == "shashin"
    assert kana_to_romaji("がっこう") == "gakkou"
    assert kana_to_romaji("ラーメン") == "raamen"


def test_japanese_sentence_lip_sync():
    frames = build_visemes("こんにちは、元気ですか？", 2.4, language="ja")
    assert frames
    assert frames[0].start == pytest.approx(0.0, abs=0.05)
    assert frames[-1].end == pytest.approx(2.4, abs=0.2)
    assert all(f.end > f.start for f in frames)


# --------------------------------------------------------------------------- #
#  Timeline
# --------------------------------------------------------------------------- #
def test_timeline_is_contiguous_and_within_duration():
    frames = build_visemes("Hello there, how are you doing today?", 3.0)
    assert frames
    assert frames[0].start >= 0.0
    assert frames[-1].end <= 3.0 + 1e-6
    for previous, current in itertools.pairwise(frames):
        assert current.start >= previous.start
        assert previous.end <= current.start + 0.2  # small overlaps are fine


def test_short_frames_are_merged():
    """No flicker: every frame must be at least MIN_FRAME_SECONDS long."""
    frames = build_visemes("supercalifragilisticexpialidocious", 1.2)
    assert all(f.end - f.start >= MIN_FRAME_SECONDS - 1e-6 for f in frames)


def test_word_boundaries_pin_the_timeline():
    phonemes = text_to_phonemes("hello world", consonant_weight=0.3)
    boundaries = [
        WordBoundary(offset=0.0, duration=0.5, text="hello"),
        WordBoundary(offset=0.6, duration=0.4, text="world"),
    ]
    frames = build_timeline(phonemes, 1.05, word_boundaries=boundaries)
    assert frames
    # The first frame must land inside the first word's window.
    assert frames[0].start < 0.5
    # Some frame must live inside the second word's window.
    assert any(0.6 <= f.start < 1.0 for f in frames)


def test_zero_duration_is_safe():
    assert build_visemes("hello", 0.0) == []


# --------------------------------------------------------------------------- #
#  Audio helpers
# --------------------------------------------------------------------------- #
def test_rms_envelope_is_normalised():
    sample_rate = 16000
    tone = 0.5 * np.sin(2 * np.pi * 220 * np.arange(sample_rate) / sample_rate).astype(np.float32)
    envelope = rms_envelope(tone, sample_rate, slice_ms=20)
    assert len(envelope) == pytest.approx(sample_rate / (sample_rate * 0.02), rel=0.02)
    assert max(envelope) == pytest.approx(1.0, abs=0.01)
    assert all(0.0 <= value <= 1.0 for value in envelope)


def test_trim_silence_reports_offset():
    sample_rate = 16000
    speech = (0.4 * np.random.default_rng(0).standard_normal(sample_rate // 2)).astype(np.float32)
    padded = np.concatenate(
        [np.zeros(sample_rate // 4, np.float32), speech, np.zeros(sample_rate // 4, np.float32)]
    )
    trimmed, offset = trim_silence(padded, sample_rate)
    assert offset > 0.2
    assert len(trimmed) < len(padded)


def test_pcm_roundtrip():
    samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
    payload = to_pcm16(samples)
    restored = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    assert np.allclose(samples, restored, atol=1e-3)
