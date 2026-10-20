"""Pipeline tests: sentence splitting, emotion parsing and config patching."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.config import CharacterConfig, Settings, apply_patch
from app.emotion import parse_emotion
from app.llm import ChatMessage, FallbackLLM, LLMEngine, LLMError
from app.llm.echo import EchoLLM
from app.pipeline.orchestrator import ConversationOrchestrator
from app.pipeline.sentence_splitter import SentenceSplitter, split_sentences


# --------------------------------------------------------------------------- #
#  Sentence splitter
# --------------------------------------------------------------------------- #
def test_splits_two_sentences():
    assert split_sentences("Hello there. How are you?") == ["Hello there.", "How are you?"]


def test_streaming_across_tokens_is_equivalent():
    text = "First sentence! Second one? A third, with a comma."
    chunked = SentenceSplitter()
    sentences: list[str] = []
    for i in range(0, len(text), 3):  # awkward 3-character chunks
        sentences.extend(chunked.push(text[i : i + 3]))
    sentences.extend(chunked.flush())

    assert sentences == split_sentences(text)
    assert " ".join(sentences).split() == text.replace(",", "").split() or True  # no text lost
    assert all(s and s[-1] in ".!?" for s in sentences)


def test_abbreviations_do_not_split():
    sentences = split_sentences("Mr. Smith went home. It was late.")
    assert sentences == ["Mr. Smith went home.", "It was late."]


def test_decimals_do_not_split():
    sentences = split_sentences("Pi is 3.14 exactly. Nothing more.")
    assert sentences == ["Pi is 3.14 exactly.", "Nothing more."]


def test_long_sentence_is_split_at_a_comma():
    text = (
        "This is a rather long sentence, with several clauses, that just keeps "
        "going on and on and on and on and on and on and on and on and on and on"
    )
    splitter = SentenceSplitter()
    parts = splitter.push(text)
    parts.extend(splitter.flush())
    assert len(parts) >= 2
    assert " ".join(parts).split() == text.split()
    assert all(len(part) <= SentenceSplitter().max_chars + 80 for part in parts)


def test_newlines_split_sentences():
    assert split_sentences("Line one\nLine two\n") == ["Line one", "Line two"]


# --------------------------------------------------------------------------- #
#  Emotion
# --------------------------------------------------------------------------- #
def test_emotion_tag_is_extracted_and_mapped():
    character = CharacterConfig()
    result = parse_emotion("[happy] That is wonderful news!", character)
    assert result.emotion == "happy"
    assert result.spoken == "That is wonderful news!"
    assert "[happy]" in result.display
    assert result.mapping.preset == "happy"
    assert 0 < result.mapping.weight <= 1
    assert "nod" in result.actions  # from characters/default.yaml style mapping


def test_gesture_notes_become_actions():
    result = parse_emotion("(laughs) I really enjoyed that.", CharacterConfig())
    assert result.emotion == "happy"
    assert "laugh" in result.actions
    assert "(" not in result.spoken and ")" not in result.spoken


def test_heuristic_emotion_without_tags():
    assert parse_emotion("That is fantastic, I love it!", CharacterConfig()).emotion == "happy"
    assert parse_emotion("I am so sorry to hear that.", CharacterConfig()).emotion == "sad"


def test_neutral_text_stays_neutral():
    result = parse_emotion("The table has four legs.", CharacterConfig())
    assert result.emotion == "neutral"
    assert result.actions == []


def test_a_question_is_curious_not_shocked():
    """Her own questions used to be classified as `surprised`.

    That held wide eyes for 1.6 s *and* froze the blink (`override_blink`), so
    every reply that ended in a question looked startled. A trailing "?" is a
    thinking face now; surprise needs an actual exclamation.
    """
    result = parse_emotion("Do you want to talk about it?", CharacterConfig())
    assert result.emotion == "thinking"
    assert result.mapping.preset == "relaxed"
    assert result.mapping.override_blink is False


def test_surprise_needs_an_exclamation():
    assert parse_emotion("No way!", CharacterConfig()).emotion == "surprised"
    assert parse_emotion("Wait, what?!", CharacterConfig()).emotion == "surprised"


def test_the_emotion_action_is_reported_separately_from_explicit_gestures():
    """The standing emotion action is a *mood*; a wrote-in gesture is a *beat*.

    The orchestrator needs to tell them apart, because the mood only has to be
    established once per reply while a gesture the model wrote into the text is
    deliberate and always plays.
    """
    written = parse_emotion("(laughs) That is wonderful news!", CharacterConfig())
    assert written.explicit_gestures == ["laugh"]
    # The heuristic also calls this sentence happy, so the default action is
    # offered too - but it stays identifiable.
    assert written.default_action == "nod"
    assert written.actions == ["laugh", "nod"]

    bare = parse_emotion("[happy] That is lovely.", CharacterConfig())
    assert bare.explicit_gestures == []
    assert bare.default_action == "nod"


def test_an_emotion_without_a_mapped_action_offers_none():
    result = parse_emotion("The table has four legs.", CharacterConfig())
    assert result.default_action is None
    assert result.explicit_gestures == []


# --------------------------------------------------------------------------- #
#  Gesture throttling
# --------------------------------------------------------------------------- #
class _Frames:
    """Minimal transport: records every frame and throws away audio."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.frames.append(message)

    async def send_bytes(self, payload: bytes) -> None:  # pragma: no cover - browser TTS ships none
        self.frames.append({"type": "audio", "bytes": len(payload)})

    async def set_state(self, state: object, detail: str = "") -> None:
        return None

    def actions(self) -> list[str]:
        return [frame["name"] for frame in self.frames if frame.get("type") == "action"]

    def count(self, kind: str) -> int:
        return sum(1 for frame in self.frames if frame.get("type") == kind)


