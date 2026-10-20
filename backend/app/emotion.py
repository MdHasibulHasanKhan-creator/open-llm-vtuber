"""Turn a sentence of the model's reply into a VRM expression + gestures.

Two sources of signal, in priority order:

1. **Explicit tags** the LLM was asked to emit, e.g. ``[happy] That's great!``
   or an inline gesture ``(laughs)``. Tags are removed from the text we speak
   but kept in the text we display.
2. **Heuristics** over the sentence (keywords + punctuation) so a model that
   ignores the tag instruction still animates.

The result is expressed with the *character's* emotion vocabulary
(``characters/*.yaml``), which maps a friendly name to a VRM 1.0 preset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from loguru import logger

from .config import CharacterConfig, ExpressionMapping

# --------------------------------------------------------------------------- #
#  Tag syntax
# --------------------------------------------------------------------------- #
#: ``[happy]`` / ``[[surprised]]`` / ``<sad>`` - usually at the start of a line.
_EMOTION_TAG_RE = re.compile(
    r"[\[<]\s*\[?\s*(?P<name>[a-zA-Z_\u3040-\u30ff\u4e00-\u9fff]{2,20})\s*\]?\s*[\]>]",
)
#: ``(smiles)`` / ``*sighs*`` - gestures, never spoken.
_ACTION_RE = re.compile(r"\((?P<paren>[a-zA-Z ,'\-]{2,32})\)|\*(?P<star>[a-zA-Z ,'\-]{2,32})\*")

#: ``(smiles)`` / ``*sighs*`` style notes -> skeleton gestures the frontend knows.
GESTURE_NOTES: dict[str, str] = {
    "nod": "nod", "nods": "nod", "nodding": "nod",
    "shake": "shake", "shakes": "shake", "shakes head": "shake",
    "tilt": "tilt", "tilts": "tilt", "tilts head": "tilt",
    "wave": "wave", "waves": "wave", "waving": "wave",
    "sigh": "sigh", "sighs": "sigh", "sighing": "sigh",
    "think": "think", "thinks": "think", "thinking": "think",
    "lean": "lean_in", "leans": "lean_in", "lean in": "lean_in",
    "smile": "smile", "smiles": "smile", "smiling": "smile",
    "laugh": "laugh", "laughs": "laugh", "laughing": "laugh", "giggle": "laugh",
    "giggles": "laugh", "chuckle": "laugh", "chuckles": "laugh",
    "blush": "blush", "blushes": "blush", "embarrassed": "blush",
}

#: Gestures the frontend action controller implements.
KNOWN_GESTURES: frozenset[str] = frozenset(GESTURE_NOTES.values())

EMOTION_ALIASES: dict[str, str] = {
    # happy
    "happy": "happy", "joy": "happy", "joyful": "happy", "cheerful": "happy",
    "excited": "happy", "glad": "happy", "smile": "happy", "smiles": "happy",
    "smiling": "happy", "laugh": "happy", "laughs": "happy", "laughing": "happy",
    "giggle": "happy", "giggles": "happy", "grin": "happy", "proud": "happy",
    "warm": "happy", "playful": "happy", "teasing": "happy",
    # angry
    "angry": "angry", "anger": "angry", "mad": "angry", "annoyed": "angry",
    "irritated": "angry", "frustrated": "angry", "pout": "angry", "pouts": "angry",
    "frown": "angry", "frowns": "angry", "stern": "angry",
    # sad
    "sad": "sad", "sorrow": "sad", "sorrowful": "sad", "cry": "sad", "cries": "sad",
    "crying": "sad", "teary": "sad", "upset": "sad", "worried": "sad",
    "disappointed": "sad", "lonely": "sad", "sigh": "sad", "sighs": "sad",
    # relaxed
    "relaxed": "relaxed", "calm": "relaxed", "fun": "relaxed", "content": "relaxed",
    "sleepy": "relaxed", "tired": "relaxed", "gentle": "relaxed",
    # shy (first-class: borrows the `relaxed` preset, distinguished by a blush)
    "shy": "shy", "bashful": "shy", "embarrassed": "shy", "blush": "shy",
    "blushes": "shy", "flustered": "shy",
    # surprised
    "surprised": "surprised", "surprise": "surprised", "shocked": "surprised",
    "shock": "surprised", "amazed": "surprised", "wow": "surprised",
    "confused": "surprised",
    # thinking (first-class: borrows the `relaxed` preset + a head tilt)
    "thinking": "thinking", "think": "thinking", "hmm": "thinking",
    "hmmm": "thinking", "curious": "thinking", "wondering": "thinking",
    # neutral
    "neutral": "neutral", "normal": "neutral", "serious": "neutral",
}

#: Used only when a *custom* character vocabulary is missing the resolved name.
#: Keeps a model that tags ``[thinking]`` from silently going blank on a persona
#: that never declared it: it degrades to a preset that character does have.
_DOWNGRADE: dict[str, str] = {
    "thinking": "relaxed",
    "shy": "relaxed",
}

#: Heuristic keyword lists (lowercase, matched against whole words).
_HEURISTICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("happy", ("great", "awesome", "amazing", "love", "lovely", "wonderful", "haha",
               "yay", "thanks", "thank you", "happy", "glad", "cool", "nice",
               "excited", "fun", "best", "beautiful", "perfect")),
    ("sad", ("sorry", "unfortunately", "sad", "miss you", "alone", "hurt",
             "lost", "unlucky", "difficult", "worry", "worried")),
    ("angry", ("angry", "annoying", "hate", "stop it", "ridiculous", "unfair",
               "seriously", "how dare", "don't do that", "rude")),
    # Deliberately narrow. "really", "what" and "seriously" are ordinary
    # conversational words: treating them as surprise put a wide-eyed, frozen
    # face on half of her own questions. Surprise needs an actual exclamation.
    ("surprised", ("wow", "whoa", "no way", "unbelievable", "oh my", "shocked",
                   "amazing")),
    ("thinking", ("hmm", "maybe", "perhaps", "i wonder", "let me think",
                  "not sure", "possibly", "depends")),
    ("relaxed", ("relax", "calm", "softly", "slowly", "sleepy", "peaceful",
                 "cozy", "sweet dreams", "good night")),
)

# A trailing "?" is curiosity, not shock: she is the one asking. Marking every
# question `surprised` gave her wide eyes and a frozen blink on half her replies.
# An actual interrobang still counts as surprise, because that *is* a reaction.


# --------------------------------------------------------------------------- #
#  Result
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class EmotionResult:
    """What the frontend should play for one sentence."""

    spoken: str
    """Text sent to the TTS engine (tags and gestures removed)."""

    display: str
    """Text shown in the chat bubble (tags kept, whitespace tidied)."""

    emotion: str = "neutral"
    """Friendly emotion name (key of ``CharacterConfig.emotions``)."""

    mapping: ExpressionMapping = field(default_factory=ExpressionMapping)
    """Resolved VRM preset + weight + hold time."""

    actions: list[str] = field(default_factory=list)
    """Gesture hints for the frontend, e.g. ``["nod", "smile"]``."""

    explicit_gestures: list[str] = field(default_factory=list)
    """Gestures the *model* asked for by name (``(laughs)``). Always play these."""

    default_action: str | None = None
    """The character's standing action for this emotion (``actions:`` in the yaml).

    Kept separate from ``explicit_gestures`` so the orchestrator can play it
    **once per turn** instead of once per sentence: a four-sentence happy reply
    otherwise queues four nods, and a queue of gestures reads as an avatar that
    will not sit still.
    """


def normalise_emotion(name: str, vocabulary: dict[str, ExpressionMapping] | None = None) -> str:
    """``"joy"`` -> ``"happy"``, respecting a character's custom vocabulary."""
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    if vocabulary and key in vocabulary:
        return key
    resolved = EMOTION_ALIASES.get(key, "")
    if resolved and vocabulary and resolved not in vocabulary:
        # A custom persona that never declared this emotion: step down to a
        # preset it does have rather than resolving to an inert name.
        fallback = _DOWNGRADE.get(resolved, "")
        return fallback if fallback in vocabulary else ""
    return resolved


