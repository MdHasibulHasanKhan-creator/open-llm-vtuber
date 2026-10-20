"""The conversation brain: memory injection, language mirroring, summaries.

These tests drive the real orchestrator with a scripted engine, so they cover
the wiring that a pure unit test of ``memory`` or ``quality`` cannot: does what
the user said actually reach the model on the *next* turn, and does the greeting
stay out of the profile?
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.config import CharacterConfig, Settings
from app.llm.base import ChatMessage
from app.pipeline.orchestrator import ConversationOrchestrator


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
    """An engine that records the messages it is handed."""

    name = "scripted"

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = replies or ["[relaxed] Noted, I will keep that in mind."]
        self.seen: list[list[ChatMessage]] = []
        self.context: list[dict] = []
        self.calls = 0

    def set_context(self, context: dict) -> None:
        self.context.append(context)

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        self.seen.append(list(messages))
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        yield reply

    async def health_check(self) -> tuple[bool, str]:
        return True, "scripted"

    async def aclose(self) -> None:
        return None


def _orchestrator(
    scripted: _ScriptedLLM,
    *,
    greeting: str = "",
    character: CharacterConfig | None = None,
) -> tuple[ConversationOrchestrator, _RecordingTransport]:
    settings = Settings(_env_file=None)
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.llm.provider = "echo"
    settings.memory.enabled = True

    transport = _RecordingTransport()
    orchestrator = ConversationOrchestrator(
        settings=settings,
        character=character or CharacterConfig(name="Ai", greeting=greeting),
        transport=transport,  # type: ignore[arg-type]
        session_id="brain-test",
    )
    orchestrator.llm = scripted  # type: ignore[assignment]
    return orchestrator, transport


def _system_text(messages: list[ChatMessage]) -> str:
    return "\n".join(m.content for m in messages if m.role == "system")


@pytest.mark.asyncio
async def test_a_fact_learned_on_turn_one_reaches_the_prompt_on_turn_two():
    scripted = _ScriptedLLM(["[happy] Nice to meet you.", "[relaxed] Good choice."])
    orchestrator, _ = _orchestrator(scripted)
    assert orchestrator.memory is not None

    await orchestrator._run_turn("My name is Rahim and I love coffee.", 1)
    await orchestrator._run_turn("What should I order?", 2)

    # The second turn must carry the facts learned on the first. (The first turn
    # already carries them too - the fact is recorded before the reply is
    # generated, which is what lets her answer "nice to meet you, Rahim".)
    second = _system_text(scripted.seen[1])
    assert "Rahim" in second
    assert "coffee" in second
    # And nothing was invented that was never said.
    assert "Dhaka" not in second


@pytest.mark.asyncio
async def test_the_greeting_is_never_mined_for_facts():
    """The greeting is submitted as user text; it is the *avatar* speaking."""
    scripted = _ScriptedLLM()
    orchestrator, _ = _orchestrator(scripted, greeting="Hi! I love long walks by the sea.")
    assert orchestrator.memory is not None

    await orchestrator.start()

    assert orchestrator.memory.profile.likes == []
    assert orchestrator.memory.profile.turns == 0


@pytest.mark.asyncio
async def test_language_is_mirrored_in_the_prompt():
    scripted = _ScriptedLLM()
    orchestrator, _ = _orchestrator(scripted)
    orchestrator.history.append(ChatMessage("user", "আমি কেমন আছি জানো?"))

    directive = _system_text(orchestrator._messages())
    assert "Bengali" in directive


@pytest.mark.asyncio
async def test_the_offline_brain_is_handed_the_profile():
    scripted = _ScriptedLLM()
    orchestrator, _ = _orchestrator(scripted)
    assert orchestrator.memory is not None
    orchestrator.memory.profile.name = "Ada"

    await orchestrator._run_turn("hello there", 1)

    assert scripted.context, "set_context must be called before streaming"
    assert scripted.context[-1]["profile"]["name"] == "Ada"
    assert scripted.context[-1]["language"] == "en"


@pytest.mark.asyncio
async def test_memory_can_be_switched_off():
    settings = Settings(_env_file=None)
    settings.memory.enabled = False
    orchestrator = ConversationOrchestrator(
        settings=settings,
        character=CharacterConfig(),
        transport=_RecordingTransport(),  # type: ignore[arg-type]
        session_id="no-memory",
    )
    assert orchestrator.memory is None
    orchestrator._observe_user("My name is Rahim")  # must be a harmless no-op
    assert "Rahim" not in _system_text(orchestrator._messages())
