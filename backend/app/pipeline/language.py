"""Which language is the user actually writing in?

The persona promises to answer in the language it is addressed in - a promise
that is empty for a language the model cannot see in the prompt. This module is
the smallest thing that keeps it: it looks at what the user typed and names the
language, which the orchestrator then states explicitly in the system prompt
("The user just wrote in Bengali (বাংলা). Reply in Bengali (বাংলা)") *and* uses
to pick a voice that can actually pronounce the answer.

Two kinds of input need naming, and only one of them is a script question:

* **scripted text** - ``"আমি ভালো আছি"`` is Bengali, ``"こんにちは"`` is Japanese.
  Exact, free, needs no model.
* **romanised text** - ``"ami bhalo achi"`` is Bengali *typed in Latin letters*
  ("Banglish"), which is how a very large number of Bengali speakers type every
  day. A script-only detector answers "English" here and the avatar replies in
  English, which is the precise complaint this module exists to prevent. A
  conservative marker-word list catches it instead: at least two distinct
  Bengali function words, or it stays English.

Honest about its limits: script detection is not language detection (Hindi and
Marathi share Devanagari), and the romanised check is a heuristic rather than a
classifier. Both cost nothing, need no model, and are trivially testable - which
is what the language hint in a *prompt* needs to be.
"""

from __future__ import annotations

import re

__all__ = [
    "BANGLISH",
    "LANGUAGE_NAMES",
    "LOCALES",
    "detect_language",
    "is_banglish",
    "locale_for",
    "primary_subtag",
]

#: Romanised Bengali ("Banglish"). Not a real BCP-47 tag: it is a *code point
#: for our own prompts and logs*, signalling "Bengali, written in Latin script".
BANGLISH = "bn-Latn"

#: Short code -> the name used in the prompt.
LANGUAGE_NAMES: dict[str, str] = {
    "bn": "Bengali (বাংলা)",
    BANGLISH: "Bengali written in Latin letters (Banglish)",
    "hi": "Hindi (हिन्दी)",
    "ar": "Arabic",
    "fa": "Persian",
    "ur": "Urdu",
    "ja": "Japanese (日本語)",
    "zh": "Chinese",
    "ko": "Korean",
    "ru": "Russian",
    "el": "Greek",
    "he": "Hebrew",
    "th": "Thai",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "ml": "Malayalam",
    "gu": "Gujarati",
    "pa": "Punjabi",
    "ne": "Nepali",
    "si": "Sinhala",
    "en": "English",
}

#: Which locale *speaks* a language.
#:
#: Used for two things that both fail without it: the browser's
#: ``speechSynthesis`` (which picks a voice from the locale) and the
#: ``tts.start.lang`` frame we send it. A script-specific code deliberately
#: collapses onto the spoken locale - ``bn-Latn`` answers ``bn-BD``, because a
#: Bengali voice reading romanised Bengali is still Bengali, while an English
#: voice reading Bengali *script* is noise.
LOCALES: dict[str, str] = {
    "en": "en-US",
    "bn": "bn-BD",
    "hi": "hi-IN",
    "ur": "ur-PK",
    "ar": "ar-SA",
    "fa": "fa-IR",
    "ta": "ta-IN",
    "te": "te-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "gu": "gu-IN",
    "mr": "mr-IN",
    "ne": "ne-NP",
    "si": "si-LK",
    "pa": "pa-IN",
    "ja": "ja-JP",
    "zh": "zh-CN",
    "ko": "ko-KR",
    "ru": "ru-RU",
    "uk": "uk-UA",
    "pl": "pl-PL",
    "cs": "cs-CZ",
    "hu": "hu-HU",
    "ro": "ro-RO",
    "el": "el-GR",
    "he": "he-IL",
    "tr": "tr-TR",
    "vi": "vi-VN",
    "th": "th-TH",
    "id": "id-ID",
    "ms": "ms-MY",
    "fil": "fil-PH",
    "nl": "nl-NL",
    "de": "de-DE",
    "fr": "fr-FR",
    "es": "es-ES",
    "it": "it-IT",
    "pt": "pt-BR",
    "sv": "sv-SE",
    "da": "da-DK",
    "fi": "fi-FI",
    "nb": "nb-NO",
}

#: Ordered: the first script with enough characters to be unambiguous wins.
#: Latin/ASCII is checked last and only needs to be *present*.
#:
#: The Arabic block is deliberately absent here: Persian and Urdu share it, so a
#: single range check would answer "Arabic" for all three. See `_arabic_variant`.
_SCRIPTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bn", re.compile(r"[\u0980-\u09ff]")),
    ("hi", re.compile(r"[\u0900-\u097f]")),
    ("ta", re.compile(r"[\u0b80-\u0bff]")),
    ("te", re.compile(r"[\u0c00-\u0c7f]")),
    ("kn", re.compile(r"[\u0c80-\u0cff]")),
    ("ml", re.compile(r"[\u0d00-\u0d7f]")),
    ("gu", re.compile(r"[\u0a80-\u0aff]")),
    ("pa", re.compile(r"[\u0a00-\u0a7f]")),
    ("th", re.compile(r"[\u0e00-\u0e7f]")),
    ("ja", re.compile(r"[\u3040-\u30ff]")),
    ("ko", re.compile(r"[\uac00-\ud7af]")),
    ("zh", re.compile(r"[\u4e00-\u9fff]")),
    ("ru", re.compile(r"[\u0400-\u04ff]")),
    ("el", re.compile(r"[\u0370-\u03ff]")),
    ("he", re.compile(r"[\u0590-\u05ff]")),
)

