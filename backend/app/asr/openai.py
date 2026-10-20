"""Remote speech recognition through an OpenAI compatible
``POST /v1/audio/transcriptions`` endpoint (OpenAI, Groq, ...).

The utterance is uploaded as a small in-memory WAV file.
"""

from __future__ import annotations

import httpx
from loguru import logger

from ..config import ASRConfig
from ..utils.audio import DecodedAudio, encode_wav
from .base import ASREngine, ASRError


class OpenAICompatibleASR(ASREngine):
    name = "openai"

    def __init__(self, config: ASRConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers: dict[str, str] = {}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
        return self._client

    def _endpoint(self) -> str:
        base = (self.config.base_url or "https://api.openai.com/v1").strip().rstrip("/")
        return f"{base}/audio/transcriptions"

    async def transcribe(self, audio: DecodedAudio, *, language: str | None = None) -> str:
        if self._too_short(audio):
            logger.trace("skipping {:.2f}s utterance (too short)", audio.duration)
            return ""

        # Re-sample to 16 kHz: it halves the upload and every Whisper API
        # resamples internally anyway.
        upload = audio.resampled(16000)
        wav = encode_wav(upload.samples, upload.sample_rate)

        data: dict[str, str] = {
            "model": self.config.model,
            "response_format": "json",
        }
        target_language = (language or self.config.language or "").strip()
        if target_language and target_language.lower() not in ("auto", "none"):
            data["language"] = target_language
        if self.config.prompt:
            data["prompt"] = self.config.prompt

        try:
            response = await self.client().post(
                self._endpoint(),
                data=data,
                files={"file": ("utterance.wav", wav, "audio/wav")},
            )
        except httpx.HTTPError as exc:
            raise ASRError(f"Cannot reach the ASR server at {self._endpoint()}: {exc}") from exc

        if response.status_code >= 400:
            raise ASRError(
                f"ASR server returned {response.status_code}: {response.text[:300]}"
            )

        try:
            payload = response.json()
        except ValueError:
            return response.text.strip()

        text = (payload.get("text") or "").strip()
        logger.debug("remote ASR: {:.2f}s -> {!r}", audio.duration, text)
        return text

    async def health_check(self) -> tuple[bool, str]:
        if not self.config.base_url:
            return False, "asr.base_url is not set"
        if "api.openai.com" in self.config.base_url and not self.config.api_key:
            return False, "an API key is required for api.openai.com"
        return True, "configured"

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


__all__ = ["OpenAICompatibleASR"]
