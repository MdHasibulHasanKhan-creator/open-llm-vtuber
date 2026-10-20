"""OpenAI compatible ``/v1/audio/speech`` provider.

Works with OpenAI, and with the many local servers that copy that endpoint
(Kokoro-FastAPI, openedai-speech, GPT-SoVITS wrappers, Azure proxies, ...).

Raw PCM is requested first (24 kHz s16le, zero decode cost, lowest latency); if
the server rejects it we transparently retry with mp3.
"""

from __future__ import annotations

import httpx
from loguru import logger

from ..utils.audio import estimate_duration
from .base import SynthesisResult, TTSEngine, TTSError

#: OpenAI's `pcm` response_format is 24 kHz 16 bit mono.
PCM_SAMPLE_RATE = 24000


def rate_to_speed(rate: str | None, default: float = 1.0) -> float:
    """``"+20%"`` -> ``1.2`` (OpenAI only accepts a float multiplier)."""
    if not rate:
        return default
    cleaned = rate.strip().replace("%", "")
    try:
        value = float(cleaned)
    except ValueError:
        return default
    return max(0.25, min(4.0, 1.0 + value / 100.0))


class OpenAITTS(TTSEngine):
    name = "openai"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(60.0, connect=5.0),
            )
        return self._client

    def _endpoint(self) -> str:
        base = (self.config.base_url or "https://api.openai.com/v1").strip().rstrip("/")
        return f"{base}/audio/speech"

    async def _request(self, payload: dict, response_format: str, text: str, voice: str) -> bytes:
        payload = {**payload, "response_format": response_format}
        response = await self.client().post(self._endpoint(), json=payload)
        if response.status_code >= 400:
            body = response.text[:300]
            raise TTSError(f"TTS server returned {response.status_code}: {body}")
        if not response.content:
            raise TTSError("TTS server returned an empty body")
        logger.debug(
            "openai tts: {} bytes as {}, model={}, voice={}, text={!r}",
            len(response.content), response_format, payload.get("model"), voice, text[:40],
        )
        return response.content

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        rate: str | None = None,
        pitch: str | None = None,
    ) -> SynthesisResult:
        selected_voice = (voice or self.config.voice or "alloy").strip()
        payload = {
            "model": self.config.model or "tts-1",
            "input": text,
            "voice": selected_voice,
            "speed": rate_to_speed(rate or self.config.rate),
        }

        try:
            try:
                audio = await self._request(payload, "pcm", text, selected_voice)
                audio_format, sample_rate = "pcm_s16le", PCM_SAMPLE_RATE
            except TTSError as first_error:
                logger.debug("pcm response_format refused, falling back to mp3: {}", first_error)
                audio = await self._request(payload, "mp3", text, selected_voice)
                audio_format, sample_rate = "mp3", 0
        except httpx.ConnectError as exc:
            raise TTSError(
                f"Cannot reach the TTS server at {self._endpoint()}. Is it running?"
            ) from exc
        except httpx.HTTPError as exc:
            raise TTSError(f"TTS transport error: {exc}") from exc

        return SynthesisResult(
            audio=audio,
            audio_format=audio_format,
            sample_rate=sample_rate,
            word_timings=[],
            estimated_duration=estimate_duration(text),
            voice=selected_voice,
        )

    async def health_check(self) -> tuple[bool, str]:
        base = (self.config.base_url or "").strip().rstrip("/")
        if not base:
            return False, "tts.base_url is not set"
        if not (self.config.api_key):
            return True, "configured (no api key set - fine for local servers)"
        return True, "configured"

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
