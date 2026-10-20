"""Real time conversation pipeline (ASR -> LLM -> TTS -> lip-sync)."""

from .sentence_splitter import SentenceSplitter

__all__ = ["SentenceSplitter"]
