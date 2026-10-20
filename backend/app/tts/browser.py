"""Client-side speech synthesis fallback.

The backend returns **no audio**; it only describes *what* to say and the viseme
timeline. The browser speaks the sentence with the Web Speech API while Three-VRM
animates the mouth off the same timeline.

Why it matters: this makes the whole project usable with zero TTS dependencies
and no internet, and it is the only engine whose timings come from the browser's
own speech engine (``onboundary`` events are forwarded back to the client-side
lip-sync controller when available).
"""

from __future__ import annotations

from ..utils.audio import estimate_duration
from .base import SynthesisResult, TTSEngine


class BrowserTTS(TTSEngine):
    name = "browser"

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        rate: str | None = None,
        pitch: str | None = None,
    ) -> SynthesisResult:
        # `voice` is a browser voice name here (see GET /api/voices?engine=browser
        # on the frontend side - it lives in the browser, not on the server).
        return SynthesisResult(
            audio=None,
            audio_format=None,
            sample_rate=0,
            word_timings=[],
            estimated_duration=estimate_duration(text, chars_per_second=15.5),
            voice=voice or self.config.voice,
        )

    async def health_check(self) -> tuple[bool, str]:
        return True, "delegated to the browser (no server audio)"

    async def list_voices(self) -> list[dict[str, str]]:
        # Voices are enumerated by the client; keep a few sensible defaults so
        # the settings UI is never empty.
        return [
            {"id": "default", "name": "System default voice", "locale": "", "gender": ""},
        ]


__all__ = ["BrowserTTS"]
