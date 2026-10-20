"""Grapheme -> viseme conversion ("G2P lite").

We do not ship a phonemizer (espeak/Phonetisaurus are heavy and awkward to
install). Instead we use a rule based transliteration that is good enough to
drive mouth shapes:

* English: digraph table (``oo``, ``ea``, ``ai`` ...) -> vowel viseme(s), every
  consonant collapses into a "closed-ish" shape, punctuation adds pauses.
* Japanese: kana -> romaji (complete table below) then the same vowel rules,
  which is exact for kana based text.

The viseme set is VRM 1.0's own mouth preset names plus ``nn`` which the
frontend interprets as "relax towards a closed mouth":

    aa = open "ah"      ih = wide "ee"      ou = rounded "oo"
    ee = mid "eh"       oh = rounded "oh"   nn = closed / consonant
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Every viseme this module can emit.
VISEMES: tuple[str, ...] = ("aa", "ih", "ou", "ee", "oh", "nn")

#: "Closed / neutral" pseudo viseme (consonants, pauses, silence).
CLOSED = "nn"

VOWEL_VISEME: dict[str, str] = {
    "a": "aa",
    "i": "ih",
    "u": "ou",
    "e": "ee",
    "o": "oh",
}

#: Multi letter clusters -> sequence of visemes (diphthongs get two frames).
#: Matched longest-first, before single letters.
DIGRAPHS: dict[str, tuple[str, ...]] = {
    # /aɪ/
    "igh": ("aa",),
    # pure long vowels
    "ee": ("ih",),
    "ea": ("ih",),
    "ie": ("ih",),
    "oo": ("ou",),
    # diphthongs
    "ai": ("ee", "ih"),
    "ay": ("ee", "ih"),
    "ei": ("ee", "ih"),
    "ey": ("ee", "ih"),
    "oi": ("oh", "ih"),
    "oy": ("oh", "ih"),
    "ou": ("aa", "ou"),
    "ow": ("aa", "ou"),
    "oa": ("oh", "ou"),
    "au": ("oh", "ou"),
    "eu": ("ih", "ou"),
    "ew": ("ih", "ou"),
    "aw": ("oh",),
    # r-coloured vowels (rhotic languages only, harmless elsewhere)
    "ar": ("aa",),
    "er": ("aa",),
    "ir": ("aa",),
    "or": ("oh",),
    "ur": ("aa",),
    "yr": ("aa",),
    # common silent endings handled by the "silent e" rule below
}

#: Extra units added to the timeline when we hit punctuation (in "phoneme
#: units"; a plain vowel costs 1.0).
PUNCTUATION_UNITS: dict[str, float] = {
    ",": 0.55,
    ";": 0.60,
    ":": 0.60,
    "-": 0.35,
    "\u3001": 0.55,  # 、
    ".": 1.05,
    "!": 1.15,
    "?": 1.15,
    "\u3002": 1.05,  # 。
    "\uff01": 1.15,  # ！
    "\uff1f": 1.15,  # ？
    "\u2014": 0.90,  # —
    "\u2026": 0.95,  # …
}

#: Cost of a word break (short closure between words).
SPACE_UNITS = 0.3
#: Cost of a single consonant (see `consonant_weight`).
DEFAULT_CONSONANT_UNITS = 0.32
DEFAULT_VOWEL_UNITS = 1.0

_VOWELS = set("aeiou")
_CONSONANTS = set("bcdfghjklmnpqrstvwxyz")
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_DIGRAPH_KEYS = sorted(DIGRAPHS, key=len, reverse=True)


@dataclass(slots=True, frozen=True)
class Phoneme:
    """One mouth shape and how long it should last, in relative units."""

    viseme: str
    units: float
    #: 0..1 relative emphasis; vowels are louder than consonants. Multiplied
    #: into the frame weight so the mouth opens wider on stressed vowels.
    loudness: float = 1.0


# --------------------------------------------------------------------------- #
#  Japanese: kana -> romaji
# --------------------------------------------------------------------------- #
_KANA_ROMAJI: dict[str, str] = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "ゐ": "i", "ゑ": "e", "を": "o", "ん": "n",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ゔ": "vu", "ゎ": "wa",
}
_SMALL_KANA: dict[str, str] = {"ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
                              "ゃ": "ya", "ゅ": "yu", "ょ": "yo"}
_YOUON_STEMS = {"ki", "shi", "chi", "ni", "hi", "mi", "ri", "gi", "ji", "bi", "pi"}


def _to_hiragana(text: str) -> str:
    """Katakana -> hiragana so a single table is enough."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:  # katakana block
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return "".join(out)


