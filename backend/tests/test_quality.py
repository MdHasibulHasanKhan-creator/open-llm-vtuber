"""Reply-quality guard: heuristics, the offline brain, and the retry path.

The user-visible bug these tests lock down was "the avatar just repeats what I
say". It had two causes: an echoing ``echo`` provider selected as a fallback, and
no guard at all on a real model that parrots its prompt.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator

import pytest

from app.config import CharacterConfig, Settings
from app.llm.base import ChatMessage
from app.llm.echo import EchoLLM
from app.pipeline.orchestrator import ConversationOrchestrator
from app.pipeline.quality import (
    content_tokens,
    is_echo,
    is_repetition,
    normalise,
    overlap,
)

USER_TEXT = "I really love drinking coffee in the morning before work"


# --------------------------------------------------------------------------- #
#  Tokenisation
# --------------------------------------------------------------------------- #
def test_content_tokens_drop_stopwords_and_case():
    tokens = content_tokens("I really LOVE that, and so do you.")
    assert "love" in tokens
    assert "really" in tokens
    assert "that" not in tokens  # stop word
    assert "you" not in tokens
    assert "i" not in tokens  # single letters carry no topic


def test_content_tokens_split_cjk_per_character():
    tokens = content_tokens("私はコーヒーが好きです")
    assert "コ" in tokens and "私" in tokens


BENGALI_USER = "আমি প্রতিদিন সকালে কফি খেতে ভালোবাসি"


def test_content_tokens_work_in_bengali():
    """``\\w`` cannot see combining marks, so this used to return an empty set."""
    tokens = content_tokens(BENGALI_USER)
    assert "কফি" in tokens
    assert "সকালে" in tokens
    # Whole words, matras included - and folded the same way the stop-word list is.
    assert normalise("ভালোবাসি") in tokens
    assert "আমি" not in tokens  # a Bengali stop word


def test_bengali_matras_are_never_stripped():
    """Only *Latin* accents fold; an abugida keeps its vowel signs."""
    assert normalise("আমি") == "আমি"
    assert normalise("ভালোবাসি") == normalise("ভালোবাসি")


def test_bengali_parroting_is_caught():
    assert is_echo(BENGALI_USER + "।", BENGALI_USER)
    assert is_echo("তুমি প্রতিদিন সকালে কফি খেতে ভালোবাসো।", BENGALI_USER)
    assert not is_echo("আমি বরং চা পছন্দ করি, কফি আমার জন্য নয়।", BENGALI_USER)


def test_bengali_repetition_is_caught():
    first = "আমি গতকাল পাহাড়ে হেঁটেছিলাম, অনেক কাদা ছিল।"
    assert is_repetition(first, first)
    assert not is_repetition("তুমি কোন গান শুনতে ভালোবাসো?", first)


def test_banglish_is_guarded_too():
    assert is_echo("ami kal bazaar e giyechilam", "ami kal bazaar e giyechilam")
    # Banglish function words carry no topic either.
    assert content_tokens("ami tomake bhalobashi") == {"bhalobashi"}


def test_latin_accents_still_fold():
    assert is_echo("Café au lait!", "I love a cafe au lait")


def test_overlap_is_containment_not_symmetry():
    assert overlap({"a", "b"}, {"b"}) == 0.5
    assert overlap(set(), {"b"}) == 0.0


# --------------------------------------------------------------------------- #
#  Parroting
# --------------------------------------------------------------------------- #
def test_verbatim_repeat_is_an_echo():
    assert is_echo(USER_TEXT + ".", USER_TEXT)


def test_paraphrased_repeat_is_an_echo():
    assert is_echo("So you love drinking coffee in the morning.", USER_TEXT)


def test_a_real_answer_is_not_an_echo():
    assert not is_echo("Coffee is a solid choice. I am more of a tea person.", USER_TEXT)


def test_a_short_reaction_is_not_an_echo():
    # "Yes!" shares nothing, and "sounds lovely" shares one word out of two -
    # neither may trigger a regeneration.
    assert not is_echo("Yes!", USER_TEXT)
    assert not is_echo("That sounds lovely.", USER_TEXT)


def test_a_long_reply_is_never_an_echo():
    long_reply = " ".join([USER_TEXT] * 6) + " but honestly the best part is the quiet."
    assert not is_echo(long_reply, USER_TEXT)


# --------------------------------------------------------------------------- #
#  Repetition
# --------------------------------------------------------------------------- #
def test_repeating_the_previous_reply_is_caught():
    first = "I spent the weekend hiking up a very muddy hill near the coast."
    assert is_repetition(first, first)


def test_a_new_reply_is_not_a_repetition():
    first = "I spent the weekend hiking up a very muddy hill near the coast."
    assert not is_repetition("What kind of music do you listen to?", first)


def test_short_interjections_are_ignored_by_the_repetition_check():
    assert not is_repetition("Okay!", "Okay!")


# --------------------------------------------------------------------------- #
#  The offline brain must not parrot either
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_echo_brain_never_quotes_the_user():
    brain = EchoLLM(Settings(_env_file=None).llm)
    for text in (
        USER_TEXT,
        "hello there",
        "thanks!",
        "Could you explain what a websocket handshake is?",
        "yes",
        "Well, " + "and then we kept walking " * 8,
    ):
        reply = "".join([piece async for piece in brain.stream_chat([ChatMessage("user", text)])])
        assert USER_TEXT not in reply
        assert not is_echo(reply, text), f"echo brain parroted: {reply!r}"


@pytest.mark.asyncio
async def test_echo_brain_does_not_repeat_itself():
    brain = EchoLLM(Settings(_env_file=None).llm)
    replies = [
        "".join([piece async for piece in brain.stream_chat([ChatMessage("user", "I like coffee")])])
        for _ in range(8)
    ]
    assert len(set(replies)) >= 4, "the offline brain looped"
    for earlier, later in itertools.pairwise(replies):
        assert earlier != later, "the offline brain repeated itself back to back"
        assert not is_repetition(later, earlier)


# --------------------------------------------------------------------------- #
#  The guard, end to end through a turn
# --------------------------------------------------------------------------- #
class _RecordingTransport:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.states: list[str] = []

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)

    async def send_bytes(self, payload: bytes) -> None:
        return None

    async def set_state(self, state: str, detail: str = "") -> None:
        self.states.append(state)


class _ScriptedLLM:
    """Replays prepared attempts, recording the messages it was given."""

    name = "scripted"

    def __init__(self, attempts: list[list[str]]) -> None:
        self.attempts = attempts
        self.seen: list[list[ChatMessage]] = []
        self.calls = 0

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        self.seen.append(list(messages))
        script = self.attempts[min(self.calls, len(self.attempts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk

    async def health_check(self) -> tuple[bool, str]:
        return True, "scripted"


def _orchestrator(scripted: _ScriptedLLM) -> tuple[ConversationOrchestrator, _RecordingTransport]:
    settings = Settings(_env_file=None)
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.llm.provider = "echo"
    transport = _RecordingTransport()
    orchestrator = ConversationOrchestrator(
        settings=settings,
        character=CharacterConfig(),
        transport=transport,  # type: ignore[arg-type]
        session_id="test",
    )
    orchestrator.llm = scripted  # type: ignore[assignment]
    return orchestrator, transport


@pytest.mark.asyncio
async def test_parroting_reply_is_regenerated_once():
    scripted = _ScriptedLLM(
        [
            [USER_TEXT + "."],  # attempt 1: a straight copy of the user
            ["[happy] Coffee is a solid choice. I am more of a tea person."],
        ]
    )
    orchestrator, transport = _orchestrator(scripted)

    await orchestrator._run_turn(USER_TEXT, 1)

    types = [message["type"] for message in transport.messages]
    assert "llm.restart" in types, "the client must be told to drop the rejected preview"
    assert types.index("llm.restart") < types.index("llm.done")
    assert scripted.calls == 2, "the guard must regenerate exactly once"

    # Nothing from the rejected attempt may reach TTS.
    spoken = [message["text"] for message in transport.messages if message["type"] == "tts.start"]
    assert spoken, "the good attempt must still be spoken"
    assert all(USER_TEXT not in text for text in spoken)

    # The corrective note is added to the second attempt only, as a system turn.
    retry_messages = scripted.seen[1]
    assert any("rejected" in message.content for message in retry_messages if message.role == "system")
    assert not any("rejected" in message.content for message in scripted.seen[0] if message.role == "system")

    history = [message.content for message in orchestrator.history if message.role == "assistant"]
    assert history and USER_TEXT not in history[-1]


@pytest.mark.asyncio
async def test_a_good_reply_is_not_regenerated():
    scripted = _ScriptedLLM([["[relaxed] Tea, then. I never got the taste for coffee."]])
    orchestrator, transport = _orchestrator(scripted)

    await orchestrator._run_turn(USER_TEXT, 1)

    types = [message["type"] for message in transport.messages]
    assert "llm.restart" not in types
    assert scripted.calls == 1


@pytest.mark.asyncio
async def test_guard_can_be_switched_off():
    scripted = _ScriptedLLM([[USER_TEXT + "."], ["[happy] Something else entirely."]])
    orchestrator, transport = _orchestrator(scripted)
    orchestrator.settings.llm.quality_guard = False

    await orchestrator._run_turn(USER_TEXT, 1)

    types = [message["type"] for message in transport.messages]
    assert "llm.restart" not in types
    assert scripted.calls == 1
