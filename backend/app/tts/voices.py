"""Which voice speaks which language.

Edge is a *many-voice* engine: the voice decides the language, and the same text
read by the wrong one is not "slightly accented", it is unintelligible - Bengali
through ``en-US-AriaNeural`` comes out as English phonemes mashed together.
Since the persona mirrors whatever language the user writes in, a single fixed
``tts.voice`` cannot be right for more than one of them.

So: one female neural voice per language - the persona reads as feminine, and
every entry below was checked against Edge's live voice list (see
``GET /api/voices``). It is only used when the sentence's language does *not*
match the configured voice, so an explicit choice in the persona file or the
settings drawer always wins.

Nothing here is a hard dependency. An unknown language, a missing entry, an
offline voice list or a renamed voice all fall back to the configured voice,
which is exactly the behaviour of every version before this one.

Only Edge needs a map. ``openai`` voices are *styles* (``alloy``, ``nova``) and
one multilingual model covers every language; ``browser`` voices belong to the
browser, which picks from the locale we send in ``tts.start.lang``.
"""

from __future__ import annotations

from ..pipeline.language import primary_subtag

__all__ = ["LANGUAGE_VOICES", "voice_for_language"]

#: Language (ISO 639-1, as returned by ``pipeline.language``) -> Edge voice.
LANGUAGE_VOICES: dict[str, str] = {
    "en": "en-US-AriaNeural",
    "bn": "bn-BD-NabanitaNeural",
    "hi": "hi-IN-SwaraNeural",
    "ur": "ur-PK-UzmaNeural",
    "ar": "ar-SA-ZariyahNeural",
    "fa": "fa-IR-DilaraNeural",
    "ta": "ta-IN-PallaviNeural",
    "te": "te-IN-ShrutiNeural",
    "kn": "kn-IN-SapnaNeural",
    "ml": "ml-IN-SobhanaNeural",
    "gu": "gu-IN-DhwaniNeural",
    "mr": "mr-IN-AarohiNeural",
    "ne": "ne-NP-HemkalaNeural",
    "si": "si-LK-ThiliniNeural",
    "ja": "ja-JP-NanamiNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ko": "ko-KR-SunHiNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "uk": "uk-UA-PolinaNeural",
    "pl": "pl-PL-ZofiaNeural",
    "cs": "cs-CZ-VlastaNeural",
    "hu": "hu-HU-NoemiNeural",
    "ro": "ro-RO-AlinaNeural",
    "el": "el-GR-AthinaNeural",
    "he": "he-IL-HilaNeural",
    "tr": "tr-TR-EmelNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "th": "th-TH-PremwadeeNeural",
    "id": "id-ID-GadisNeural",
    "ms": "ms-MY-YasminNeural",
    "fil": "fil-PH-BlessicaNeural",
    "nl": "nl-NL-FennaNeural",
    "de": "de-DE-KatjaNeural",
    "fr": "fr-FR-DeniseNeural",
    "es": "es-ES-ElviraNeural",
    "it": "it-IT-ElsaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "sv": "sv-SE-SofieNeural",
    "da": "da-DK-ChristelNeural",
    "fi": "fi-FI-NooraNeural",
    "nb": "nb-NO-PernilleNeural",
}


def voice_for_language(language: str) -> str:
    """A voice that speaks ``language``, or ``""`` when we do not know one.

    ``"bn-Latn"`` (Banglish) answers the Bengali voice: romanised Bengali read
    by a Bengali voice is still Bengali, and the orchestrator asks the model to
    answer Banglish in Bengali script so there is something for it to read.
    """
    return LANGUAGE_VOICES.get(primary_subtag(language), "")
