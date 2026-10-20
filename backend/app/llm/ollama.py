"""Native Ollama ``/api/chat`` provider.

Why not just use Ollama's OpenAI compatibility layer? Because the native API
exposes ``keep_alive`` (so the first token after a pause is not a 30 s model
load) and reports progress while a model is being pulled.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import ChatMessage, LLMEngine, LLMError


class OllamaLLM(LLMEngine):
    name = "ollama"

    def __init__(self, config) -> None:
        super().__init__(config)
        base = (config.base_url or "http://localhost:11434").strip().rstrip("/")
        base = base.removesuffix("/v1")
        self._base = base
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                # Model loading on a cold start can exceed a minute; the read
                # timeout is intentionally generous, connect stays short.
                timeout=httpx.Timeout(self.config.timeout, connect=5.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.as_dict() for m in messages],
            "stream": True,
            "keep_alive": "10m",
            "options": {
                "temperature": self.config.temperature if temperature is None else temperature,
                "top_p": self.config.top_p,
                # Ollama takes the OpenAI penalty names inside `options`.
                "presence_penalty": self.config.presence_penalty,
                "frequency_penalty": self.config.frequency_penalty,
                "num_predict": self.config.max_tokens if max_tokens is None else max_tokens,
            },
        }
        payload.update(self.config.extra_body or {})

        try:
            async with self.client().stream("POST", f"{self._base}/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "ignore")
                    if response.status_code == 404:
                        raise LLMError(
                            f"Ollama has no model '{self.config.model}'. "
                            f"Run:  ollama pull {self.config.model}"
                        )
                    raise LLMError(f"Ollama error {response.status_code}: {body[:300]}")

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if error := chunk.get("error"):
                        raise LLMError(f"Ollama error: {error}")
                    text = (chunk.get("message") or {}).get("content") or ""
                    if text:
                        yield text
                    if chunk.get("done"):
                        break

        except LLMError:
            raise
        except httpx.ConnectError as exc:
            raise LLMError(
                f"Cannot reach Ollama at {self._base}. Start it with `ollama serve`."
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(
                "Ollama did not answer in time - the model is probably still loading."
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama transport error: {exc}") from exc

    async def health_check(self) -> tuple[bool, str]:
        try:
            response = await self.client().get(f"{self._base}/api/tags", timeout=5.0)
        except httpx.HTTPError as exc:
            return False, f"unreachable ({exc.__class__.__name__}) - run `ollama serve`"
        if response.status_code != 200:
            return False, f"unexpected status {response.status_code}"
        try:
            names = [m.get("name", "") for m in response.json().get("models", [])]
        except Exception:
            return True, "reachable"
        target = self.config.model.split(":")[0]
        if names and not any(n.split(":")[0] == target for n in names):
            return False, (
                f"model '{self.config.model}' is not pulled yet "
                f"(available: {', '.join(names[:5]) or 'none'}) - run `ollama pull {self.config.model}`"
            )
        return True, f"reachable, {len(names)} model(s)"

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