def _extract_tags(
    text: str,
    vocabulary: dict[str, ExpressionMapping] | None,
) -> tuple[str, str | None, list[str]]:
    """Pull ``[emotion]`` / ``(gesture)`` markers out of the sentence.

    Returns ``(remaining_text, emotion_or_None, gestures)``. Unknown bracket
    content is dropped from the *spoken* text but the display text is untouched
    (the caller keeps the original string for the UI).
    """
    state: dict[str, str | None] = {"emotion": None}
    gestures: list[str] = []

    def _emotion_sub(match: re.Match[str]) -> str:
        name = match.group("name").strip().lower()
        candidate = normalise_emotion(name, vocabulary)
        if candidate:
            state["emotion"] = state["emotion"] or candidate
        # Either way the marker is not spoken.
        return ""

    def _action_sub(match: re.Match[str]) -> str:
        raw = (match.group("paren") or match.group("star") or "").strip().lower()
        name = raw.split()[0] if raw else ""
        gesture = GESTURE_NOTES.get(name)
        if gesture:
            gestures.append(gesture)
            # A gesture note also tells us the mood ("(laughs)" -> happy).
            emotion = EMOTION_ALIASES.get(name)
            if emotion and emotion != "neutral":
                state["emotion"] = state["emotion"] or emotion
        elif emotion := EMOTION_ALIASES.get(name):
            # Pure emotion note, e.g. "(angry)".
            if emotion != "neutral":
                state["emotion"] = state["emotion"] or emotion
        return ""

    text = _EMOTION_TAG_RE.sub(_emotion_sub, text)
    text = _ACTION_RE.sub(_action_sub, text)
    return text, state["emotion"], gestures


