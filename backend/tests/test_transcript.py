"""Input-side transcript gate: noise, speaker echo, and the orchestrator wiring.

The user-visible bug these tests lock down is the other half of "the avatar
talks to itself": with speakers on, the microphone hears the TTS and the model is
asked to answer its own last sentence. The reply guard (``test_quality``) catches
what comes back; this gate stops the garbage going in.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.config import CharacterConfig, Settings, TranscriptConfig
from app.llm.base import ChatMessage
from app.pipeline.orchestrator import ConversationOrchestrator
from app.pipeline.transcript import (
    REASON_LABELS,
    compare_key,
    evaluate,
    normalise,
    similarity,
)
from app.utils.audio import DecodedAudio

SESSION_LINE = "I love rainy mornings with a cup of coffee."
FILLER = TranscriptConfig()


def gate(text: str, **kwargs) -> bool:
    return evaluate(text, FILLER, **kwargs).accepted


# --------------------------------------------------------------------------- #
#  Text helpers
# --------------------------------------------------------------------------- #
def test_normalise_collapses_whitespace_and_fullwidth():
    assert normalise("  Hello,\u3000there \n friend  ") == "Hello, there friend"


def test_compare_key_strips_punctuation_and_case():
    assert compare_key("Thank you, for watching!") == "thank you for watching"


def test_compare_key_keeps_bengali_and_japanese_letters():
    assert compare_key("আমি ভালো আছি।") == "আমি ভালো আছি"
    assert compare_key("私は元気です。") == "私は元気です"


def test_similarity_bounds():
    assert similarity("hello world", "hello world") == 1.0
    assert similarity("hello world", "HELLO, WORLD!") == 1.0
    assert similarity("hello world", "") == 0.0
    assert 0.0 <= similarity("good morning", "good night") < 1.0


# --------------------------------------------------------------------------- #
#  Silence, punctuation, tags
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["", "   ", "...", "؟", "!!!", "。、"])
def test_empty_and_punctuation_only_are_dropped(text):
    verdict = evaluate(text, FILLER)
    assert not verdict
    assert verdict.reason == "empty"


@pytest.mark.parametrize("text", ["[BLANK_AUDIO]", "[music]", "(applause)", "*sigh*", "<noise>"])
def test_tag_only_transcripts_are_dropped(text):
    verdict = evaluate(text, FILLER)
    assert not verdict
    assert verdict.reason == "tag"


# --------------------------------------------------------------------------- #
#  ASR hallucinations
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Thanks for watching!",
        "thank you for watching",
        "Please subscribe.",
        "字幕",
        "ご視聴ありがとうございました",
        "Subtitles by the Amara.org community",
    ],
)
def test_hard_hallucinations_are_dropped_regardless_of_duration(text):
    assert not evaluate(text, FILLER, duration=30.0)
    assert not evaluate(text, FILLER, duration=None)


def test_hard_hallucination_survives_a_word_of_padding():
    verdict = evaluate("Thanks for watching, everyone!", FILLER)
    assert not verdict and verdict.reason == "noise"


def test_hard_hallucination_does_not_swallow_a_real_sentence():
    assert gate("Thanks for watching that film with me, what did you think?")


@pytest.mark.parametrize("text", ["thank you", "Thank you!", "you", "music"])
def test_plausible_short_phrase_dropped_only_when_audio_is_short(text):
    assert not evaluate(text, FILLER, duration=0.4)
    # A real "thank you" takes longer than that to say.
    assert gate(text, duration=1.6)


def test_short_phrase_is_kept_without_duration():
    """Browser ASR reports no duration - and does not hallucinate on silence."""
    assert gate("thank you")


# --------------------------------------------------------------------------- #
#  Real answers must survive
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["no", "ok", "yes", "হ্যাঁ", "はい", "what?", "hmm"])
def test_genuine_short_answers_are_kept(text):
    assert gate(text, duration=0.5)


def test_single_stray_character_is_dropped():
    assert not evaluate("a", FILLER, duration=0.5)


def test_a_long_bengali_sentence_is_kept():
    assert gate("আমি আজ সকালে চা খেয়েছি, তুমি কী করছ?")


# --------------------------------------------------------------------------- #
#  Speaker echo
# --------------------------------------------------------------------------- #
def test_echo_of_the_avatar_is_dropped():
    verdict = evaluate(SESSION_LINE, FILLER, recent_assistant=[SESSION_LINE], duration=2.0)
    assert not verdict
    assert verdict.reason == "echo"
    assert verdict.score >= FILLER.echo_similarity


def test_echo_detection_tolerates_asr_sloppiness():
    heard = "I love rainy morning with a cup of coffee"
    assert not evaluate(heard, FILLER, recent_assistant=[SESSION_LINE])


def test_the_users_own_new_sentence_is_not_an_echo():
    assert gate("Actually I prefer tea in the afternoon.", recent_assistant=[SESSION_LINE])


def test_echo_guard_can_be_switched_off():
    config = TranscriptConfig(echo_guard=False)
    assert evaluate(SESSION_LINE, config, recent_assistant=[SESSION_LINE])


def test_echo_window_limits_how_far_back_it_looks():
    config = TranscriptConfig(echo_window=1)
    older = "I love rainy mornings with a cup of coffee."
    newer = "Say something else entirely, please."
    assert evaluate(older, config, recent_assistant=[newer, older])
    assert not evaluate(newer, config, recent_assistant=[newer, older])


# --------------------------------------------------------------------------- #
#  Duplicate submit
# --------------------------------------------------------------------------- #
def test_dedupe_is_off_by_default():
    assert gate("tell me a joke", recent_user=["tell me a joke"])


def test_dedupe_drops_an_immediate_repeat_when_enabled():
    config = TranscriptConfig(dedupe=True)
    assert not evaluate("tell me a joke", config, recent_user=["tell me a joke"])


# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #
def test_gate_can_be_disabled_entirely():
    assert evaluate("[BLANK_AUDIO]", TranscriptConfig(enabled=False))


def test_extra_noise_phrases_are_honoured():
    config = TranscriptConfig(extra_noise_phrases=["i love you too"])
    assert not evaluate("I love you too.", config)
    assert evaluate("I love you too.", TranscriptConfig()).accepted


def test_min_chars_is_configurable():
    assert evaluate("no", TranscriptConfig(min_chars=4)).reason == "short"


def test_every_rejection_reason_is_reachable_and_labelled():
    """The reason codes are part of the wire protocol - keep them honest."""
    observed = {
        evaluate("", FILLER).reason,
        evaluate("[music]", FILLER).reason,
        evaluate("a", FILLER, duration=1.0).reason,
        evaluate("Thanks for watching!", FILLER).reason,
        evaluate(SESSION_LINE, FILLER, recent_assistant=[SESSION_LINE]).reason,
        evaluate("tell me a joke", TranscriptConfig(dedupe=True), recent_user=["tell me a joke"]).reason,
    }
    assert observed == {"empty", "tag", "short", "noise", "echo", "duplicate"}
    # The client renders its own copy of these labels, so a code without one
    # would show up as a bare identifier in the UI.
    assert set(REASON_LABELS) >= observed


# --------------------------------------------------------------------------- #
#  End to end through the orchestrator
# --------------------------------------------------------------------------- #
class _Transport:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.states: list[str] = []

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)

    async def send_bytes(self, payload: bytes) -> None:
        return None

    async def set_state(self, state: str, detail: str = "") -> None:
        self.states.append(state)

    def of_type(self, kind: str) -> list[dict]:
        return [message for message in self.messages if message["type"] == kind]


class _FixedASR:
    name = "fixed"

    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, audio: DecodedAudio) -> str:
        return self.text

    async def aclose(self) -> None:
        return None


def _audio(seconds: float) -> DecodedAudio:
    return DecodedAudio(np.zeros(int(16000 * seconds), dtype=np.float32), 16000)


def _orchestrator() -> tuple[ConversationOrchestrator, _Transport]:
    settings = Settings(_env_file=None)
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.llm.provider = "echo"
    transport = _Transport()
    orchestrator = ConversationOrchestrator(
        settings=settings,
        character=CharacterConfig(),
        transport=transport,  # type: ignore[arg-type]
        session_id="test",
    )
    return orchestrator, transport


@pytest.mark.asyncio
async def test_server_asr_echo_never_reaches_the_llm():
    orchestrator, transport = _orchestrator()
    orchestrator.asr = _FixedASR(SESSION_LINE)  # type: ignore[assignment]
    orchestrator.history.append(ChatMessage("assistant", SESSION_LINE))

    await orchestrator.submit_audio(_audio(2.0))

    assert transport.of_type("user.text") == []
    assert orchestrator._turn_task is None, "no turn may be started for an echo"
    finals = transport.of_type("asr.final")
    assert finals and finals[-1]["reason"] == "echo"
    assert finals[-1]["text"] == ""
    assert transport.states[-1] == "idle"


@pytest.mark.asyncio
async def test_server_asr_noise_never_reaches_the_llm():
    orchestrator, transport = _orchestrator()
    orchestrator.asr = _FixedASR("Thanks for watching!")  # type: ignore[assignment]

    await orchestrator.submit_audio(_audio(0.4))

    assert transport.of_type("user.text") == []
    assert transport.of_type("asr.final")[-1]["reason"] == "noise"


@pytest.mark.asyncio
async def test_browser_asr_transcript_is_gated_when_marked_as_asr():
    orchestrator, transport = _orchestrator()
    orchestrator.history.append(ChatMessage("assistant", SESSION_LINE))

    await orchestrator.submit_text(SESSION_LINE, source="asr")

    assert transport.of_type("user.text") == []
    assert transport.of_type("asr.final")[-1]["reason"] == "echo"


@pytest.mark.asyncio
async def test_typed_text_bypasses_the_gate():
    orchestrator, transport = _orchestrator()
    orchestrator.history.append(ChatMessage("assistant", SESSION_LINE))

    await orchestrator.submit_text(SESSION_LINE)  # source defaults to "typed"

    assert transport.of_type("user.text"), "a typed message is taken at face value"
    if orchestrator._turn_task is not None:
        await orchestrator._turn_task


@pytest.mark.asyncio
async def test_accepted_server_transcript_starts_a_turn():
    orchestrator, transport = _orchestrator()
    orchestrator.asr = _FixedASR("Tell me about the weather in Dhaka.")  # type: ignore[assignment]

    await orchestrator.submit_audio(_audio(2.5))

    assert transport.of_type("asr.final")[-1]["reason"] == ""
    assert transport.of_type("user.text"), "an accepted transcript must become a user turn"
    if orchestrator._turn_task is not None:
        await orchestrator._turn_task
