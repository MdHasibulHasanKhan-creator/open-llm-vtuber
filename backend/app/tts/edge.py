"""Microsoft Edge neural voices via the ``edge-tts`` package.

This is the default engine because it gives us the single most valuable thing
for convincing lip-sync: **exact word boundaries**. ``WordBoundary`` events
carry the offset (in 100 ns ticks) and the duration of every spoken word, which
:mod:`app.lipsync.timeline` uses to pin mouth shapes to real audio.

Requires internet access to Microsoft's read-aloud endpoint (free, no key).
"""

from __future__ import annotations

from loguru import logger

from ..utils.audio import estimate_duration
from .base import SynthesisResult, TTSEngine, TTSError, WordTiming
from .voices import voice_for_language

#: edge-tts always emits 24 kHz 48 kbps mono mp3.
EDGE_SAMPLE_RATE = 24000

_voice_cache: list[dict[str, str]] | None = None


class EdgeTTS(TTSEngine):
    name = "edge"

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        rate: str | None = None,
        pitch: str | None = None,
    ) -> SynthesisResult:
        import edge_tts

        selected_voice = (voice or self.config.voice or "en-US-AriaNeural").strip()
        kwargs = {
            "voice": selected_voice,
            "rate": (rate or self.config.rate or "+0%"),
            "pitch": (pitch or self.config.pitch or "+0Hz"),
            "volume": self.config.volume or "+0%",
        }

        audio = bytearray()
        timings: list[WordTiming] = []

        try:
            communicate = edge_tts.Communicate(text, **kwargs)
            async for chunk in communicate.stream():
                chunk_type = chunk.get("type")
                if chunk_type == "audio":
                    audio.extend(chunk["data"])
                elif chunk_type == "WordBoundary":
                    # offset/duration are in 100-nanosecond ticks.
                    timings.append(
                        WordTiming(
                            offset=float(chunk["offset"]) / 1e7,
                            duration=float(chunk["duration"]) / 1e7,
                            text=str(chunk.get("text", "")),
                        )
                    )
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            logger.warning("edge-tts failed for voice={} : {}", selected_voice, message)
            raise TTSError(
                f"edge-tts failed ({message}). Check the voice name and your internet "
                "connection, or switch tts.engine to 'browser' for offline speech."
            ) from exc

        if not audio:
            raise TTSError(
                f"edge-tts returned no audio for voice '{selected_voice}'. "
                "The voice id may be invalid - check GET /api/voices."
            )

        logger.debug(
            "edge-tts: {} bytes, {} word boundaries, voice={}",
            len(audio), len(timings), selected_voice,
        )
        return SynthesisResult(
            audio=bytes(audio),
            audio_format="mp3",
            sample_rate=EDGE_SAMPLE_RATE,
            word_timings=timings,
            estimated_duration=estimate_duration(text),
            voice=selected_voice,
        )

    async def list_voices(self) -> list[dict[str, str]]:
        """All public Edge voices (cached for the process lifetime)."""
        global _voice_cache
        if _voice_cache is not None:
            return _voice_cache

        import edge_tts

        try:
            raw = await edge_tts.list_voices()
        except Exception as exc:
            logger.warning("could not fetch the edge voice list: {}", exc)
            return []

        _voice_cache = [
            {
                "id": voice.get("ShortName", ""),
                "name": voice.get("FriendlyName") or voice.get("ShortName", ""),
                "locale": voice.get("Locale", ""),
                "gender": voice.get("Gender", ""),
            }
            for voice in raw
            if voice.get("ShortName")
        ]
        _voice_cache.sort(key=lambda v: (v["locale"], v["name"]))
        return _voice_cache

    def voice_for_language(self, language: str) -> str:
        """Edge: the voice *is* the language (see ``app/tts/voices.py``)."""
        return voice_for_language(language)


    async def health_check(self) -> tuple[bool, str]:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return False, "the `edge-tts` package is missing (pip install edge-tts)"
        return True, "ready (needs internet at synthesis time)"
