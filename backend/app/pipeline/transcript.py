"""Input-side transcript gate - keep noise out of the conversation.

The microphone does not only carry the user's voice. With speakers on it also
carries the avatar's *own* speech; in a quiet room it carries room tone that a
Whisper-style model happily transcribes as "Thank you for watching."; on a stuck
VAD it carries the same sentence twice. All three look like a user turn to the
rest of the pipeline, and all three are why a companion appears to talk to
itself or answer questions nobody asked. Fixing the *reply* (see
:mod:`app.pipeline.quality`) catches what the model says back; this module stops
the garbage getting in.

The gate is deliberately conservative, because dropping a real utterance is a
worse failure than accepting noise:

* nothing at all, or punctuation / sound tags only;
* a known ASR hallucination - exactly, or with a word or two of padding;
* a *plausible* short reply ("thank you", "you") only when the audio it came
  from was too short to have contained it (see ``short_utterance_seconds``);
* a near-copy of something the avatar just said - the speaker-echo loop;
* optionally, the same transcript twice in a row (off by default: people do
  legitimately repeat themselves when they think they were not heard).

Similarity is :func:`difflib.SequenceMatcher.ratio` over a punctuation-stripped,
case-folded form - no extra dependency, and identical behaviour for Bengali,
Japanese and English, because both are compared in the same normalised space.

Everything is driven by :class:`app.config.TranscriptConfig`; tunable per
session from the settings UI, and switchable off entirely.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..config import TranscriptConfig

__all__ = [
    "HALLUCINATIONS_HARD",
    "HALLUCINATIONS_SHORT",
    "REASON_LABELS",
    "TranscriptVerdict",
    "compare_key",
    "evaluate",
    "normalise",
    "similarity",
]

#: Phrases that are never a plausible thing to say to an avatar: video sign-offs,
#: subtitle credits, non-speech tags and the classic Whisper artefacts. Dropped
#: regardless of how long the audio was. Compared in :func:`compare_key` space
#: (case-folded, punctuation-free), which :data:`HALLUCINATIONS_HARD` holds.
_HARD_PHRASES: tuple[str, ...] = (
    "thanks for watching",
    "thank you for watching",
    "thank you for watching this video",
    "please subscribe",
    "subscribe to my channel",
    "like and subscribe",
    "see you next time",
    "subtitles by",
    "subtitles by the amaraorg community",
    "amaraorg",
    "transcription by",
    "subtitle",
    "字幕",
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございます",
)

#: Phrases that *are* plausible replies, but that a Whisper-style model also
#: emits from near-silence. Only dropped when the utterance was too short to
#: have contained the words (see ``TranscriptConfig.short_utterance_seconds``).
_SHORT_PHRASES: tuple[str, ...] = (
    "you",
    "thank you",
    "thanks",
    "bye bye",
    "music",
    "applause",
    "silence",
    "laughter",
    "www",
    "com",
    "you you",
    "thank you thank you",
)

#: ``[music]`` / ``(applause)`` / ``<noise>`` / ``*sigh*`` groups.
_NON_SPEECH_GROUP_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|<[^>]*>|\*[^*]*\*")

_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)

#: Unicode categories that make up a "word" character: letters, *marks* (the
#: matras and harakat that Python's ``\w`` throws away - which would gut Bengali
#: and Devanagari), and numbers.
_WORD_CATEGORIES = frozenset("LMN")


@dataclass(frozen=True, slots=True)
class TranscriptVerdict:
    """What the gate decided about one transcript.

    ``reason`` is a stable machine code (also sent to the client so the UI can
    explain the silence): ``""`` when accepted, otherwise one of ``empty``,
    ``tag``, ``noise``, ``short``, ``echo``, ``duplicate``.
    """

    accepted: bool
    text: str
    reason: str = ""
    score: float = 0.0

    def __bool__(self) -> bool:  # `if verdict:` reads better than `.accepted`
        return self.accepted


# --------------------------------------------------------------------------- #
#  Text helpers
# --------------------------------------------------------------------------- #
def normalise(text: str) -> str:
    """Compatibility-normalised, whitespace-collapsed form (still readable)."""
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", text or "")).strip()


def _has_word(text: str) -> bool:
    return any(unicodedata.category(ch)[0] in _WORD_CATEGORIES for ch in text)


def compare_key(text: str) -> str:
    """Case-folded, punctuation-free form used for every comparison."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    kept = [
        ch if unicodedata.category(ch)[0] in _WORD_CATEGORIES else " "
        for ch in folded
    ]
    return _WHITESPACE_RE.sub(" ", "".join(kept)).strip()


