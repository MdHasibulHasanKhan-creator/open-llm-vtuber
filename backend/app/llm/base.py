"""Provider agnostic LLM interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..config import LLMConfig


class LLMError(RuntimeError):
    """Raised for configuration / connectivity problems the user can fix.

    The message is forwarded to the web UI verbatim, so keep it actionable.
    """


@dataclass(slots=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class LLMEngine(ABC):
    """A streaming chat completion backend."""

    #: Short identifier reported to the client.
    name: str = "base"

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    #  Required
    # ------------------------------------------------------------------ #
    @abstractmethod
    def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield text deltas as they are produced.

        Implementations must raise :class:`LLMError` with a human readable
        message when the backend is unreachable or misconfigured.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    #  Optional
    # ------------------------------------------------------------------ #
    def set_context(self, context: dict[str, Any]) -> None:
        """Receive per-turn context (memory snapshot, language, ...).

        Real providers ignore this: everything they need is already in the
        messages they are handed. It exists for the *offline* brain, which has
        no system prompt of its own and would otherwise not know the user's
        name or what they like. The default is a no-op.
        """
        return None

    async def health_check(self) -> tuple[bool, str]:
        """Cheap reachability probe performed once at startup."""
        return True, "ok"

    async def aclose(self) -> None:
        """Release sockets / clients."""
        return None
