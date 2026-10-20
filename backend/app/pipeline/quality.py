"""Reply-quality heuristics: catch the two ways a companion goes bad.

Both failure modes are *cheap to detect before anything is spoken*, which is why
this module exists: the orchestrator holds back the reply's **first sentence**
until it has been judged, so a bad attempt can be thrown away and regenerated
without a single word of it reaching the speakers.

1. **Parroting.** ``"I like coffee"`` -> ``"You said I like coffee."`` The words
   are all there, nothing was *answered*. Measured as the share of the reply's
   content words that also appear in the user's message.
2. **Repetition.** The model answers a new question with a reworded version of
   what it said last time. Measured as the share of content words shared with the
   previous assistant reply, plus plain Jaccard similarity.

Both are word-set measures, not semantic ones, and that is deliberate: they must
be *fast* (they run on the token path), *deterministic* (unit-testable) and
*language agnostic* (they make no assumptions about grammar). A false positive
costs one extra generation; a false negative is what the user complains about.

"Language agnostic" has to mean more than "ASCII agnostic": the avatar is built
to be talked to in English, Bengali *and* Banglish, and all three have to be
guarded. See :func:`content_tokens` for the one implementation detail that
matters (combining marks).
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

__all__ = [
    "content_tokens",
    "is_echo",
    "is_repetition",
    "jaccard",
    "normalise",
    "overlap",
]

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")

#: A token is a run of characters whose Unicode general category starts with L
#: (letter), M (mark) or N (number).
#:
#: ``\w`` cannot be used: in Python it matches letters and digits but **not**
#: combining marks, and Bengali writes its vowels - and its nukta - as combining
#: marks. ``\w`` would answer ``"আমি"`` with ``"আ"`` + ``"ম"`` and quietly
#: disable the guard for the language this app is built for. Categories do the
#: right thing in every script, including the Indic ones.
_TOKEN_CATEGORIES = frozenset("LMN")
#: An apostrophe stays inside a word ("don't"); an underscore does not.
_WORD_PUNCTUATION = frozenset("'\u2019")

#: Extremely common words carry no topical signal, and counting them would make
#: every short reply look like an echo ("I am ..." / "you are ...").
_STOPWORDS_EN = """
    a an and are as at be been but by can could did do does doing for from had has
    have having he her here him his how i if in into is it its just like me my no
    not of on or our out she so some than that the their them then there these
    they this to too up us very was we were what when where which who why will
    with would you your yours
"""

#: The same idea in Bengali script. Spelling is what a speaker would *type*,
#: and the set is folded exactly like the text is (``য়`` decomposes under NFKD,
#: so a literal set would never match a token built from the same word).
_STOPWORDS_BN = """
    আমি আমার আমাকে আমরা আমাদের তুমি তোমার তোমাকে তুই তোর আপনি আপনার আপনাকে
    সে তার তাকে তারা তাদের এটা ওটা এই সেই এখানে সেখানে
    কি কী কেন কেমন করে করেছে করেছি করেছ করব করবে করছে করছি করা
    হয় হচ্ছে হয়েছিল হয়েছে হবে ছিলেন ছিল ছিলাম ছিলো
    না নেই নাই আর আরও কিন্তু তাই তাহলে তবে যদি যে যেন হ্যাঁ এবং
    জন্য থেকে সাথে সঙ্গে মধ্যে পরে আগে এখন আজ কাল
    একটা একটি এর মতো মানে বলো বলে বলছি
"""

#: ...and in romanised Bengali, so a Banglish reply that only recycles the
#: user's function words is still caught.
_STOPWORDS_BANGLISH = """
    ami amake amar amader amra apni apnake apnar apnader apnara
    tumi tomake tomar tomader tumra tui tor
    she se tar tader tara ei oi eta eita oita
    ki keno kemon kemne kotha kothay kobe koi
    nei nai ache achhe achen achi acho chilo chilam
    hoy hoye hoyeche holo hobe hocche
    kori koro kore korte korle korchi korche korbo korlam kora
    ar aro aktu ekta ektu khub onek
    ekhon akhon tokhon aj ajke kalke por porer ager
    theke sathe shathe jonno jnno moddhe majhe kintu tai tahole jodi hole
    na hae hyan haan mane
