"""Long-term memory: what the companion knows about *you*.

Single-user by design. One JSON profile under ``backend/memory/``, readable and
editable by hand, updated without an LLM. See :mod:`app.memory.profile` for the
storage format and :mod:`app.memory.facts` for the extraction rules.
"""

from .facts import ExtractedFacts, extract, topical_words
from .profile import Profile, ProfileStore

__all__ = ["ExtractedFacts", "Profile", "ProfileStore", "extract", "topical_words"]