def _heuristic(text: str) -> str:
    lowered = f" {text.lower()} "
    words = set(re.findall(r"[a-z']+", lowered))
    scores: dict[str, int] = {}
    for emotion, keywords in _HEURISTICS:
        score = 0
        for keyword in keywords:
            if " " in keyword:
                if keyword in lowered:
                    score += 2
            elif keyword in words:
                score += 1
        if score:
            scores[emotion] = score

    exclamations = text.count("!")
    if exclamations:
        scores["happy"] = scores.get("happy", 0) + min(exclamations, 2)

    # A question is a question, not a shock. `thinking` borrows the relaxed
    # preset, so this reads as a small, curious face rather than a wide-eyed
    # stare, and it does not freeze the blink.
    if text.rstrip().endswith("?"):
        scores["thinking"] = scores.get("thinking", 0) + 1
    if "?!" in text or "!?" in text:
        scores["surprised"] = scores.get("surprised", 0) + 2

    if not scores:
        return "neutral"
    return max(scores.items(), key=lambda kv: kv[1])[0]


def parse_emotion(
    text: str,
    character: CharacterConfig | None = None,
    *,
    use_heuristics: bool = True,
) -> EmotionResult:
    """Analyse one sentence of the reply.

    The ``spoken`` field must be handed to TTS; ``display`` to the chat UI.
    """
    vocabulary = character.emotions if character else None

    display = " ".join(text.split())
    stripped, tagged_emotion, gestures = _extract_tags(text, vocabulary)
    spoken = " ".join(stripped.split())

    emotion = tagged_emotion or (_heuristic(spoken) if use_heuristics else "neutral")
    if not emotion:
        emotion = "neutral"

    mapping = ExpressionMapping()
    if character and emotion in character.emotions:
        mapping = character.emotions[emotion]

    # Gestures: what the LLM hinted at, plus the character's default for the
    # detected emotion (``actions`` in the character yaml).
    explicit = [g for g in dict.fromkeys(gestures) if g in KNOWN_GESTURES]
    default_action = None
    if character:
        candidate = character.actions.get(emotion)
        if candidate and candidate in KNOWN_GESTURES:
            default_action = candidate

    actions = list(explicit)
    if default_action and default_action not in actions:
        actions.append(default_action)

    if emotion != "neutral" or actions:
        logger.debug("emotion={} actions={} for {!r}", emotion, actions, spoken[:40])

    return EmotionResult(
        spoken=spoken,
        display=display,
        emotion=emotion,
        mapping=mapping,
        actions=actions,
        explicit_gestures=explicit,
        default_action=default_action,
    )


__all__ = [
    "EMOTION_ALIASES",
    "GESTURE_NOTES",
    "KNOWN_GESTURES",
    "EmotionResult",
    "normalise_emotion",
    "parse_emotion",
]