#: Stems that contract instead of taking a "y" glide (Hepburn): しゃ is "sha",
#: not "shya"; じゃ is "ja", not "jya".
_CONTRACTING_STEMS = {"shi": "sh", "chi": "ch", "ji": "j"}


def kana_to_romaji(text: str) -> str:
    """Transliterate kana (and keep other characters as-is)."""
    text = _to_hiragana(text)
    out: list[str] = []
    for ch in text:
        if ch in _SMALL_KANA:
            replacement = _SMALL_KANA[ch]  # "ya" | "yu" | "yo" | "a" | "i" | ...
            if replacement.startswith("y") and out:
                # き + ゃ -> kya ; し + ゃ -> sha ; じ + ゅ -> ju
                if (contracted := _CONTRACTING_STEMS.get(out[-1])) is not None:
                    out[-1] = contracted + replacement[1:]
                    continue
                if out[-1] in _YOUON_STEMS:
                    out[-1] = out[-1][:-1] + replacement
                    continue
            out.append(replacement)
            continue
        if ch == "っ":  # sokuon: doubles the following consonant
            out.append("_geminate_")
            continue
        if ch == "ー":  # long vowel mark
            out.append(out[-1][-1] if out and out[-1][-1] in _VOWELS else "a")
            continue
        out.append(_KANA_ROMAJI.get(ch, ch))
    romaji = "".join(out)
    # resolve sokuon: "_geminate_" becomes a duplicated initial consonant
    romaji = re.sub(r"_geminate_(\w)", lambda m: m.group(1) * 2, romaji)
    return romaji


def detect_language(text: str, default: str = "en") -> str:
    """Very small language guess - only used to pick a transliteration table."""
    if _KANA.search(text):
        return "ja"
    latin = sum(1 for ch in text if ch.isalpha() and ch.isascii())
    if latin >= 2:
        return "en"
    return default


# --------------------------------------------------------------------------- #
#  Tokenisation
# --------------------------------------------------------------------------- #
def _push(tokens: list[tuple[str, float, float]], viseme: str, units: float, loudness: float) -> None:
    if units <= 0:
        return
    if tokens and tokens[-1][0] == viseme:
        # coalesce: identical neighbours become one longer shape
        prev_viseme, prev_units, prev_loud = tokens[-1]
        tokens[-1] = (prev_viseme, prev_units + units, max(prev_loud, loudness))
        return
    tokens.append((viseme, units, loudness))


