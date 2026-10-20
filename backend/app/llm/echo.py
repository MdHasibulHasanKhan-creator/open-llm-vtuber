"""Offline demo "brain".

``provider: echo`` lets the whole stack (WebSocket, TTS, lip-sync, emotions,
gestures) be exercised without an LLM, a GPU or an API key. It streams a reply
with a configurable delay so it feels like a real model, and it deliberately
uses ``[emotion]`` tags so you can watch the face react.

    VTUBER_LLM__PROVIDER=echo

The important thing about this module is that it is **not a fixed script** any
more. Earlier versions picked a canned sentence per *intent*, so every question
got one of the same four replies and the app read as broken - which is exactly
what the shipped ``.env`` produced when Ollama was not installed. Three rules
now make it behave like a very small, very confident friend:

* **It reacts to the topic.** The salient word is pulled out of the message
  (any script: English, Bengali, CJK) and woven into the reply, so "how do I fix
  my deadlines?" is answered *about deadlines*.
* **It never quotes you.** The topic word appears once, inside a sentence whose
  other content words are its own, which keeps it well clear of the parroting
  guard in :mod:`app.pipeline.quality`.
* **It never repeats itself.** One shuffled "bag" per pool: every reply is used
  once before any is reused, and the same reply can never come twice in a row.

It also uses :meth:`set_context` to fold in the long-term memory profile, so the
offline experience still remembers your name and the things you said you like.

The UI shows a banner whenever this provider is active, so nobody mistakes demo
output for a model's.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import AsyncIterator
from string import Template
from typing import Any

from loguru import logger

from ..memory.facts import topical_words
from .base import ChatMessage, LLMEngine

# ---------------------------------------------------------------- vocabulary --

_GREETINGS = ("hi", "hello", "hey", "yo", "good morning", "good evening", "morning", "নমস্কার", "হ্যালো")
_FAREWELLS = ("bye", "goodbye", "see you", "good night", "goodnight", "বিদায়")
_THANKS = ("thank", "thanks", "cheers", "appreciate", "ধন্যবাদ")
_AFFIRMATIONS = ("yes", "yeah", "yep", "no", "nope", "sure", "ok", "okay", "right", "exactly", "হ্যাঁ", "না")

#: Interrogative word -> the question kind we answer as.
_QUESTION_WORDS: dict[str, str] = {
    "what": "what", "why": "why", "how": "how", "when": "when", "where": "where",
    "who": "who", "whom": "who", "whose": "who", "which": "which",
    "কী": "what", "কি": "what", "কেন": "why", "কীভাবে": "how", "কিভাবে": "how",
    "কেমন": "how", "কখন": "when", "কোথায়": "where", "কে": "who",
    "কোনটা": "which", "কোন": "which",
}

#: Auxiliaries that turn a sentence into a yes/no question.
_YESNO_STARTERS = frozenset(
    """
    do does did is are was were can could would will shall have has had am
    should may might must
    """.split()
)

#: Used when a message has no topic word at all, so the templates still read.
_TOPIC_FALLBACK = "that"

#: Split on whitespace and punctuation only - never inside a word. A regex
#: ``\w`` class would cut Bengali words at the matras (combining marks are not
#: ``\w``), and substring checks would read "you" as the greeting "yo".
_SEPARATORS = re.compile(
    r"[\s.,!?;:\"'()\[\]{}\u2018\u2019\u201c\u201d\u2013\u2014/\\|<>*+=~`@#$%^&]+"
)


def _tokens(lowered: str) -> set[str]:
    return {token for token in _SEPARATORS.split(lowered) if token}


def _mentions(lowered: str, tokens: set[str], phrases: tuple[str, ...]) -> bool:
    for phrase in phrases:
        if " " in phrase:
            if phrase in lowered:
                return True
        elif phrase in tokens:
            return True
    return False

# --------------------------------------------------------------- reply pools --
#: ``$Topic`` / ``$topic`` is the salient word (capitalised / as written), and
#: ``$name`` is only filled when the memory profile knows it.
_POOLS: dict[str, tuple[str, ...]] = {
    "greeting": (
        "[happy] Hey, good to see you. I was idling here waiting for someone to talk to.",
        "[happy] Hi there - come in, pull up a chair, metaphorically speaking.",
        "[relaxed] Hello! You have my full attention, such as it is.",
        "[happy] There you are. What is on your mind?",
    ),
    "thanks": (
        "[happy] Any time. That is what I am here for.",
        "[relaxed] No problem at all - glad that helped.",
        "[happy] You are welcome. I like being useful.",
    ),
    "farewell": (
        "[sad] Going already? Alright. Come back and tell me how it went.",
        "[relaxed] See you soon. I will be right here.",
        "[happy] Bye for now! I will keep the lights on.",
    ),
    "affirmation": (
        "[happy] Right? I thought so too.",
        "[relaxed] Good, we are on the same page then.",
        "[surprised] Huh, I did not expect that answer.",
    ),
    "short": (
        "[relaxed] Mm. Tell me a bit more?",
        "[happy] Alright. And then what happened?",
        "[surprised] Oh, go on.",
        "[happy] Short and to the point - I like it.",
    ),
    "long": (
        "[surprised] That is a lot to take in. The part that stands out is how much of "
        "it you did yourself.",
        "[relaxed] Okay, I think I follow. The interesting bit is not the result, it is "
        "how you got there.",
        "[happy] I like this story. Give me the next part.",
        "[thinking] Hm, that is a lot of moving pieces. Which one is actually bothering you?",
    ),
    "statement": (
        "[relaxed] $Topic. I get that - I would probably feel the same way.",
        "[happy] $Topic is a good thing to have on your mind, honestly.",
        "[surprised] Huh, I had not thought about $topic like that before.",
        "[thinking] $Topic - I will keep that in mind for later.",
        "[relaxed] Okay, noted about $topic. Is that new, or has it been sitting there a while?",
        "[happy] I like that you brought up $topic. It is the kind of thing worth saying out loud.",
    ),
    "question_generic": (
        "[relaxed] Good question. I do not have the whole answer, but I would start with "
        "the part of $topic that is easiest to test.",
        "[surprised] Hmm, $topic is worth thinking about. My instinct says it depends on "
        "how much time you want to spend.",
        "[thinking] Let me think about $topic for a second. The honest answer is that I am "
        "not sure yet.",
        "[happy] Oh, I like that one. Short answer: probably. Long answer: mostly, with one "
        "annoying exception.",
        "[relaxed] $Topic? I am honestly not sure. What made you think of it?",
    ),
    "question_what": (
        "[thinking] $Topic is one of those things with a tidier answer than people expect. "
        "Give me a second and I will get there.",
        "[relaxed] The simple explanation of $topic is usually close enough to be useful.",
        "[surprised] $Topic. Ask me again tomorrow and I might have a better answer.",
        "[happy] Oh, easy - for $topic the beginner answer and the expert answer are "
        "annoyingly similar.",
    ),
    "question_why": (
        "[relaxed] Why $topic? Usually because the obvious option costs more than it looks.",
        "[thinking] $Topic has two reasons: the one people say out loud, and the one that "
        "is actually true.",
        "[surprised] Good question. I suspect $topic comes down to something simpler than "
        "it feels right now.",
    ),
    "question_how": (
        "[relaxed] For $topic, I would break it into one step you can finish today and go "
        "from there.",
        "[thinking] How to do $topic - start with the smallest piece that still counts as "
        "progress.",
        "[happy] $Topic? Smallest possible version first. That is how these things actually "
        "get done.",
    ),
    "question_when": (
        "[relaxed] For $topic, I would say sooner than feels comfortable and later than it "
        "feels urgent.",
        "[thinking] When it comes to $topic, the honest answer is 'it depends' and the "
        "useful answer is 'soon'.",
    ),
    "question_where": (
        "[relaxed] For $topic, I would look wherever the people who already solved it hang out.",
        "[thinking] Where to find $topic... probably closer than you think, in a place you "
        "have already looked.",
    ),
    "question_who": (
        "[thinking] Who to ask about $topic? Someone who has already been through it - "
        "those people are usually easier to find than you expect.",
        "[relaxed] For $topic, the right person is whoever answers without making you feel "
        "stupid for asking.",
    ),
    "question_which": (
        "[thinking] Between the options, I would pick the one you can explain out loud in "
        "one sentence.",
        "[relaxed] For $topic, the choice that leaves you less to maintain is usually the "
        "right one.",
    ),
    "question_yesno": (
        "[relaxed] For $topic, I would lean yes - with one small caveat I cannot pin down yet.",
        "[thinking] $Topic... probably yes. Not a confident yes, but a yes.",
        "[happy] I want to say yes about $topic. Let me not overthink it.",
    ),
}

#: Used when the profile knows the user's name and they just said hello.
_NAME_GREETINGS: tuple[str, ...] = (
    "[happy] $name! Good to see you again.",
    "[happy] Hey $name - I was hoping you would come back.",
    "[relaxed] Oh, it is you, $name. What are we getting into today?",
)

#: Used when the topic matches something the profile says they like / dislike.
_MEMORY_LIKE: tuple[str, ...] = (
    "[happy] $Topic - I remember you are fond of that. Good.",
    "[happy] $Topic again! I did not forget you like it.",
)
_MEMORY_DISLIKE: tuple[str, ...] = (
    "[sad] $Topic - I remember that is not your favourite thing.",
    "[thinking] $Topic. Right, you told me you are not keen on it.",
)

#: Used when the client connects and the greeting turn fires with no user text.
_SMALL_TALK: tuple[str, ...] = (
    "[happy] Hello! I am Ai - this is the offline demo brain, but everything you can "
    "see and hear around it is the real pipeline.",
    "[happy] Hi! I am running without a language model right now, so my words are canned - "
    "my voice, face and gestures are not.",
)

#: Mentioned occasionally, so a demo session is self-explanatory.
_DISCLAIMER = (
    "By the way, I am still in offline demo mode, so that reply came from a script "
    "rather than a model."
)


# -------------------------------------------------------------- classification --

def _question_kind(lowered: str, words: list[str], tokens: set[str]) -> str:
    """``""`` or ``"question_<kind>"``."""
    if not words:
        return ""
    first = words[0].strip(".,!?\u0964\u06d4\u2026")
    if first in _QUESTION_WORDS:
        return f"question_{_QUESTION_WORDS[first]}"
    for word, kind in _QUESTION_WORDS.items():
        # Bengali question words can appear anywhere in the sentence.
        if len(word) > 1 and word in tokens:
            return f"question_{kind}"
    if "?" in lowered:
        if first in _YESNO_STARTERS:
            return "question_yesno"
        return "question_generic"
    if first in _YESNO_STARTERS:
        return "question_yesno"
    return ""


def _classify(text: str) -> str:
    """A deliberately cheap intent guess: this is a demo, not an NLU stack."""
    lowered = text.strip().casefold()
    if not lowered:
        return "greeting"
    words = lowered.split()
    tokens = _tokens(lowered)

    if len(words) <= 4:
        if _mentions(lowered, tokens, _THANKS):
            return "thanks"
        if _mentions(lowered, tokens, _FAREWELLS):
            return "farewell"
        if _mentions(lowered, tokens, _GREETINGS):
            return "greeting"
        first = words[0].strip(".,!?")
        if first in _AFFIRMATIONS:
            return "affirmation"

    kind = _question_kind(lowered, words, tokens)
    if kind:
        return kind

    if len(words) >= 35:
        return "long"
    # Whole words only: "you" must not match the greeting "yo".
    if words and (words[0] in _GREETINGS or any(
        lowered.startswith(phrase) for phrase in _GREETINGS if " " in phrase
    )):
        return "greeting"
    if len(words) <= 4:
        return "short"
    return "statement"


#: Social moves carry no topic: "hello there" must not answer *about* "hello".
_SOCIAL_WORDS = frozenset(
    word for word in (*_GREETINGS, *_FAREWELLS, *_THANKS, *_AFFIRMATIONS) if " " not in word
)


def _topic(text: str) -> str:
    """The single most salient word in the message (any script), or ``""``.

    Two cheap signals, in order: a *proper noun* (a word capitalised somewhere
    other than the start of the message - "France", "Dhaka") beats a common word
    ("capital"), and failing that the longest remaining word wins. Scripts
    without letter case (Bengali, Japanese) simply skip the first signal.
    """
    words = topical_words(text, limit=8)
    candidates = [
        word
        for word in words
        if word.casefold() not in _QUESTION_WORDS and word.casefold() not in _SOCIAL_WORDS
    ]
    if not candidates:
        return ""

    lowered_candidates = {word.casefold() for word in candidates}
    raw_tokens = _SEPARATORS.split(text.strip())
    proper = [
        token
        for index, token in enumerate(raw_tokens)
        if index > 0 and token[:1].isupper() and token.casefold() in lowered_candidates
    ]
    if proper:
        return proper[-1]
    # `topical_words` sorts by descending length, so the first is the longest.
    return candidates[0]


def _capitalise(word: str) -> str:
    """``coffee`` -> ``Coffee``. A no-op for scripts without letter case."""
    return word[:1].upper() + word[1:] if word else _TOPIC_FALLBACK


class EchoLLM(LLMEngine):
    """Deterministic, dependency free responder."""

    name = "echo"

    def __init__(self, config) -> None:
        super().__init__(config)
        #: Fake time-to-first-token (seconds).
        self.ttft = 0.12
        #: Fake per-token delay (seconds), keeps the streaming UI honest.
        self.token_delay = 0.018
        self._turn = 0
        self._profile: dict[str, Any] = {}
        #: One shuffled "bag" per reply pool. We draw from the bag until it is
        #: empty, then reshuffle - so every reply is used once before any is
        #: reused, and (see :meth:`_pick`) the same reply can never come twice in
        #: a row. Drawing independently at random *did* repeat, which is exactly
        #: the looping demo the offline brain is meant to avoid.
        self._bags: dict[tuple[str, ...], list[str]] = {}
        self._last = ""

    # ------------------------------------------------------------------ memory --
    def set_context(self, context: dict[str, Any]) -> None:
        """Remember who we are talking to (see :meth:`LLMEngine.set_context`)."""
        profile = context.get("profile")
        self._profile = profile if isinstance(profile, dict) else {}

    def _memory_pool(self, topic: str, intent: str) -> tuple[str, ...] | None:
        """A personalised pool, or ``None`` to use the ordinary one.

        Deliberately rationed: calling back a *like* every single time would be
        worse than never mentioning it.
        """
        if not self._profile:
            return None
        name = str(self._profile.get("name") or "").strip()
        if name and intent == "greeting" and self._turn % 2 == 0:
            return _NAME_GREETINGS
        if not topic:
            return None
        needle = topic.casefold()
        if self._turn % 3 == 0:
            if any(needle in str(item).casefold() for item in self._profile.get("likes") or ()):
                return _MEMORY_LIKE
            if any(needle in str(item).casefold() for item in self._profile.get("dislikes") or ()):
                return _MEMORY_DISLIKE
        return None

    # ------------------------------------------------------------------ picking --
    def _pick(self, pool: tuple[str, ...]) -> str:
        bag = self._bags.get(pool)
        if not bag:
            bag = list(pool)
            random.shuffle(bag)
            if len(bag) > 1 and bag[-1] == self._last:
                # Do not start the new cycle with the reply we just used.
                bag[0], bag[-1] = bag[-1], bag[0]
            self._bags[pool] = bag
        reply = bag.pop()
        self._last = reply
        return reply

    def _compose(self, user_text: str) -> str:
        intent = _classify(user_text)
        topic = _topic(user_text)
        if intent == "short" and topic:
            # A short but *meaningful* message ("I love coffee") deserves a reply
            # about its content, not a generic "go on".
            intent = "statement"

        pool = self._memory_pool(topic, intent)
        if pool is None:
            pool = _POOLS.get(intent) or _POOLS["statement"]

        template = self._pick(pool)
        name = str(self._profile.get("name") or "").strip() or "you"
        reply = Template(template).safe_substitute(
            topic=topic or _TOPIC_FALLBACK,
            Topic=_capitalise(topic or _TOPIC_FALLBACK),
            name=name,
        )
        if self._turn % 4 == 2:
            reply += " " + _DISCLAIMER
        logger.debug("echo brain turn {}: intent={} topic={!r}", self._turn, intent, topic)
        return reply

    # ------------------------------------------------------------------- stream --
    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        last_user = next(
            (m.content.strip() for m in reversed(messages) if m.role == "user"),
            "",
        )
        self._turn += 1
        template = self._pick(_SMALL_TALK) if not last_user else self._compose(last_user)

        await asyncio.sleep(self.ttft)

        # Stream word by word, splitting inside the words sometimes, which is
        # exactly the fragmentation a real tokenizer produces.
        cursor = 0
        while cursor < len(template):
            step = random.randint(1, 4)
            piece = template[cursor : cursor + step]
            cursor += step
            yield piece
            await asyncio.sleep(self.token_delay * step)

    async def health_check(self) -> tuple[bool, str]:
        return True, "offline demo brain"


__all__ = ["EchoLLM"]