"""

#: A reply this long is never written "by accident", so it is never treated as a
#: copy of the user's message.
_MAX_ECHO_TOKENS = 30


@lru_cache(maxsize=1)
def _stopwords() -> frozenset[str]:
    """Every stop-word block, folded the way :func:`normalise` folds text."""
    return frozenset(
        normalise(word)
        for block in (_STOPWORDS_EN, _STOPWORDS_BN, _STOPWORDS_BANGLISH)
        for word in block.split()
    )


def normalise(text: str) -> str:
    """Case-fold and fold Latin accents, so ``Café`` == ``cafe``.

    Accents are only folded on Latin letters. Dropping *every* combining mark -
    the obvious implementation, and the one this replaced - also strips Bengali
    vowel signs (``ি``, ``ু``, ``ো``) and leaves consonant skeletons, which is a
    silent "the guard does not work in Bengali" bug. Limiting the fold to Latin
    keeps ``Café`` == ``cafe`` without mangling an abugida.
    """
    folded = unicodedata.normalize("NFKD", text).casefold()
    out: list[str] = []
    for char in folded:
        if unicodedata.combining(char) and out and ord(out[-1]) < 0x0250:
            continue  # a Latin accent: Café -> cafe
        out.append(char)
    return "".join(out)


def _is_token_char(char: str) -> bool:
    return char in _WORD_PUNCTUATION or unicodedata.category(char)[0] in _TOKEN_CATEGORIES


def _tokenise(text: str) -> list[str]:
    """Split `text` on everything that is not a letter, mark, digit or '."""
    tokens: list[str] = []
    current: list[str] = []
    for char in text:
        if _is_token_char(char):
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def content_tokens(text: str) -> set[str]:
    """Topical words in `text`: stop words removed, CJK split per character."""
    lowered = normalise(text)
    tokens = {token for token in _tokenise(lowered) if len(token) > 1}
    tokens -= _stopwords()
    # Japanese/Chinese have no word separators: characters are the unit.
    tokens.update(_CJK_RE.findall(lowered))
    return tokens


def overlap(candidate: set[str], reference: set[str]) -> float:
    """Share of `candidate` that also appears in `reference`, 0..1."""
    if not candidate:
        return 0.0
    return len(candidate & reference) / len(candidate)


def jaccard(a: set[str], b: set[str]) -> float:
    """Symmetric similarity of two token sets, 0..1."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def is_echo(reply: str, user_text: str, *, threshold: float = 0.75) -> bool:
    """True when the reply is (almost) only the user's own words rearranged.

    A short ``"Yes, exactly!"`` shares nothing and passes; a straight repeat of
    the user's sentence scores 1.0 and is rejected.
    """
    reply_tokens = content_tokens(reply)
    user_tokens = content_tokens(user_text)
    if len(reply_tokens) < 2 or not user_tokens:
        return False
    if len(reply_tokens) > _MAX_ECHO_TOKENS:
        return False
    return overlap(reply_tokens, user_tokens) >= threshold


def is_repetition(
    reply: str,
    previous: str | None,
    *,
    threshold: float = 0.8,
) -> bool:
    """True when the reply repeats an earlier assistant message.

    Something as short as ``"Okay!"`` is never flagged, otherwise the second
    ``"Okay!"`` in a long conversation would trigger a pointless regeneration.
    """
    if not previous:
        return False
    reply_tokens = content_tokens(reply)
    previous_tokens = content_tokens(previous)
    if len(reply_tokens) < 3 or len(previous_tokens) < 3:
        return False
    if overlap(reply_tokens, previous_tokens) >= threshold:
        return True
    return jaccard(reply_tokens, previous_tokens) >= threshold
