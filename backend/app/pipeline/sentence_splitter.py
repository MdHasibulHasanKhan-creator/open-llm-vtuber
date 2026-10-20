"""Incremental sentence segmentation of a token stream.

Why this exists: to keep perceived latency low we must start synthesising
speech *before* the LLM has finished writing. Splitting the stream into
sentences is what makes the first audio play within a few hundred milliseconds
instead of after the whole (possibly multi-second) reply.

Handles the annoying cases correctly:

* abbreviations (``Mr.``, ``e.g.``, ``etc.``) do not end a sentence
* decimals (``3.14``) and ellipsis (``...``) do not end a sentence
* newlines and CJK terminators (``。``, ``！``, ``？``) do end a sentence
* very long sentences are broken at a comma / space so TTS is never starved
* quotes/brackets after a terminator (``He said "stop."``) are kept together

Usage::

    splitter = SentenceSplitter()
    for token in stream:
        for sentence in splitter.push(token):
            await speak(sentence)
    for sentence in splitter.flush():
        await speak(sentence)
"""

from __future__ import annotations

import re

HARD_TERMINATORS = ".!?\u2026\u3002\uff01\uff1f\n"
SOFT_TERMINATORS = ",;:\uff0c\uff1b\uff1a\u3001"

#: Lowercase, dot-stripped words that never end a sentence.
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "inc",
    "ltd", "co", "no", "vol", "fig", "approx", "dept", "est", "min", "max",
    "e.g", "i.e", "a.m", "p.m", "u.s", "u.k", "ph.d", "don't",
}

_TRAILING_WRAPPERS = "\"')]}\u300d\u300f\uff09\u3009\u300b"
_LEADING_WRAPPERS = "\"'(（「『"

_SHORT_WORD_RE = re.compile(r"([A-Za-z][A-Za-z.]{0,6}|[\u4e00-\u9fff])\s*$")


