"""Server side lip-sync: text -> mouth shapes -> a time aligned timeline.

Pipeline
--------
``text_to_phonemes()``  lightweight grapheme-to-viseme (no heavy G2P model,
                        works for English *and* Japanese romaji)
``build_timeline()``    distributes those phonemes over the real audio duration,
                        optionally snapping to TTS word boundaries
``build_visemes()``     convenience wrapper used by the orchestrator

The output is a list of :class:`VisemeFrame` in *seconds relative to the start
of the audio the client is about to play*, which the frontend samples every
animation frame (see ``frontend/src/vrm/LipSyncController.ts``).
"""

from .timeline import VisemeFrame, build_timeline, build_visemes
from .visemes import VISEMES, Phoneme, text_to_phonemes

__all__ = [
    "VISEMES",
    "Phoneme",
    "VisemeFrame",
    "build_timeline",
    "build_visemes",
    "text_to_phonemes",
]
