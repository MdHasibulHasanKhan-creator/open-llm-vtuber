"""A tiny, human-readable memory of the person on the other side.

One JSON file, one profile. The app is a single-user desktop companion (that was
an explicit design decision, see ``docs/ARCHITECTURE.md``), so there is no keying
by browser or session: ``backend/memory/profile.json`` is *your* file, and you
are invited to open it in an editor and fix anything the extractor got wrong.

The file is deliberately boring::

    {
      "version": 1,
      "name": "Rahim",
      "facts": {"from": "Dhaka", "works_as": "a teacher"},
      "likes": ["coffee", "rainy afternoons"],
      "dislikes": ["early mornings"],
      "topics": ["deadlines", "weekend"],
      "summary": "They are wrestling with a deadline and planning the weekend.",
      "turns": 14,
      "updated": 1730000000.0
    }

``describe()`` renders the prompt block that makes the model *sound* like it
remembers; everything else here is bookkeeping. Nothing in this module talks to
a model, so memory works identically with a local model, a cloud key, or the
offline brain.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from .facts import extract

__all__ = ["Profile", "ProfileStore"]

#: Hard caps. A memory that grows forever eventually *crowds out* the actual
#: conversation in the context window, so every list is bounded and the oldest
#: entries fall off first.
MAX_FACTS = 40
MAX_LIKES = 16
MAX_DISLIKES = 12
MAX_TOPICS = 14
MAX_SUMMARY_CHARS = 400
#: How much of the rendered block may enter the system prompt.
MAX_BLOCK_CHARS = 900

#: Stored fact key -> a sentence that reads like something a person would say.
_FACT_PHRASES: dict[str, str] = {
    "works_as": "they work as {value}",
    "from": "they are from {value}",
    "lives_in": "they live in {value}",
    "studies": "they study {value}",
}


def _phrase(key: str, value: str) -> str:
    if key in _FACT_PHRASES:
        return _FACT_PHRASES[key].format(value=value)
    if key.startswith("favourite "):
        return f"their favourite {key.split(' ', 1)[1]} is {value}"
    return f"{key.replace('_', ' ')}: {value}"


@dataclass(slots=True)
class Profile:
    version: int = 1
    name: str = ""
    facts: dict[str, str] = field(default_factory=dict)
    likes: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    summary: str = ""
    turns: int = 0
    updated: float = 0.0

    # -------------------------------------------------------------- render --
    def describe(self) -> str:
        """The block injected into the system prompt. ``""`` when we know nothing."""
        bullets: list[str] = []
        if self.name:
            bullets.append(f"Their name is {self.name}.")
        if self.facts:
            rendered = "; ".join(_phrase(key, value) for key, value in self.facts.items())
            bullets.append(f"Things they have told you: {rendered}.")
        if self.likes:
            bullets.append(f"Their likes: {', '.join(self.likes)}.")
        if self.dislikes:
            bullets.append(f"Their dislikes: {', '.join(self.dislikes)}.")
        if self.topics:
            bullets.append(f"Most recently they have been talking about: {', '.join(self.topics)}.")
        if self.summary:
            bullets.append(f"Earlier in this conversation: {self.summary}")

        if not bullets:
            return ""

        body = "\n".join(f"* {line}" for line in bullets)
        block = (
            "What you already know about the person you are talking to\n"
            "--------------------------------------------------------\n"
            f"{body}\n"
            "Use this the way a friend uses memory: naturally, occasionally, and only\n"
            "when it fits. Never recite the list back at them, and never say that you\n"
            "have 'notes' or that you are 'remembering' - just know it."
        )
        return block[:MAX_BLOCK_CHARS]

    def is_empty(self) -> bool:
        return not (self.name or self.facts or self.likes or self.dislikes or self.topics or self.summary)


class ProfileStore:
    """Loads, updates and saves :class:`Profile` with atomic writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.profile = self._load()
        self._mtime: float | None = self._stat()

    def _stat(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def _maybe_reload(self) -> None:
        """Re-read the file when something else changed it.

        The profile is documented as human-editable, and ``DELETE /api/memory``
        clears it out from under a live session. Both cases are handled by
        noticing the newer mtime instead of silently resurrecting old data on the
        next save.
        """
        mtime = self._stat()
        if mtime is not None and mtime != self._mtime:
            self.profile = self._load()
            self._mtime = mtime

    # ---------------------------------------------------------- persistence --
    def _load(self) -> Profile:
        if not self.path.exists():
            return Profile()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("profile is not an object")
        except Exception as exc:  # corrupt / hand-edited badly
            logger.warning("memory file {} is unreadable ({}); starting a fresh one", self.path, exc)
            return Profile()

        profile = Profile()
        profile.name = str(raw.get("name") or "")
        profile.summary = str(raw.get("summary") or "")
        profile.turns = int(raw.get("turns") or 0)
        profile.updated = float(raw.get("updated") or 0.0)
        facts = raw.get("facts")
        if isinstance(facts, dict):
            profile.facts = {str(k): str(v) for k, v in facts.items() if v}
        for attr in ("likes", "dislikes", "topics"):
            value = raw.get(attr)
            if isinstance(value, list):
                setattr(profile, attr, [str(v) for v in value if str(v).strip()])
        return profile

    def save(self) -> None:
        """Atomic write: a crash mid-save can never truncate the profile."""
        self.profile.updated = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(asdict(self.profile), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("could not write memory file {}: {}", self.path, exc)
        else:
            self._mtime = self._stat()

    # ------------------------------------------------------------- mutation --
    def observe(self, text: str) -> list[str]:
        """Learn from one user message. Returns a human list of what is new."""
        self._maybe_reload()
        learned: list[str] = []
        facts = extract(text)

        if facts.name and facts.name.casefold() != self.profile.name.casefold():
            self.profile.name = facts.name
            learned.append(f"name={facts.name}")
        for key, value in facts.facts.items():
            if key not in self.profile.facts:
                self.profile.facts[key] = value
                learned.append(f"{key}={value}")
                if len(self.profile.facts) > MAX_FACTS:
                    self.profile.facts.pop(next(iter(self.profile.facts)))
        for value in facts.likes:
            if self._remember(self.profile.likes, value, MAX_LIKES):
                learned.append(f"like={value}")
        for value in facts.dislikes:
            if self._remember(self.profile.dislikes, value, MAX_DISLIKES):
                learned.append(f"dislike={value}")

        # Topics refresh every turn: they describe "lately", not "ever".
        fresh = [w for w in facts.topics if w.casefold() not in {t.casefold() for t in self.profile.topics}]
        if fresh:
            self.profile.topics = (fresh + self.profile.topics)[:MAX_TOPICS]

        self.profile.turns += 1
        if learned or fresh:
            self.save()
        return learned

    @staticmethod
    def _remember(bucket: list[str], value: str, cap: int) -> bool:
        if any(value.casefold() == existing.casefold() for existing in bucket):
            return False
        bucket.append(value)
        del bucket[:-cap]
        return True

    def set_summary(self, summary: str) -> None:
        cleaned = " ".join(summary.split())[:MAX_SUMMARY_CHARS]
        if cleaned and cleaned != self.profile.summary:
            self.profile.summary = cleaned
            self.save()

    def clear(self) -> None:
        self.profile = Profile()
        self.save()
        logger.info("memory cleared ({})", self.path)

    # ------------------------------------------------------------- accessors --
    def describe(self) -> str:
        return self.profile.describe()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self.profile)

    def snapshot(self) -> dict[str, Any]:
        """Small dict for the offline brain and the ``/api/memory`` route."""
        return {
            "name": self.profile.name,
            "likes": list(self.profile.likes),
            "dislikes": list(self.profile.dislikes),
            "topics": list(self.profile.topics),
            "turns": self.profile.turns,
        }
