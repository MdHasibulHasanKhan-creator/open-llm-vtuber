"""Text scrubbing helpers shared by the LLM, TTS and emotion layers."""

from __future__ import annotations

import re

# Markdown / formatting noise an LLM likes to emit but TTS should never speak.
_MD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"```.*?```", re.DOTALL), " "),          # fenced code
    (re.compile(r"`([^`]*)`"), r"\1"),                    # inline code
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),                # bold
    (re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)"), r"\1"),  # italics
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), r"\1"),      # links / images
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE), ""),  # headings
    (re.compile(r"^\s{0,3}>\s?", re.MULTILINE), ""),       # blockquote
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),       # bullets
    (re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE), ""),     # numbered lists
    (re.compile(r"\|"), " "),                              # tables
    (re.compile(r"[\u200b\u200d\ufeff]"), ""),             # zero width junk
)

#: Emoji + pictographs. Voice models read them out loud ("smiling face"), so
#: they are removed from the spoken text but kept in the chat bubble.
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U0001F000-\U0001F2FF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U0000FE0F"
    "]",
    flags=re.UNICODE,
)

_WS = re.compile(r"[ \t\u3000]+")
_MULTI_NL = re.compile(r"\n{2,}")


def strip_markdown(text: str) -> str:
    out = text
    for pattern, repl in _MD_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def strip_emoji(text: str) -> str:
    return _EMOJI.sub("", text)


def normalise_for_speech(text: str, *, keep_newlines: bool = False) -> str:
    """Prepare a chunk of LLM output for the TTS engine."""
    out = strip_emoji(strip_markdown(text))
    out = out.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    out = out.replace("\u2014", ", ").replace("\u2013", ", ")
    if not keep_newlines:
        out = out.replace("\n", " ")
    out = _WS.sub(" ", out)
    out = _MULTI_NL.sub("\n", out)
    return out.strip()


def collapse_whitespace(text: str) -> str:
    return _WS.sub(" ", text).strip()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - len(suffix)]
    # try not to cut inside a word
    if " " in cut[max(0, len(cut) - 12) :]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:") + suffix


def word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


__all__ = [
    "collapse_whitespace",
    "normalise_for_speech",
    "strip_emoji",
    "strip_markdown",
    "truncate",
    "word_count",
]