class SentenceSplitter:
    """Feed it tokens, get back complete sentences."""

    def __init__(
        self,
        *,
        min_chars: int = 4,
        soft_max_chars: int = 90,
        max_chars: int = 260,
    ) -> None:
        self.min_chars = min_chars
        self.soft_max_chars = soft_max_chars
        self.max_chars = max_chars

        self._buffer = ""
        #: True while we are waiting for one more character to decide whether a
        #: "." was a real sentence end (abbreviation / decimal lookahead).
        self._await_lookahead = False

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #
    def push(self, token: str) -> list[str]:
        """Add a chunk of streamed text; return every sentence now complete."""
        if not token:
            return []

        sentences: list[str] = []

        if self._await_lookahead:
            self._await_lookahead = False
            # The very first character of the new token tells us whether the dot
            # we kept was a sentence end ("Hello. Yes" vs "e.g. foo" / "3.14").
            first = token.lstrip()[:1]
            if first and (first.islower() or first.isdigit() or first in "_/,"):
                pass  # not a boundary - keep accumulating
            else:
                sentence = self._emit_up_to(len(self._buffer))
                if sentence:
                    sentences.append(sentence)

        self._buffer += token

        while True:
            boundary = self._find_hard_boundary()
            if boundary is None:
                break
            position, needs_lookahead = boundary
            if needs_lookahead:
                # Wait for one more token before committing (streaming).
                self._await_lookahead = True
                break
            sentence = self._emit_up_to(position)
            if sentence:
                sentences.append(sentence)

        # Soft split: a long sentence still waiting for its full stop.
        while len(self._buffer) >= self.soft_max_chars:
            split_at = self._find_soft_boundary()
            if split_at is None:
                break
            sentence = self._emit_up_to(split_at)
            if sentence:
                sentences.append(sentence)

        return sentences

    def flush(self) -> list[str]:
        """Return whatever is left (end of stream).

        The ``min_chars`` filter is bypassed here: a leftover short tail is
        still something the user must be able to hear.
        """
        remaining = self._buffer
        self._buffer = ""
        self._await_lookahead = False
        cleaned = self._clean(remaining, force=True)
        return [cleaned] if cleaned else []

    @property
    def pending(self) -> str:
        """Text currently held back (useful for UI previews)."""
        return self._buffer

    # ------------------------------------------------------------------ #
    #  Internals
    # ------------------------------------------------------------------ #
    def _emit_up_to(self, position: int) -> str:
        """Cut the buffer at ``position`` and return the cleaned sentence."""
        sentence = self._clean(self._buffer[:position])
        self._buffer = self._buffer[position:].lstrip()
        return sentence

    def _clean(self, text: str, *, force: bool = False) -> str:
        text = text.strip()
        # Drop leading list markers ("1. ", "- ") the model may have emitted.
        text = re.sub(r"^(?:\d+[.)]|[-*+])\s+", "", text)
        if not text:
            return ""
        if force:
            return text
        # Anything that ends on a terminator is a complete sentence, however
        # short ("Hi!"). Fragments without one are held back.
        if len(text) < self.min_chars and text[-1] not in HARD_TERMINATORS:
            return ""
        return text

    def _find_hard_boundary(self) -> tuple[int, bool] | None:
        """First real sentence end in the buffer.

        Returns ``(cut_index, needs_lookahead)``: ``cut_index`` is *after* the
        terminator (plus closing quotes), ``needs_lookahead`` asks the caller to
        wait for the next character before committing.
        """
        buffer = self._buffer
        for index, char in enumerate(buffer):
            if char not in HARD_TERMINATORS:
                continue

            if char == ".":
                if self._is_abbreviation(buffer, index) or self._is_decimal(buffer, index):
                    continue
                # "..." (ellipsis) is a pause, not a sentence end.
                if index + 1 < len(buffer) and buffer[index + 1] == ".":
                    continue
                if index >= 1 and buffer[index - 1] == ".":
                    continue

            end = index + 1
            # Keep trailing quotes / brackets with the sentence.
            while end < len(buffer) and buffer[end] in _TRAILING_WRAPPERS:
                end += 1

            if end >= len(buffer):
                # Nothing after the terminator yet: ask for one more token so
                # "Mr." / "3." can be detected.
                return (end, True) if len(buffer[:index].strip()) >= self.min_chars else None
            return end, False

        return None

    def _find_soft_boundary(self) -> int | None:
        """Break a long sentence at the last clause boundary we have.

        Preference order: a comma / semicolon anywhere in the buffer (natural
        speech pause) -> the last space (never split inside a word) -> a hard
        cut at ``max_chars`` as the very last resort.
        """
        buffer = self._buffer
        candidate = -1
        for index, char in enumerate(buffer):
            if index >= self.min_chars and char in SOFT_TERMINATORS:
                candidate = index + 1
        if candidate > 0:
            return candidate

        window_start = max(self.min_chars, len(buffer) - 70)
        space = buffer.rfind(" ", window_start)
        if space > 0:
            return space + 1

        if len(buffer) >= self.max_chars:
            return self.max_chars
        return None

    @staticmethod
    def _is_abbreviation(buffer: str, dot_index: int) -> bool:
        match = _SHORT_WORD_RE.search(buffer[:dot_index])
        if not match:
            return False
        token = match.group(1).lower().rstrip(".")
        if not token:
            return True
        if token in ABBREVIATIONS:
            return True
        # Single letter + dot = an initial ("J. K. Rowling").
        return len(token) == 1 and token.isalpha()

    @staticmethod
    def _is_decimal(buffer: str, dot_index: int) -> bool:
        before = buffer[dot_index - 1 : dot_index]
        after = buffer[dot_index + 1 : dot_index + 2]
        return before.isdigit() and after.isdigit()


def split_sentences(text: str) -> list[str]:
    """Non-streaming helper (used by tests)."""
    splitter = SentenceSplitter()
    sentences = splitter.push(text)
    sentences.extend(splitter.flush())
    return sentences


__all__ = ["SentenceSplitter", "split_sentences"]
