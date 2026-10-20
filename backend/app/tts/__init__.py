"""Text-to-speech providers.

Engines return *encoded* audio (mp3 / raw PCM) plus, when the provider offers
them, word timings. Decoding, loudness analysis and viseme generation all
happen later in :mod:`app.pipeline.speech`, so an engine only has to know how to
talk to its service.
"""

from .base import SynthesisResult, TTSEngine, TTSError, WordTiming
from .factory import create_tts

__all__ = ["SynthesisResult", "TTSEngine", "TTSError", "WordTiming", "create_tts"]
