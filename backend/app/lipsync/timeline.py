"""Turn a phoneme sequence into a wall-clock viseme timeline.

The tricky part of lip-sync is not *which* shape to make but *when*. This module
solves it with a two stage alignment:

1. **Global scaling** - phonemes carry relative durations ("units"), and their
   total is stretched/squeezed over the real audio duration. Works with any TTS
   engine, even ones that report nothing.
2. **Word snapping** (optional) - engines that expose word timings (edge-tts)
   let us pin each group of phonemes inside the exact window of its word, which
   removes the accumulation drift of a purely proportional mapping.

Everything is expressed relative to the beginning of the audio that the client
is about to play, so the frontend can simply compare it with its audio clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

from .visemes import Phoneme, text_to_phonemes

#: Frames shorter than this are absorbed into their neighbour, otherwise the
#: mouth would flap faster than a human can perceive (~20 fps of visemes).
MIN_FRAME_SECONDS = 0.045


@dataclass(slots=True)
class WordBoundary:
    """A word and when it is spoken, as reported by the TTS engine."""

    offset: float  # seconds from the start of the utterance
    duration: float
    text: str


@dataclass(slots=True)
class VisemeFrame:
    start: float
    end: float
    viseme: str
    weight: float

    def as_list(self) -> list[float | str]:
        """Compact JSON form: ``[start, end, "aa", 0.85]``."""
        return [
            round(self.start, 3),
            round(self.end, 3),
            self.viseme,
            round(self.weight, 3),
        ]


# --------------------------------------------------------------------------- #
#  Allocation
# --------------------------------------------------------------------------- #
def _allocate(
    phonemes: list[Phoneme],
    start: float,
    end: float,
    gain: float,
) -> list[VisemeFrame]:
    """Spread ``phonemes`` proportionally across ``[start, end]``."""
    total_units = sum(p.units for p in phonemes)
    span = max(1e-3, end - start)
    if total_units <= 0:
        return []

    frames: list[VisemeFrame] = []
    cursor = start
    for phoneme in phonemes:
        length = span * (phoneme.units / total_units)
        weight = max(0.0, min(1.0, phoneme.loudness * gain))
        frames.append(VisemeFrame(cursor, cursor + length, phoneme.viseme, weight))
        cursor += length
    return frames


def _group_by_words(
    phonemes: list[Phoneme],
    words: list[WordBoundary],
    duration: float,
    gain: float,
) -> list[VisemeFrame]:
    """Assign phonemes to word windows by character share, then allocate."""
    usable = [w for w in words if w.text.strip() and w.duration > 0.0]
    if not usable or len(usable) > len(phonemes):
        return []

    lengths = [max(1, len(re.sub(r"\s+", "", w.text))) for w in usable]
    total_chars = float(sum(lengths))
    count = len(phonemes)
    frames: list[VisemeFrame] = []

    start_index = 0
    cumulative = 0.0
    for i, (word, char_len) in enumerate(zip(usable, lengths, strict=True)):
        cumulative += char_len
        # Inclusive index so every phoneme is used exactly once, and the last
        # word always swallows the remainder.
        end_index = count if i == len(usable) - 1 else max(start_index + 1, round(count * cumulative / total_chars))
        end_index = min(end_index, count)
        group = phonemes[start_index:end_index]
        start_index = end_index

        window_start = max(0.0, min(word.offset, duration))
        window_end = max(window_start, min(word.offset + word.duration, duration))
        if group and window_end > window_start:
            frames.extend(_allocate(group, window_start, window_end, gain))

    if start_index < count:  # leftover phonemes (rounding) -> tail of the audio
        frames.extend(_allocate(phonemes[start_index:], frames[-1].end if frames else 0.0, duration, gain))
    return frames


def _post_process(frames: list[VisemeFrame], duration: float) -> list[VisemeFrame]:
    """Clip, merge sub-perceptual frames and drop redundant collapses."""
    if not frames:
        return []

    cleaned: list[VisemeFrame] = []
    for frame in frames:
        frame.start = max(0.0, min(frame.start, duration))
        frame.end = max(0.0, min(frame.end, duration))
        if frame.end - frame.start < 1e-3:
            continue
        if cleaned and frame.viseme == cleaned[-1].viseme:
            cleaned[-1].end = frame.end
            cleaned[-1].weight = max(cleaned[-1].weight, frame.weight)
            continue
        cleaned.append(frame)

    merged: list[VisemeFrame] = []
    for frame in cleaned:
        if merged and frame.end - frame.start < MIN_FRAME_SECONDS:
            # Too short to read: extend the previous shape instead of flickering.
            merged[-1].end = frame.end
            continue
        merged.append(frame)

    if merged:
        merged[-1].end = max(merged[-1].end, duration)
    return merged


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #
def build_timeline(
    phonemes: list[Phoneme],
    duration: float,
    *,
    word_boundaries: list[WordBoundary] | None = None,
    offset: float = 0.0,
    gain: float = 1.0,
) -> list[VisemeFrame]:
    """Build the final viseme timeline for one utterance.

    ``offset`` shifts the whole timeline forward in time (used when the audio
    was trimmed at the front, so the mouth follows the audio, not the file).
    """
    if duration <= 0 or not phonemes:
        return []

    frames: list[VisemeFrame] = []
    if word_boundaries:
        frames = _group_by_words(phonemes, word_boundaries, duration, gain)
    if not frames:
        frames = _allocate(phonemes, 0.0, duration, gain)

    frames = _post_process(frames, duration)
    if offset:
        for frame in frames:
            frame.start += offset
            frame.end += offset
    return frames


def build_visemes(
    text: str,
    duration: float,
    *,
    language: str | None = None,
    word_boundaries: list[WordBoundary] | None = None,
    offset: float = 0.0,
    gain: float = 1.0,
    consonant_weight: float | None = None,
) -> list[VisemeFrame]:
    """Convenience wrapper: text + audio duration -> viseme timeline."""
    kwargs = {} if consonant_weight is None else {"consonant_weight": consonant_weight}
    phonemes = text_to_phonemes(text, language=language, **kwargs)
    frames = build_timeline(
        phonemes,
        duration,
        word_boundaries=word_boundaries,
        offset=offset,
        gain=gain,
    )
    logger.trace(
        "visemes for {!r} ({:.2f}s): {} frames, {} unique shapes",
        text[:40], duration, len(frames), len({f.viseme for f in frames}),
    )
    return frames


__all__ = [
    "MIN_FRAME_SECONDS",
    "VisemeFrame",
    "WordBoundary",
    "build_timeline",
    "build_visemes",
]
