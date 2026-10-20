"""Language handling: script detection, Banglish, and the voice that follows.

The user-visible bugs this locks down:

* writing Bengali in Latin letters ("ami bhalo achi") was read as English, so the
  avatar answered in English and sounded like it had not listened;
* a Bengali reply was spoken by ``en-US-AriaNeural`` and came out as noise;
* the reply-quality guard silently did nothing in Bengali (``content_tokens``
  returned an empty set, because ``\\w`` cannot see combining marks).

The last one lives with the other guard tests in ``test_quality.py``; this file
covers the naming of a language and everything downstream of that name.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.config import CharacterConfig, Settings
from app.llm.base import ChatMessage
from app.pipeline.language import (
    BANGLISH,
    LANGUAGE_NAMES,
    LOCALES,
    detect_language,
    is_banglish,
    locale_for,
    primary_subtag,
)
from app.pipeline.orchestrator import ConversationOrchestrator
from app.tts.base import SynthesisResult
from app.tts.edge import EdgeTTS
from app.tts.voices import LANGUAGE_VOICES, voice_for_language

BENGALI = "আমি প্রতিদিন সকালে কফি খেতে ভালোবাসি"
BANGLISH_TEXT = "ami bhalo achi"


# --------------------------------------------------------------------------- #
#  Naming the language
# --------------------------------------------------------------------------- #
def test_bengali_script_is_bengali():
    assert detect_language(BENGALI) == "bn"


def test_english_is_english():
    assert detect_language("Where did you put the kettle?") == "en"


def test_romanised_bengali_is_bengali_not_english():
    assert detect_language(BANGLISH_TEXT) == BANGLISH
    assert detect_language("ki koro tumi") == BANGLISH
    assert detect_language("tumi kemon acho bhai") == BANGLISH


def test_a_single_stray_marker_never_flips_english():
    """"ki" alone, or a word that merely looks Bengali, is not evidence."""
    assert not is_banglish("ki")
    assert detect_language("ki") == "en"
    assert detect_language("Ami is a nice name") == "en"
    assert detect_language("I asked her about the keno flower") == "en"


def test_punctuation_and_digits_are_not_a_language():
    assert detect_language("...") == ""
    assert detect_language("42") == ""
    assert detect_language("   ") == ""


def test_the_shared_arabic_block_is_told_apart():
    """Arabic, Persian and Urdu share one Unicode block; they are not one language."""
    assert detect_language("ما اسمك؟ أنا بخير شكرا") == "ar"
    assert detect_language("سلام! حال شما چطور است؟ خیلی خوبم") == "fa"
    assert detect_language("آپ کیسے ہیں؟ میں ٹھیک ہوں") == "ur"


def test_every_known_language_has_a_spoken_locale():
    for code in LANGUAGE_NAMES:
        assert locale_for(code), code


# --------------------------------------------------------------------------- #
#  Locales (what the browser and the wire use)
# --------------------------------------------------------------------------- #
def test_locales_collapse_scripts_onto_the_spoken_language():
    assert locale_for("bn") == "bn-BD"
    assert locale_for(BANGLISH) == "bn-BD"
    assert locale_for("en") == "en-US"
    assert locale_for("zh-Hans") == "zh-CN"


def test_a_real_region_is_left_alone():
    assert locale_for("en-GB") == "en-GB"
    assert locale_for("pt-BR") == "pt-BR"


def test_an_unknown_language_is_passed_through():
    # Better the browser tries `sv` itself than that we hand it an English voice.
    assert locale_for("sv") == "sv-SE"
    assert locale_for("xx") == "xx"
    assert locale_for("") == "en-US"
    assert locale_for("", fallback="ja-JP") == "ja-JP"


def test_primary_subtag_reads_languages_and_voice_names():
    assert primary_subtag("bn-BD") == "bn"
    assert primary_subtag("en-US-AriaNeural") == "en"
    assert primary_subtag("bn-BD-NabanitaNeural") == "bn"
    assert primary_subtag("") == ""


# --------------------------------------------------------------------------- #
#  Voices (what actually gets spoken)
# --------------------------------------------------------------------------- #
def test_voice_map_speaks_banglish_with_a_bengali_voice():
    assert voice_for_language("bn") == LANGUAGE_VOICES["bn"]
    assert voice_for_language(BANGLISH) == LANGUAGE_VOICES["bn"]
    assert voice_for_language("en-GB") == "en-US-AriaNeural"


def test_voice_map_returns_nothing_rather_than_guessing():
    assert voice_for_language("") == ""
    assert voice_for_language("xx") == ""


def test_voice_map_is_one_female_voice_per_language():
    assert len(set(LANGUAGE_VOICES.values())) == len(LANGUAGE_VOICES)
    for code, voice in LANGUAGE_VOICES.items():
        assert voice.startswith(f"{code}-"), voice
        assert voice.endswith("Neural"), voice
    # Every locale we promise to *speak* is also one we can voice.
    assert set(LANGUAGE_VOICES) <= set(LOCALES)


def test_only_edge_claims_a_voice_per_language():
    settings = Settings(_env_file=None)
    assert EdgeTTS(settings.tts).voice_for_language("bn") == LANGUAGE_VOICES["bn"]

    from app.tts.browser import BrowserTTS
    from app.tts.openai import OpenAITTS

    assert BrowserTTS(settings.tts).voice_for_language("bn") == ""
    assert OpenAITTS(settings.tts).voice_for_language("bn") == ""


# --------------------------------------------------------------------------- #
#  Downstream: the turn that speaks Bengali
# --------------------------------------------------------------------------- #
class _Frames:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.frames.append(message)

    async def send_bytes(self, payload: bytes) -> None:
        return None

    async def set_state(self, state: str, detail: str = "") -> None:
        return None

    def tts_starts(self) -> list[dict]:
        return [frame for frame in self.frames if frame.get("type") == "tts.start"]


class _ScriptedLLM:
    name = "scripted"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen: list[list[ChatMessage]] = []

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        self.seen.append(list(messages))
        yield self.reply

    async def health_check(self) -> tuple[bool, str]:
        return True, "scripted"

    async def aclose(self) -> None:
        return None


class _StubEdge(EdgeTTS):
    """Edge's real voice map, without the network round-trip."""

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, text, *, voice=None, rate=None, pitch=None):  # type: ignore[no-untyped-def]
        self.calls.append((text, voice or ""))
        return SynthesisResult(
            audio=None,
            audio_format=None,
            sample_rate=0,
            estimated_duration=1.5,
            voice=voice or "",
        )


