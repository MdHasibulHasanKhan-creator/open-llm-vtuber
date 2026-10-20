"""OpenAI compatible chat completions (OpenAI, DeepSeek, Groq, LM Studio,
vLLM, llama.cpp server, OpenRouter, Ollama's ``/v1`` endpoint, ...).

Only ``POST {base_url}/chat/completions`` and ``GET {base_url}/models`` are
used, which every one of those servers implements.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from loguru import logger

from ..security import redact
from .base import ChatMessage, LLMEngine, LLMError

#: Default local endpoints, used to build a friendlier error message.
_LOCAL_HINTS = {
    "localhost": "Is your local server (Ollama / LM Studio / vLLM) running?",
    "127.0.0.1": "Is your local server (Ollama / LM Studio / vLLM) running?",
}


class OpenAICompatibleLLM(LLMEngine):
    name = "openai"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        #: Clients retired when the key or the base_url changed at runtime. They
        #: are closed on `aclose()`; a key swap is rare enough that holding one
        #: extra wrapper until teardown is cheaper than making `client()` async.
        self._stale_clients: list[httpx.AsyncClient] = []
        self._endpoint = self._normalise(config.base_url)
        self._client_key: str | None = None

    # ------------------------------------------------------------------ #
    #  Plumbing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalise(base_url: str) -> str:
        url = (base_url or "").strip().rstrip("/")
        if not url:
            raise LLMError("llm.base_url is empty - set e.g. http://localhost:11434/v1")
        if url.endswith("/chat/completions"):
            return url
        return f"{url}/chat/completions"

    def _refresh_target(self) -> None:
        """Re-read ``base_url``, which the settings UI can change in a live session.

        Caching it in ``__init__`` was why a saved provider only took effect after
        a server restart: the running session kept posting to ``localhost:11434``
        no matter what the drawer said.
        """
        endpoint = self._normalise(self.config.base_url)
        if endpoint != self._endpoint:
            logger.debug("LLM endpoint changed at runtime -> {}", endpoint)
            self._endpoint = endpoint

    def client(self) -> httpx.AsyncClient:
        self._refresh_target()
        # `effective_api_key()` resolves a placeholder ("ollama") to the key stored
        # by the settings UI, and returns "" when the endpoint wants no
        # authentication at all - so we never send `Bearer ollama` to a hosted
        # provider. Comparing against the last key makes a freshly saved key apply
        # to the very next turn.
        api_key = self.config.effective_api_key()
        if self._client is None or self._client.is_closed or self._client_key != api_key:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            if self._client is not None and not self._client.is_closed:
                self._stale_clients.append(self._client)
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(self.config.timeout, connect=10.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
            self._client_key = api_key
        return self._client

    def _payload(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.as_dict() for m in messages],
            "stream": True,
            "temperature": self.config.temperature if temperature is None else temperature,
            "top_p": self.config.top_p,
            # Penalties are what stop a small model from parroting the prompt or
            # looping on its own last sentence. `extra_body` can still override.
            "presence_penalty": self.config.presence_penalty,
            "frequency_penalty": self.config.frequency_penalty,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
        }
        payload.update(self.config.extra_body or {})
        return payload

    @staticmethod
    def _friendly_error(status: int, body: str, headers: httpx.Headers | None = None) -> str:
        """A message a human can act on, per status code.

        The tail is the provider's own words, truncated - it is often the only
        thing that says *which* model name was wrong.
        """
        detail = body.strip()[:300]
        if status == 401:
            return (
                "The provider rejected the API key (401). Open Settings, Model, check the "
                "key belongs to this provider, and Save again. " + detail
            )
        if status == 403:
            return (
                "The provider refused this request (403). The key may not be allowed to use "
                "this model, or the model is not enabled on your account. " + detail
            )
        if status == 404:
            return (
                "Model or endpoint not found (404). The model name or the base_url is "
                f"probably wrong (expected POST <base_url>/chat/completions). {detail}"
            )
        if status == 429:
            wait = ""
            if headers is not None:
                after = (headers.get("retry-after") or "").strip()
                if after:
                    wait = f" The provider suggests waiting about {after} seconds."
            return (
                "Rate limited (429)."
                + (wait or " The free tier allows a limited number of tokens per minute.")
                + f" {detail}"
            )
        if status in (500, 502, 503, 504):
            return (
                f"The provider is having trouble (server error {status}). This is usually "
                f"temporary - try again in a moment. {detail}"
            )
        return f"LLM error {status}: {detail}"

    # ------------------------------------------------------------------ #
    #  Streaming
    # ------------------------------------------------------------------ #
    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        payload = self._payload(messages, temperature=temperature, max_tokens=max_tokens)

        try:
            async with self.client().stream("POST", self._endpoint, json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "ignore")
                    # A provider error body can echo the request's Authorization
                    # header; scrub it before it reaches a sink.
                    logger.error("LLM HTTP {}: {}", response.status_code, redact(body[:400]))
                    raise LLMError(
                        self._friendly_error(response.status_code, body, response.headers)
                    )

                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    # Some local servers ignore `stream` and answer with JSON.
                    body = (await response.aread()).decode("utf-8", "ignore")
                    yield self._extract_full(body)
                    return

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):  # SSE keep-alive comment
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data in ("[DONE]", "done"):
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        logger.trace("skipping malformed SSE payload: {}", data[:120])
                        continue

                    if error := chunk.get("error"):
                        raise LLMError(
                            f"LLM stream error: {error.get('message') if isinstance(error, dict) else error}"
                        )

                    text = self._extract_delta(chunk)
                    if text:
                        yield text

        except LLMError:
            raise
        except httpx.ConnectError as exc:
            hint = next((v for k, v in _LOCAL_HINTS.items() if k in self._endpoint), "")
            raise LLMError(
                f"Cannot reach the LLM at {self._endpoint} ({exc.__class__.__name__}). {hint}".strip()
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"The LLM did not answer within {self.config.timeout:.0f}s - a local model "
                "may still be loading. Try again, or lower llm.max_tokens."
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM transport error: {exc}") from exc

    # ------------------------------------------------------------------ #
    #  Parsing helpers (tolerant of small spec deviations)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_delta(chunk: dict[str, Any]) -> str:
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        choice = choices[0]
        delta = choice.get("delta") or {}
        text = delta.get("content")
        if text is None:
            # non-streaming shape smuggled into a stream
            message = choice.get("message") or {}
            text = message.get("content")
        if not text:
            # Reasoning models expose `reasoning_content`; never spoken.
            return ""
        return text

    @staticmethod
    def _extract_full(body: str) -> str:
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM returned a non JSON body: {body[:200]}") from exc
        if error := data.get("error"):
            raise LLMError(f"LLM error: {error.get('message') if isinstance(error, dict) else error}")
        choices = data.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message") or {}).get("content") or ""

    async def health_check(self) -> tuple[bool, str]:
        self._refresh_target()
        url = self._endpoint.rsplit("/chat/completions", 1)[0] + "/models"
        try:
            response = await self.client().get(url, timeout=8.0)
        except httpx.HTTPError as exc:
            hint = next((v for k, v in _LOCAL_HINTS.items() if k in url), "")
            return False, f"unreachable ({exc.__class__.__name__}). {hint}".strip()
        if response.status_code == 200:
            return True, "reachable (200)"
        if response.status_code in (401, 403):
            # Reachable, but it will never answer a question with this key -
            # reporting that as "ready" is how a broken key wastes an hour.
            return False, (
                f"reachable, but the API key was rejected ({response.status_code}). "
                "Open Settings -> Model and check the key."
            )
        if response.status_code == 404:
            # Not every OpenAI-compatible server implements /models; the chat
            # endpoint can still work perfectly, so this is not a failure.
            return True, "reachable (no /models route)"
        return False, f"unexpected status {response.status_code}"

    async def aclose(self) -> None:
        for client in (self._client, *self._stale_clients):
            if client is not None and not client.is_closed:
                await client.aclose()
        self._stale_clients = []
        self._client = None
        self._client_key = None