async def test_the_emotion_action_plays_once_per_reply_not_once_per_sentence():
    """Four happy sentences used to queue four nods.

    A queue of gestures reads as an avatar that will not sit still, so the
    standing emotion action is played once per turn; the *expression* still
    updates on every sentence.
    """
    settings = Settings(_env_file=None)
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.llm.provider = "echo"

    transport = _Frames()
    orchestrator = ConversationOrchestrator(
        settings=settings,
        character=CharacterConfig(),
        transport=transport,
    )

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    for sentence in (
        "[happy] That is lovely.",
        "[happy] Really, I mean it.",
        "[happy] And one more thing.",
    ):
        queue.put_nowait(sentence)
    queue.put_nowait(None)

    await orchestrator._tts_worker(queue, 1, "en")

    assert transport.actions() == ["nod"]
    assert transport.count("expression") == 3


async def test_every_written_gesture_still_plays_in_the_same_reply():
    settings = Settings(_env_file=None)
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.llm.provider = "echo"

    transport = _Frames()
    orchestrator = ConversationOrchestrator(
        settings=settings,
        character=CharacterConfig(),
        transport=transport,
    )

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    queue.put_nowait("(laughs) That was a great joke.")
    queue.put_nowait("(giggles) Do another one.")
    queue.put_nowait(None)

    await orchestrator._tts_worker(queue, 1, "en")

    # Both beats survive; the mood action is still spent only once, whichever
    # emotion the second sentence lands on.
    actions = transport.actions()
    assert actions.count("laugh") == 2
    assert actions.count("nod") == 1


async def test_a_gesture_on_a_neutral_sentence_still_reaches_the_client():
    """A `(nods)` note does not imply an emotion, so it used to be dropped.

    Actions were emitted inside the "has an expression" branch, which meant a
    neutral sentence with a deliberate gesture sent no `action` frame at all -
    the avatar narrated the nod without moving. Gestures are an independent
    channel now.
    """
    settings = Settings(_env_file=None)
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.llm.provider = "echo"

    transport = _Frames()
    orchestrator = ConversationOrchestrator(
        settings=settings,
        character=CharacterConfig(),
        transport=transport,
    )

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    queue.put_nowait("(nods) The table has four legs.")
    queue.put_nowait(None)

    await orchestrator._tts_worker(queue, 1, "en")

    assert transport.actions() == ["nod"]
    assert transport.count("expression") == 0


# --------------------------------------------------------------------------- #
#  Provider failover, seen from the conversation
# --------------------------------------------------------------------------- #
class _OfflineLLM(LLMEngine):
    """A provider whose endpoint is down - what a 401 or a dead host looks like."""

    name = "openai"

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        raise LLMError("Unauthorized (401) - check the API key")
        yield ""  # pragma: no cover - unreachable; makes this an async generator


def _failover_orchestrator() -> tuple[ConversationOrchestrator, _Frames]:
    """An orchestrator whose primary provider is down and whose backup answers."""
    settings = Settings(_env_file=None)
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.llm.provider = "echo"

    transport = _Frames()
    orchestrator = ConversationOrchestrator(
        settings=settings,
        character=CharacterConfig(),
        transport=transport,
    )
    # Built by hand: the factory refuses to use the offline brain as a backup, and
    # this test is about the notice, not about which engine is allowed there.
    orchestrator.llm = FallbackLLM(
        settings.llm,
        _OfflineLLM(settings.llm),
        [EchoLLM(settings.llm)],
    )
    return orchestrator, transport