def _orchestrator(reply: str) -> tuple[ConversationOrchestrator, _Frames, _StubEdge, _ScriptedLLM]:
    settings = Settings(_env_file=None)
    settings.tts.engine = "edge"
    settings.asr.engine = "browser"
    settings.llm.provider = "echo"
    settings.memory.enabled = False

    transport = _Frames()
    orchestrator = ConversationOrchestrator(
        settings=settings,
        character=CharacterConfig(name="Ai", language="en"),
        transport=transport,  # type: ignore[arg-type]
    )
    stub = _StubEdge(settings.tts)
    scripted = _ScriptedLLM(reply)
    orchestrator.tts = stub  # type: ignore[assignment]
    orchestrator.llm = scripted  # type: ignore[assignment]
    return orchestrator, transport, stub, scripted


@pytest.mark.asyncio
async def test_a_bengali_message_is_answered_by_a_bengali_voice():
    orchestrator, transport, stub, _ = _orchestrator("[happy] অবশ্যই, আমি ভালো আছি।")

    await orchestrator._run_turn(BENGALI, 1)

    frames = transport.tts_starts()
    assert frames, "the reply must still be spoken"
    for frame in frames:
        assert frame["lang"] == "bn-BD"
        assert frame["voice"] == LANGUAGE_VOICES["bn"]
    assert stub.calls and stub.calls[0][1] == LANGUAGE_VOICES["bn"]


@pytest.mark.asyncio
async def test_banglish_is_answered_by_a_bengali_voice_too():
    orchestrator, transport, _, _ = _orchestrator("[relaxed] আমি ভালো আছি, ধন্যবাদ!")

    await orchestrator._run_turn(BANGLISH_TEXT, 1)

    frames = transport.tts_starts()
    assert frames and frames[0]["lang"] == "bn-BD"
    assert frames[0]["voice"] == LANGUAGE_VOICES["bn"]


@pytest.mark.asyncio
async def test_english_keeps_the_configured_voice():
    orchestrator, transport, stub, _ = _orchestrator("[happy] Tea, then. I never got the taste for coffee.")

    await orchestrator._run_turn("I love coffee in the morning", 1)

    frames = transport.tts_starts()
    assert frames[0]["lang"] == "en-US"
    assert frames[0]["voice"] == "en-US-AriaNeural"
    assert stub.calls[0][1] == "en-US-AriaNeural"


@pytest.mark.asyncio
async def test_a_voice_chosen_for_the_language_is_never_overridden():
    """If the user picked a Bengali voice, Bengali stays *their* voice."""
    orchestrator, transport, _, _ = _orchestrator("[happy] অবশ্যই।")
    orchestrator.character = CharacterConfig(language="en", tts_voice="bn-BD-PradeepNeural")

    await orchestrator._run_turn(BENGALI, 1)

    assert transport.tts_starts()[0]["voice"] == "bn-BD-PradeepNeural"


@pytest.mark.asyncio
async def test_auto_voice_can_be_switched_off():
    orchestrator, transport, _, _ = _orchestrator("[happy] অবশ্যই।")
    orchestrator.settings.tts.auto_voice = False

    await orchestrator._run_turn(BENGALI, 1)

    frame = transport.tts_starts()[0]
    assert frame["voice"] == "en-US-AriaNeural"  # the configured voice, unchanged
    assert frame["lang"] == "bn-BD"  # ...but the client still knows the language


@pytest.mark.asyncio
async def test_a_message_with_no_letters_falls_back_to_the_persona_language():
    orchestrator, transport, _, _ = _orchestrator("[happy] Ahem.")
    orchestrator.character = CharacterConfig(language="bn")

    await orchestrator._run_turn("???", 1)

    assert transport.tts_starts()[0]["lang"] == "bn-BD"


def test_the_banglish_directive_asks_for_bengali_script():
    orchestrator, _, _, _ = _orchestrator("x")
    orchestrator.history.append(ChatMessage("user", BANGLISH_TEXT))

    directive = orchestrator._language_directive()
    assert "Banglish" in directive
    assert "বাংলা" in directive
    assert "Do not answer in English" in directive


def test_a_scripted_language_names_itself_in_the_prompt():
    orchestrator, _, _, _ = _orchestrator("x")
    orchestrator.history.append(ChatMessage("user", BENGALI))

    assert "Bengali (বাংলা)" in orchestrator._language_directive()
