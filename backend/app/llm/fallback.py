"""Failover between providers, with one rule: never splice a reply.

The common failure is not "the model is wrong", it is "the model is not there
right now". The hotspot dropped, a free hosted tier rate-limited us, the key was
rotated, or Ollama was simply not started yet. This module turns all of those
from "the app is broken" into "the app answered anyway, and said so".

Three rules keep the failover honest:

* **Only before the first token.** Once a reply has started streaming, a failure
  is reported as an error. Switching models mid-reply produces an audible seam -
  a change of voice and of facts - which is worse than admitting the failure.
* **Never the offline brain.** The scripted ``echo`` provider is deliberately
  absent from every chain. Canned text is not a fallback, it is the exact failure
  this app used to have; ``VTUBER_LLM__FALLBACK_TO_ECHO`` remains the explicit
  opt-in for anyone who wants it.
* **A dead link is skipped for a cooldown.** One timeout per minute instead of
  one per turn - and the primary is retried first as soon as the cooldown
  lapses, so a recovered cloud model is picked up again without a restart.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from loguru import logger

from ..config import LLMConfig
from .base import ChatMessage, LLMEngine, LLMError

__all__ = ["FallbackEvent", "FallbackLLM"]


def _label(engine: LLMEngine) -> str:
    """``ollama/llama3.2:3b`` - the model is what the user picked, not the brand."""
    model = (getattr(engine.config, "model", "") or "").strip()
    return f"{engine.name}/{model}" if model else engine.name


@dataclass(slots=True)
class FallbackEvent:
    """One "we switched" notice, surfaced to the UI at most once per switch."""

    from_label: str
    to_label: str
    reason: str

    def message(self) -> str:
        """A sentence a person can act on, shown as a dismissible notice."""
        return (
            f"{self.from_label} did not answer, so replies come from {self.to_label} instead. "
            "Open Settings -> Model to check the connection or turn this off."
        )


class FallbackLLM(LLMEngine):
    """Try the configured provider, then each backup, in order.

    Built by :func:`app.llm.factory.create_llm`; it receives ready-made engines
    rather than configs so the factory owns all construction (and this module
    stays free of an import cycle).
    """

    def __init__(self, config: LLMConfig, primary: LLMEngine, backups: list[LLMEngine]) -> None:
        super().__init__(config)
        self._engines: list[LLMEngine] = [primary, *backups]
        #: Index -> monotonic deadline. A link that just failed is skipped.
        self._cooldown_until: dict[int, float] = {}
        self._last_used = 0
        self._last_error = ""
        self._event: FallbackEvent | None = None

    # ------------------------------------------------------------------ #
    #  Identity
    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:  # type: ignore[override]
        """The provider that produced the last reply - never ``"fallback"``.

        Callers branch on this (``llm.name == "echo"`` selects the offline prompt
        path), so it has to stay the real, currently effective provider.
        """
        return self._engines[self._last_used].name

    @property
    def active(self) -> LLMEngine:
        return self._engines[self._last_used]

    @property
    def using_fallback(self) -> bool:
        return self._last_used > 0

    def describe(self) -> dict[str, Any]:
        """Static-ish status for ``GET /api/llm/status`` and the logs."""
        return {
            "provider": self.active.name,
            "model": self.active.config.model,
            "base_url": self.active.config.base_url,
            "using_fallback": self.using_fallback,
            "chain": [_label(engine) for engine in self._engines],
            "last_error": self._last_error,
        }

    def take_event(self) -> FallbackEvent | None:
        """Return (and clear) the pending switch notice, so the UI hears it once."""
        event, self._event = self._event, None
        return event

    # ------------------------------------------------------------------ #
    #  Cooldowns
    # ------------------------------------------------------------------ #
    def _ready(self) -> list[int]:
        """Link indices worth trying right now, in priority order."""
        now = time.monotonic()
        ready = [i for i in range(len(self._engines)) if self._cooldown_until.get(i, 0.0) <= now]
        if ready:
            return ready
        # Everything failed inside the cooldown window. Trying them all again is
        # the honest answer: it self-heals a device that came back online, and it
        # produces a real error instead of "nothing to try".
        self._cooldown_until.clear()
        return list(range(len(self._engines)))

    def _cool(self, index: int) -> None:
        cooldown = max(0.0, float(self.config.fallback_cooldown or 0.0))
        if cooldown:
            self._cooldown_until[index] = time.monotonic() + cooldown

    def _settle(self, index: int) -> None:
        """Record that link ``index`` produced a reply."""
        self._cooldown_until.pop(index, None)
        if index > 0 and self._last_used == 0:
            self._event = FallbackEvent(
                from_label=_label(self._engines[0]),
                to_label=_label(self._engines[index]),
                reason=self._last_error,
            )
        elif index == 0 and self._last_used > 0:
            logger.info("LLM: {} is answering again", _label(self._engines[0]))
        self._last_used = index

    # ------------------------------------------------------------------ #
    #  LLMEngine
    # ------------------------------------------------------------------ #
    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        failures: list[str] = []
        candidates = self._ready()
        for position, index in enumerate(candidates):
            engine = self._engines[index]
            produced = False
            try:
                stream = engine.stream_chat(messages, temperature=temperature, max_tokens=max_tokens)
                async for delta in stream:
                    produced = True
                    yield delta
            except Exception as exc:
                # `GeneratorExit` / `CancelledError` are BaseExceptions and pass
                # straight through, which is what lets the caller close the
                # generator mid-reply (barge-in) without triggering a retry.
                if produced:
                    # Half a reply is already on the wire; a different model
                    # finishing it would be worse than an error.
                    raise
                detail = str(exc) if isinstance(exc, LLMError) else f"unexpected error: {exc}"
                failures.append(f"{_label(engine)}: {detail}")
                self._last_error = failures[-1]
                self._cool(index)
                if position + 1 < len(candidates):
                    logger.warning(
                        "LLM {} failed before the first token ({}); trying {}",
                        engine.name,
                        detail,
                        self._engines[candidates[position + 1]].name,
                    )
                    continue
                raise LLMError(self._combined(failures)) from exc
            else:
                self._settle(index)
                return

    async def health_check(self) -> tuple[bool, str]:
        """Report the first link that answers, and why it is not the primary.

        Deliberately does *not* switch the active provider: at run time the
        primary is still tried first on every turn, so a cloud model that comes
        back is used again without a restart.
        """
        problems: list[str] = []
        for index, engine in enumerate(self._engines):
            try:
                ok, detail = await engine.health_check()
            except Exception as exc:  # pragma: no cover - defensive
                ok, detail = False, str(exc)
            if ok:
                if index == 0:
                    return True, detail
                because = problems[0] if problems else "see the log"
                return True, f"{detail} - backup in use because {because}"
            problems.append(f"{_label(engine)}: {detail}")
        return False, self._combined(problems)

    def set_context(self, context: dict[str, Any]) -> None:
        """Forward the per-turn context to every link (only echo reads it)."""
        for engine in self._engines:
            engine.set_context(context)

    async def aclose(self) -> None:
        for engine in self._engines:
            await engine.aclose()

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    def _combined(self, failures: list[str]) -> str:
        """One actionable sentence naming every link that was tried."""
        chain = " -> ".join(_label(engine) for engine in self._engines)
        detail = " | ".join(failures) or "no provider answered"
        return f"No model in the chain answered ({chain}). {detail}"
