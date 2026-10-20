"""Provider failover: the rules that keep a backup from becoming a lie.

The behaviour under test is the promise made in ``app/llm/fallback.py``: a reply
is never spliced from two models, the offline brain is never a link, a dead
endpoint is retried on a cooldown rather than every turn, and the credential of
one provider is never handed to another.

The engines here are scripted fakes, so no test touches the network.

check-secrets: allow-file -- the one key-shaped string is a deliberate fake.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.config import FallbackTarget, LLMConfig
from app.llm import ChatMessage, FallbackLLM, LLMEngine, LLMError, create_llm
from app.llm.factory import _target_config

#: Key-shaped and fake: it exists so the tests can assert it never travels.
FAKE_KEY = "gsk_ZZ11yy22xx33ww44vv55uu66tt77ss88rr99"


class _Scripted(LLMEngine):
    """A provider that follows a script: strings are yielded, errors are raised."""

    def __init__(self, name: str, *script: object) -> None:
        super().__init__(
            LLMConfig(provider="openai", model=f"{name}-model", base_url="http://127.0.0.1:9/v1")
        )
        self.name = name
        self.script = list(script)
        self.attempts = 0
        self.closed = False
        self.context: dict[str, Any] | None = None

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        self.attempts += 1
        for step in self.script:
            if isinstance(step, Exception):
                raise step
            yield step

    async def health_check(self) -> tuple[bool, str]:
        first = self.script[0] if self.script else ""
        if isinstance(first, Exception):
            return False, str(first)
        return True, f"{self.name} ready"

    def set_context(self, context: dict[str, Any]) -> None:
        self.context = context

    async def aclose(self) -> None:
        self.closed = True


def _config(**overrides: Any) -> LLMConfig:
    base: dict[str, Any] = {
        "provider": "openai",
        "model": "qwen/qwen3.8-27b",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "ollama",
    }
    base.update(overrides)
    return LLMConfig(**base)


async def _collect(llm: LLMEngine, *, temperature: float | None = None) -> str:
    parts: list[str] = []
    async with contextlib.aclosing(
        llm.stream_chat([ChatMessage("user", "hi")], temperature=temperature)
    ) as stream:
        async for delta in stream:
            parts.append(delta)
    return "".join(parts)


# --------------------------------------------------------------------------- #
#  Happy path
# --------------------------------------------------------------------------- #
async def test_the_primary_answers_and_no_switch_is_reported():
    primary = _Scripted("openai", "hello ")
    backup = _Scripted("ollama", "should not be used")
    llm = FallbackLLM(_config(), primary, [backup])

    assert await _collect(llm) == "hello "
    assert llm.name == "openai"
    assert llm.using_fallback is False
    assert llm.take_event() is None
    assert backup.attempts == 0


async def test_a_dead_primary_fails_over_before_the_first_token():
    primary = _Scripted("openai", LLMError("Cannot reach https://api.groq.com/openai/v1"))
    backup = _Scripted("ollama", "hi from your local model")
    llm = FallbackLLM(_config(), primary, [backup])

    assert await _collect(llm) == "hi from your local model"
    assert llm.name == "ollama"
    assert llm.using_fallback is True

    event = llm.take_event()
    assert event is not None
    assert "openai" in event.from_label and "ollama" in event.to_label
    assert "Settings" in event.message()
    assert llm.take_event() is None  # announced once per switch, not per turn


async def test_a_switch_is_not_re_announced_on_the_next_turn():
    primary = _Scripted("openai", LLMError("down"))
    backup = _Scripted("ollama", "local")
    llm = FallbackLLM(_config(), primary, [backup])

    await _collect(llm)
    assert llm.take_event() is not None

    await _collect(llm)
    assert llm.take_event() is None


# --------------------------------------------------------------------------- #
#  The rule that matters: never splice a reply
# --------------------------------------------------------------------------- #
async def test_a_failure_after_the_first_token_is_never_spliced():
    primary = _Scripted("openai", "the answer is ", LLMError("connection reset"))
    backup = _Scripted("ollama", "a different answer entirely")
    llm = FallbackLLM(_config(), primary, [backup])

    parts: list[str] = []
    with pytest.raises(LLMError):
        async with contextlib.aclosing(llm.stream_chat([ChatMessage("user", "hi")])) as stream:
            async for delta in stream:
                parts.append(delta)

    assert parts == ["the answer is "]
    assert backup.attempts == 0  # half a reply is already on the wire
    assert llm.using_fallback is False


async def test_every_link_failing_reports_one_actionable_error():
    llm = FallbackLLM(
        _config(),
        _Scripted("openai", LLMError("Unauthorized (401) - check the API key")),
        [_Scripted("ollama", LLMError("Cannot reach http://127.0.0.1:11434"))],
    )

    with pytest.raises(LLMError) as caught:
        await _collect(llm)

    text = str(caught.value)
    assert "openai" in text and "ollama" in text
    assert "401" in text and "11434" in text


# --------------------------------------------------------------------------- #
#  Cooldowns
# --------------------------------------------------------------------------- #
async def test_a_failed_link_is_skipped_while_it_cools_down():
    primary = _Scripted("openai", LLMError("down"))
    backup = _Scripted("ollama", "local answer")
    llm = FallbackLLM(_config(fallback_cooldown=60.0), primary, [backup])

    assert await _collect(llm) == "local answer"
    assert await _collect(llm) == "local answer"

    assert primary.attempts == 1  # one timeout per minute, not one per turn
    assert backup.attempts == 2


async def test_the_primary_is_retried_once_the_cooldown_lapses():
    primary = _Scripted("openai", LLMError("down"))
    backup = _Scripted("ollama", "local answer")
    llm = FallbackLLM(_config(fallback_cooldown=0.05), primary, [backup])

    assert await _collect(llm) == "local answer"
    assert primary.attempts == 1
    assert await _collect(llm) == "local answer"
    assert primary.attempts == 1  # still cooling down

    await asyncio.sleep(0.06)
    assert await _collect(llm) == "local answer"
    assert primary.attempts == 2  # a recovered cloud model gets picked up again


async def test_a_zero_cooldown_retries_the_primary_every_turn():
    primary = _Scripted("openai", LLMError("down"))
    backup = _Scripted("ollama", "local answer")
    llm = FallbackLLM(_config(fallback_cooldown=0.0), primary, [backup])

    await _collect(llm)
    await _collect(llm)

    assert primary.attempts == 2


# --------------------------------------------------------------------------- #
#  Plumbing
# --------------------------------------------------------------------------- #
async def test_health_check_names_the_link_that_answers():
    healthy = FallbackLLM(_config(), _Scripted("openai", "hi"), [_Scripted("ollama", "hi")])
    ok, detail = await healthy.health_check()
    assert ok is True
    assert detail == "openai ready"

    rescued = FallbackLLM(
        _config(),
        _Scripted("openai", LLMError("Cannot reach the API")),
        [_Scripted("ollama", "hi")],
    )
    ok, detail = await rescued.health_check()
    assert ok is True
    assert "ollama ready" in detail
    assert "Cannot reach the API" in detail
    # Probing must not silently switch the session: the primary is still first.
    assert rescued.using_fallback is False

    dead = FallbackLLM(
        _config(),
        _Scripted("openai", LLMError("no key")),
        [_Scripted("ollama", LLMError("no server"))],
    )
    ok, detail = await dead.health_check()
    assert ok is False
    assert "no key" in detail and "no server" in detail


async def test_context_and_close_reach_every_link():
    primary = _Scripted("openai", "hi")
    backup = _Scripted("ollama", "hi")
    llm = FallbackLLM(_config(), primary, [backup])

    llm.set_context({"language": "bn"})
    await llm.aclose()

    assert primary.context == {"language": "bn"}
    assert backup.context == {"language": "bn"}
    assert primary.closed is True
    assert backup.closed is True


# --------------------------------------------------------------------------- #
#  What the factory builds
# --------------------------------------------------------------------------- #
def test_the_factory_wraps_the_engine_and_orders_the_chain():
    config = _config(fallback_chain=[FallbackTarget()])
    engine = create_llm(config)

    assert isinstance(engine, FallbackLLM)
    described = engine.describe()
    assert described["provider"] == "openai"
    assert described["using_fallback"] is False
    assert described["chain"] == ["openai/qwen/qwen3.8-27b", "ollama/llama3.2:3b"]


def test_an_empty_chain_leaves_the_engine_alone():
    engine = create_llm(_config(fallback_chain=[]))
    assert not isinstance(engine, FallbackLLM)
    assert engine.name == "openai"


def test_the_offline_brain_is_never_a_link():
    # Not as the primary: `echo` has no failure mode to rescue.
    assert not isinstance(create_llm(_config(provider="echo")), FallbackLLM)
    # And not as a backup: canned text is not a fallback.
    engine = create_llm(_config(fallback_chain=[FallbackTarget(provider="echo")]))
    assert not isinstance(engine, FallbackLLM)


def test_a_backup_pointing_at_the_same_server_is_dropped():
    """The shipped default is Ollama; a duplicate link would cost a timeout."""
    config = _config(
        provider="ollama",
        base_url="http://localhost:11434",
        fallback_chain=[FallbackTarget()],  # 127.0.0.1:11434 - the same server
    )
    assert not isinstance(create_llm(config), FallbackLLM)

    # A backup on a *different* host is still kept.
    config = _config(fallback_chain=[FallbackTarget()])
    assert isinstance(create_llm(config), FallbackLLM)


def test_a_backup_never_inherits_the_primary_key():
    """A placeholder resolves to the stored key of the *primary* provider.

    Sending a Groq credential to a local server is exactly the leak this design
    has to prevent, so a link gets its own key or a harmless literal - never the
    primary's.
    """
    from app.secrets_store import set_secret

    set_secret("llm_api_key", FAKE_KEY)
    primary = _config()  # api_key="ollama" -> resolves to the stored key

    assert primary.effective_api_key() == FAKE_KEY  # the primary may use it

    link = _target_config(primary, FallbackTarget())
    assert link.effective_api_key() == "local"
    assert FAKE_KEY not in link.effective_api_key()


def test_a_backup_keeps_its_own_explicit_key():
    primary = _config()
    link = _target_config(primary, FallbackTarget(api_key="gsk_local_gateway_1234567890"))
    assert link.effective_api_key() == "gsk_local_gateway_1234567890"


def test_a_logged_link_never_shows_a_secret(monkeypatch):
    """`describe()` is what a status route or a log line would print."""
    from app.secrets_store import set_secret

    set_secret("llm_api_key", FAKE_KEY)
    engine = create_llm(_config(fallback_chain=[FallbackTarget(api_key="gsk_link_key_0000111122")]))

    text = str(engine.describe())
    assert FAKE_KEY not in text
    assert "gsk_link_key_0000111122" not in text