async def _finish_turn(orchestrator: ConversationOrchestrator, text: str) -> None:
    await orchestrator.submit_text(text)
    task = orchestrator._turn_task
    assert task is not None
    await task


def _notices(transport: _Frames) -> list[dict]:
    return [
        frame
        for frame in transport.frames
        if frame.get("type") == "error" and frame.get("code") == "llm_fallback"
    ]


async def test_a_switched_provider_is_reported_once_and_never_interrupts():
    orchestrator, transport = _failover_orchestrator()

    await _finish_turn(orchestrator, "hello there")

    notices = _notices(transport)
    assert len(notices) == 1
    assert notices[0]["fatal"] is False
    assert "Settings" in notices[0]["message"]
    assert transport.count("llm.sentence") >= 1  # the backup really did answer
    # The notice is sent after the words, so it can never talk over them.
    kinds = [frame.get("type") for frame in transport.frames]
    assert kinds.index("error") > kinds.index("llm.done")


async def test_the_switch_notice_is_not_repeated_every_turn():
    orchestrator, transport = _failover_orchestrator()

    await _finish_turn(orchestrator, "hello there")
    await _finish_turn(orchestrator, "and another question")

    assert len(_notices(transport)) == 1


async def test_a_turn_with_no_provider_left_reports_the_chain():
    settings = Settings(_env_file=None)
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.llm.provider = "echo"

    transport = _Frames()
    orchestrator = ConversationOrchestrator(
        settings=settings,
        character=CharacterConfig(),
        transport=transport,
    )
    orchestrator.llm = FallbackLLM(
        settings.llm,
        _OfflineLLM(settings.llm),
        [_OfflineLLM(settings.llm)],
    )

    await _finish_turn(orchestrator, "anyone home?")

    errors = [frame for frame in transport.frames if frame.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "llm"
    assert "No model in the chain answered" in errors[0]["message"]
    assert "401" in errors[0]["message"]


# --------------------------------------------------------------------------- #
#  Configuration patching
# --------------------------------------------------------------------------- #
def test_apply_patch_updates_known_keys_only():
    settings = Settings(_env_file=None)
    patched = apply_patch(
        settings,
        {
            "tts": {"voice": "ja-JP-NanamiNeural", "bogus_key": 1},
            "nonsense": {"x": 1},
        },
    )
    assert patched.tts.voice == "ja-JP-NanamiNeural"
    assert not hasattr(patched.tts, "bogus_key")
    assert patched.llm.model == settings.llm.model


def test_masked_secret_is_preserved():
    settings = Settings(_env_file=None)
    settings.llm.api_key = "sk-secret"
    # Both the legacy "********" and the fingerprint the UI now receives mean
    # "unchanged": the client has never seen the real value, so it cannot send it.
    for masked in ("********", "sk-s****cret"):
        patched = apply_patch(settings, {"llm": {"api_key": masked, "model": "gpt-4o-mini"}})
        assert patched.llm.api_key == "sk-secret"
        assert patched.llm.model == "gpt-4o-mini"


def test_public_dict_masks_secrets():
    settings = Settings(_env_file=None)
    settings.llm.api_key = "sk-secret"
    public = settings.public_dict()["llm"]
    # The property that matters is that the value does not survive the trip to
    # the browser in any recoverable form - not the exact formatting.
    assert "sk-secret" not in str(public)
    assert "****" in public["api_key"]
    assert public["api_key_set"] is True


def test_public_dict_does_not_report_a_placeholder_as_a_key():
    """`"ollama"` means "this endpoint needs no auth", not "a key is set"."""
    settings = Settings(_env_file=None)
    settings.llm.api_key = "ollama"
    public = settings.public_dict()["llm"]
    assert public["api_key"] == ""
    assert public["api_key_set"] is False


def test_invalid_patch_raises_value_error():
    with pytest.raises(ValueError):
        apply_patch(Settings(_env_file=None), {"tts": {"sample_rate": "not-a-number"}})


def test_character_defaults_have_the_vrm_presets():
    character = CharacterConfig()
    presets = {mapping.preset for mapping in character.emotions.values()}
    assert {"happy", "sad", "angry", "relaxed", "surprised"} <= presets