def _tokenise_romaji(
    text: str,
    *,
    consonant_units: float,
    vowel_units: float,
    include_consonants: bool,
) -> list[tuple[str, float, float]]:
    """Walk the (lowercased, romanised) text and emit (viseme, units, loudness).

    Vowels produce their own viseme, one to three consecutive consonants are
    collapsed into a single short closure (``nn``). That alternation is what
    makes the mouth look alive instead of glued open.
    """
    tokens: list[tuple[str, float, float]] = []
    i = 0
    length = len(text)
    pending_consonants = 0.0

    def flush_consonants() -> None:
        """Emit at most ~half a vowel worth of closure for a consonant run."""
        nonlocal pending_consonants
        if include_consonants and pending_consonants > 0:
            _push(tokens, CLOSED, min(pending_consonants, 0.55), 0.45)
        pending_consonants = 0.0

    while i < length:
        ch = text[i]

        # --- word break ---------------------------------------------------
        if ch.isspace():
            flush_consonants()
            if include_consonants:
                _push(tokens, CLOSED, max(SPACE_UNITS, 0.0), 0.35)
            i += 1
            continue

        # --- punctuation --------------------------------------------------
        if ch in PUNCTUATION_UNITS:
            flush_consonants()
            _push(tokens, CLOSED, PUNCTUATION_UNITS[ch], 0.3)
            i += 1
            continue
        if not ch.isalnum() and ch != "'":
            i += 1  # ignore quotes, brackets, ...
            continue

        # --- digits -------------------------------------------------------
        if ch.isdigit():
            flush_consonants()
            _push(tokens, "ih", vowel_units * 0.8, 0.8)
            i += 1
            continue

        # --- digraphs (longest first) -------------------------------------
        matched = False
        for key in _DIGRAPH_KEYS:
            if text.startswith(key, i):
                flush_consonants()
                for viseme in DIGRAPHS[key]:
                    _push(tokens, viseme, vowel_units * 0.6, 0.95)
                i += len(key)
                matched = True
                break
        if matched:
            continue

        # --- vowels -------------------------------------------------------
        if ch in _VOWELS:
            flush_consonants()
            _push(tokens, VOWEL_VISEME[ch], vowel_units, 1.0)
            i += 1
            continue

        # --- consonants ---------------------------------------------------
        if ch in _CONSONANTS:
            pending_consonants += consonant_units
            i += 1
            continue

        i += 1  # anything else (accented letters -> ignored)

    flush_consonants()
    return tokens


def text_to_phonemes(
    text: str,
    *,
    language: str | None = None,
    consonant_weight: float = DEFAULT_CONSONANT_UNITS,
    vowel_units: float = DEFAULT_VOWEL_UNITS,
    include_consonants: bool = True,
) -> list[Phoneme]:
    """Convert a sentence into a viseme sequence.

    ``consonant_weight=0`` produces a "vowels only" timeline (very smooth, less
    accurate); the default keeps short closures so plosives read correctly.
    """
    text = text.strip()
    if not text:
        return []

    language = language or detect_language(text)
    if language.startswith("ja") or _KANA.search(text):
        text = kana_to_romaji(text)
    text = text.lower()

    raw = _tokenise_romaji(
        text,
        consonant_units=consonant_weight,
        vowel_units=vowel_units,
        include_consonants=include_consonants and consonant_weight > 0,
    )

    phonemes = [Phoneme(viseme=v, units=max(u, 0.05), loudness=loud) for v, u, loud in raw]
    return _post_process(phonemes)


def _post_process(phonemes: list[Phoneme]) -> list[Phoneme]:
    """Merge tiny neighbours so the mouth never flaps at 20 Hz."""
    if not phonemes:
        return []

    out: list[Phoneme] = [phonemes[0]]
    for ph in phonemes[1:]:
        last = out[-1]
        if ph.viseme == last.viseme:
            out[-1] = Phoneme(last.viseme, last.units + ph.units, max(last.loudness, ph.loudness))
        elif last.units < 0.12:  # a sliver shorter than ~1/8 vowel: absorb it
            out[-1] = Phoneme(ph.viseme, last.units + ph.units, ph.loudness)
        else:
            out.append(ph)

    # Never start or end on a long closure: trim edge "nn" blocks so audio and
    # mouth start together.
    while out and out[0].viseme == CLOSED:
        out.pop(0)
    while out and out[-1].viseme == CLOSED:
        out.pop()
    return out


__all__ = [
    "CLOSED",
    "DIGRAPHS",
    "VISEMES",
    "VOWEL_VISEME",
    "Phoneme",
    "detect_language",
    "kana_to_romaji",
    "text_to_phonemes",
]