def _is_nonspeech_only(text: str) -> bool:
    """``[music]`` and friends: nothing but tagged non-speech groups."""
    return not _has_word(_NON_SPEECH_GROUP_RE.sub("", text))


def similarity(left: str, right: str) -> float:
    """0..1 similarity of two transcripts, ignoring case and punctuation."""
    a, b = compare_key(left), compare_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _matches(key: str, phrases: frozenset[str]) -> bool:
    """Exact match, or the phrase plus at most two words of padding."""
    if not key:
        return False
    if key in phrases:
        return True
    tokens = key.split()
    joined, glued = " ".join(tokens), "".join(tokens)
    for phrase in phrases:
        words = phrase.split()
        if len(words) <= 1 or len(tokens) > len(words) + 2:
            continue
        # `joined` matches "thanks for watching, everyone"; `glued` matches
        # "subtitles by the Amara.org community" (punctuation that was inside a
        # word loses its space when the key is built).
        if phrase in joined or phrase.replace(" ", "") in glued:
            return True
    return False


#: Built once, in comparison space, after ``compare_key`` is available.
HALLUCINATIONS_HARD: frozenset[str] = frozenset(compare_key(p) for p in _HARD_PHRASES)
HALLUCINATIONS_SHORT: frozenset[str] = frozenset(compare_key(p) for p in _SHORT_PHRASES)


# --------------------------------------------------------------------------- #
#  The gate
# --------------------------------------------------------------------------- #
def evaluate(
    text: str,
    config: TranscriptConfig,
    *,
    duration: float | None = None,
    recent_assistant: Sequence[str] = (),
    recent_user: Sequence[str] = (),
) -> TranscriptVerdict:
    """Decide whether ``text`` deserves a turn.

    ``duration`` is the length of the audio it came from, when known (server-side
    ASR always knows; the browser's Web Speech API does not, and browser
    recognition does not hallucinate on silence the way Whisper does, so the
    short-phrase tier is simply skipped for it).
    """
    cleaned = normalise(text)
    if not config.enabled:
        return TranscriptVerdict(bool(cleaned), cleaned)

    key = compare_key(cleaned)
    if not key or not _has_word(cleaned):
        return TranscriptVerdict(False, cleaned, "empty")
    if _is_nonspeech_only(cleaned):
        return TranscriptVerdict(False, cleaned, "tag")
    if len(cleaned) < max(1, config.min_chars):
        return TranscriptVerdict(False, cleaned, "short")

    noise = HALLUCINATIONS_HARD | {compare_key(phrase) for phrase in config.extra_noise_phrases}
    if _matches(key, noise):
        return TranscriptVerdict(False, cleaned, "noise")

    if (
        duration is not None
        and 0.0 < duration < config.short_utterance_seconds
        and _matches(key, HALLUCINATIONS_SHORT)
    ):
        return TranscriptVerdict(False, cleaned, "noise")

    if config.echo_guard:
        for line in list(recent_assistant)[: max(0, config.echo_window)]:
            score = similarity(cleaned, line)
            if score >= config.echo_similarity:
                return TranscriptVerdict(False, cleaned, "echo", score)

    if config.dedupe:
        for line in list(recent_user)[:1]:
            score = similarity(cleaned, line)
            if score >= config.dedupe_similarity:
                return TranscriptVerdict(False, cleaned, "duplicate", score)

    return TranscriptVerdict(True, cleaned)


#: Human-readable explanations for every reason the gate can return.
#: The wire carries the *code* (the client keeps its own copy of these labels and
#: falls back to the code itself if an unknown one ever appears); this mapping is
#: the server-side source of truth, and a test asserts it stays complete.
REASON_LABELS: dict[str, str] = {
    "empty": "didn't catch that",
    "tag": "didn't catch that",
    "short": "didn't catch that",
    "noise": "ignored background noise",
    "echo": "ignored my own voice",
    "duplicate": "ignored a repeat",
}
