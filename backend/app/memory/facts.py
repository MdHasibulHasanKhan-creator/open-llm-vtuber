"""Pull durable facts out of what the user says - without a language model.

The point of this module is *cheap, deterministic, offline* extraction. The
companion should still feel like it remembers you when no model is reachable,
and a small regex table costs microseconds where an LLM call costs a second and
may not even be available.

Two rules keep it honest:

* **Narrow triggers.** We only look for explicit statements ("my name is", "I
  work as", "I hate"). Nothing is inferred from vague text, because a memory
  that is wrong is worse than no memory at all.
* **Short captures.** A value is at most a few words and stops at the first
  clause boundary, so ``"I like coffee and long walks but not mornings"``
  records ``coffee`` rather than that whole sentence.

English and Bengali rules are included (Bengali is object-before-verb, so those
patterns look ahead for the verb). Bengali extraction is best effort: it is
unit-tested but the phrasing people actually use varies far more than English.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["ExtractedFacts", "extract", "topical_words"]


#: A value: short run of text that is not a clause boundary. Kept deliberately
#: tight - the regexes below are already narrow, so this is the last line of
#: defence against swallowing half a paragraph.
_VALUE = r"(?P<value>[^\n.!?,;\u0964\u06d4]{2,42})"

#: Leading words that are never part of what the user is telling us about.
_LEAD = frozenset(
    {"a", "an", "the", "my", "some", "really", "very", "to", "that", "of", "much", "so"}
)

#: A clause ends at one of these - the captured value must not cross it. The
#: second half is *modifiers*: without them "I work as a teacher during the week"
#: would remember the job as "teacher during the week".
_BOUNDARY = frozenset(
    {
        "and", "but", "because", "so", "although", "though", "if", "when", "while",
        "or", "also", "too", "as", "since", "unless", "whenever", "whereas",
        "during", "every", "after", "before", "until", "once", "today", "tomorrow",
        "yesterday", "now", "currently", "actually", "probably", "sometimes",
        "often", "usually", "always", "never", "still", "again", "originally",
        "nowadays", "lately", "recently", "honestly", "especially", "only", "in",
        "on", "at", "for", "with", "from", "about", "which", "who",
    }
)

#: (field, compiled pattern). ``field`` is either a fixed key or ``fact:<key>``.
_RULES: list[tuple[str, re.Pattern[str]]] = []


def _rule(field: str, pattern: str, *, flags: int = re.IGNORECASE) -> None:
    _RULES.append((field, re.compile(pattern, flags)))


# ---------------------------------------------------------------- English ---
_rule("name", rf"\bmy name(?:'s|s| is| 's)?\s+{_VALUE}")
_rule("name", rf"\bcall me\s+{_VALUE}")
_rule("name", rf"\bi'?m called\s+{_VALUE}")
_rule("from", rf"\bi(?:'m| am) from\s+{_VALUE}")
_rule("lives_in", rf"\bi live in\s+{_VALUE}")
_rule("works_as", rf"\bi work(?:,| as| at)?\s+{_VALUE}")
_rule("studies", rf"\bi study\s+{_VALUE}")
_rule("fact:favourite", rf"\bmy favou?rite\s+(?P<key>[a-z ]{{2,20}}?)\s+is\s+{_VALUE}")

_like_verbs = r"(?:love|like|enjoy|adore|prefer|am into|'m into|really like|really love)"
_rule("like", rf"\bi\s+(?:really |truly |kinda |kind of |absolutely |just )?{_like_verbs}\s+{_VALUE}")
_rule(
    "dislike",
    rf"\bi\s+(?:really |truly |absolutely |just )?"
    rf"(?:hate|dislike|can'?t stand|don'?t like|do not like|am not a fan of)\s+{_VALUE}",
)

# ---------------------------------------------------------------- Bengali ---
# Bengali is Subject-Object-Verb, so the object sits *before* the verb: the
# value is captured with a look-ahead that requires the verb to follow.
_name_bn = r"(?:আমার নাম|আমাকে বলা হয়|আমার নাম হলো)"
_rule("name", rf"{_name_bn}\s*(?P<value>[\u0980-\u09FF ]{{2,30}})")
_rule("from", r"আমি\s*(?P<value>[\u0980-\u09FF ]{2,30})\s*থাকি")
_rule("like", r"আমি\s*(?P<value>[\u0980-\u09FF ]{2,30})\s*(?:ভালোবাসি|পছন্দ করি)")
_rule("dislike", r"আমি\s*(?P<value>[\u0980-\u09FF ]{2,30})\s*পছন্দ করি না")

#: Words we never record as a "topic" - too generic to be worth remembering.
#: Includes Bengali function words and verbs: Bengali is object-before-verb, so
#: without them the *verb* of "আমি কফি ভালোবাসি" would outrank the object.
_TOPIC_STOPWORDS = frozenset(
    """
    the a an and or but if then than that this these those there here what when
    where which who why how is are was were be been being am do does did doing
    have has had having will would can could should may might must i you he she
    it we they me him her us them my your his its our their mine yours ours to
    of in on at by for with from into out up down over under again further once
    just very too also so not no yes ok okay well really quite maybe about
    thing things stuff something anything nothing get got make made know think
    want need like love hate tell say said going gonna wanna let lets
    আমি তুমি আপনি সে তারা আমরা আমার তোমার তার এই সেই একটা একটি এবং কিন্তু জন্য
    সাথে খুব সব এখন তারপর ভালোবাসি ভালো পছন্দ করি না হয় আছে ছিল থাকি কী কি কেন
    কিভাবে কেমন কখন কোথায় কে কোন কিছু অনেক এটা ওটা যে যেন করে করা হয়ে আর
    """.split()
)

_WORD = re.compile(r"[^\s]+")


@dataclass(slots=True)
class ExtractedFacts:
    """What one user message told us. Every field is optional."""

    name: str = ""
    facts: dict[str, str] = field(default_factory=dict)
    likes: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.name or self.facts or self.likes or self.dislikes)


def _clean_value(raw: str) -> str:
    """Trim a capture down to the few words that are actually the value."""
    words = raw.split()
    while words and words[0].casefold().strip(",.'\"") in _LEAD:
        words.pop(0)

    kept: list[str] = []
    for word in words:
        bare = word.casefold().strip(",.'\"")
        if bare in _BOUNDARY:
            break
        kept.append(word)
        if len(kept) >= 6:
            break

    value = " ".join(kept).strip(" \t\"'.,!?;:-\u2013\u2014")
    # A value that is *only* stopwords carries no information.
    if not value or all(w.casefold() in _TOPIC_STOPWORDS for w in value.split()):
        return ""
    return value


def topical_words(text: str, limit: int = 8) -> list[str]:
    """Salient words in a message, script-agnostic.

    ``content_tokens`` in ``pipeline.quality`` is built for *comparison* and
    deliberately drops everything outside ``[0-9a-z']`` - which silently deletes
    every Bengali word. This function has the opposite job (remembering what
    someone talks about), so it keeps any script and only removes words that are
    known to be empty filler.
    """
    words: list[str] = []
    seen: set[str] = set()
    for raw in _WORD.findall(text):
        token = raw.strip(".,!?;:\"'()[]{}\u2018\u2019\u201c\u201d\u2013\u2014-")
        if len(token) < 2:
            continue
        key = token.casefold()
        if key in _TOPIC_STOPWORDS or key in seen:
            continue
        seen.add(key)
        words.append(token)
    # Longer words tend to be the more specific ones; a stable tie-break keeps
    # the output deterministic for tests.
    words.sort(key=lambda w: (-len(w), w.casefold()))
    return words[:limit]


def extract(text: str) -> ExtractedFacts:
    """Run every rule over one message. Never raises."""
    out = ExtractedFacts()
    if not text or not text.strip():
        return out

    for field_name, pattern in _RULES:
        for match in pattern.finditer(text):
            value = _clean_value(match.group("value"))
            if not value:
                continue
            if field_name == "name":
                out.name = out.name or value.title()
            elif field_name == "like":
                out.likes.append(value)
            elif field_name == "dislike":
                out.dislikes.append(value)
            elif field_name.startswith("fact:"):
                key = (match.groupdict().get("key") or "").strip().casefold()
                key = f"{field_name.split(':', 1)[1]} {key}".strip()
                out.facts.setdefault(key, value)
            else:
                out.facts.setdefault(field_name, value)

    out.topics = topical_words(text)
    return out