#: The whole Arabic block, shared by Arabic, Persian and Urdu.
_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06ff]")
#: Letters Persian **and** Urdu added to the Arabic alphabet.
_PERSIAN_SHARED = frozenset("پچژگ")
#: Letters only Urdu has: the retroflexes, heh-doachashmee and the final ye.
_URDU_ONLY = frozenset("ٹڈڑںھےۂۓ")

_ASCII_LETTERS = re.compile(r"[a-z]", re.IGNORECASE)
_LATIN_WORDS = re.compile(r"[a-z]+")
#: A couple of non-ASCII characters is enough to outvote a stray English word.
_MIN_HITS = 2

#: Bengali words as people actually type them with a Latin keyboard.
#:
#: Chosen to be *evidence*, not vocabulary: pronouns, copulas, question words
#: and the postpositions that English prose almost never contains. A handful of
#: these (``na``, ``hae``) also occur in other languages, which is why one is
#: never enough.
_BANGLISH_MARKERS = frozenset(
    """
    ami amake amar amader amra
    apni apnake apnar apnader apnara
    tumi tomake tomar tomader tumra tui tor
    she se tar tader tara ei oi eta eita oita
    ki keno kemon kemne kotha kothay kobe koi
    nei nai ache achhe achen achi acho chilo chilam
    hoy hoye hoyeche holo hobe hocche
    kori koro kore korte korle korchi korche korbo korlam kora
    ar aro aktu ekta ektu khub onek
    ekhon akhon tokhon aj ajke kalke por porer ager
    theke sathe shathe jonno jnno moddhe majhe kintu tai tahole jodi hole
    na hae hyan haan mane dhonnobad
    """.split()
)

#: Distinct marker words needed before Latin text is called Bengali.
_BANGLISH_MIN_HITS = 2

#: Script subtags (`bn-Latn`, `zh-Hans`) describe *writing*, not a region, so
#: they are replaced by the locale we voice. Everything else (`en-GB`) is kept.
_SCRIPT_SUBTAGS = frozenset(
    {"latn", "cyrl", "arab", "hans", "hant", "beng", "deva", "jpan", "kore"}
)


def primary_subtag(code: str) -> str:
    """``"bn-BD"`` -> ``"bn"``, and ``"en-US-AriaNeural"`` -> ``"en"``.

    Works for a language code *and* for an Edge voice name, which is what makes
    "is the configured voice already the right language?" a one-liner.
    """
    return (code or "").strip().lower().replace("_", "-").split("-", 1)[0]


def is_banglish(text: str) -> bool:
    """True when Latin text is really romanised Bengali.

    Conservative on purpose: it needs :data:`_BANGLISH_MIN_HITS` *distinct*
    marker words, so a stray "ki" - or any single one - never flips an English
    sentence, while ``"ami bhalo achi"`` and ``"ki koro tumi"`` both do.
    """
    if not text:
        return False
    words = set(_LATIN_WORDS.findall(text.lower()))
    if not words:
        return False
    return len(words & _BANGLISH_MARKERS) >= _BANGLISH_MIN_HITS


def _arabic_variant(text: str) -> str:
    """Arabic, Persian or Urdu? The block is shared, the extra letters are not.

    Persian gold ``گ`` and Urdu's final ``ے`` are the giveaway: a sentence with
    an Urdu-only letter is Urdu, otherwise any Persian-shared letter means
    Persian, and a sentence that has neither is Arabic. Ties (``چ`` alone, which
    both alphabets have) go to Persian - the more likely of the two, and either
    answer is a one-sentence mistake at worst.
    """
    if any(char in text for char in _URDU_ONLY):
        return "ur"
    if any(char in text for char in _PERSIAN_SHARED):
        return "fa"
    return "ar"


def detect_language(text: str) -> str:
    """Best-effort language code for ``text``, or ``""`` when unrecognisable.

    Returns ``""`` rather than ``"en"`` for punctuation, digits and whitespace,
    so the caller can tell "this is English" from "this is not language at all".
    """
    if not text or not text.strip():
        return ""
    for code, pattern in _SCRIPTS:
        if len(pattern.findall(text)) >= _MIN_HITS:
            return code
    if len(_ARABIC_SCRIPT.findall(text)) >= _MIN_HITS:
        return _arabic_variant(text)
    if _ASCII_LETTERS.search(text):
        return BANGLISH if is_banglish(text) else "en"
    return ""


def locale_for(language: str, *, fallback: str = "en-US") -> str:
    """A BCP-47 locale for ``language``, for ``speechSynthesis`` and the wire.

    ``"bn"`` and ``"bn-Latn"`` both answer ``"bn-BD"``; a code we do not know is
    passed through untouched so the browser can still try its own voice list,
    and an empty one falls back to English.
    """
    raw = (language or "").strip()
    if not raw:
        return fallback
    head, _, rest = raw.lower().partition("-")
    known = LOCALES.get(head)
    if known is None:
        return raw
    if rest and rest not in _SCRIPT_SUBTAGS:
        return raw  # a real region (`en-GB`) means what it says
    return known
