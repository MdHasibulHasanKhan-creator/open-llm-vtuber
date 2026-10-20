"""Application configuration.

Every setting can be provided through environment variables or a ``.env`` file.
The prefix is ``VTUBER_`` and nested keys are separated by ``__``::

    VTUBER_LLM__PROVIDER=openai
    VTUBER_LLM__BASE_URL=https://api.deepseek.com/v1
    VTUBER_TTS__VOICE=ja-JP-NanamiNeural

The web client can also patch a *session's* configuration at runtime
(see :func:`apply_patch`); patches are validated with the very same models so a
bad value can never crash a running session.
"""

from __future__ import annotations

import contextlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .security import is_placeholder, mask_secret

# --------------------------------------------------------------------------- #
#  Type aliases (kept in sync with the frontend `src/types/config.ts`)
# --------------------------------------------------------------------------- #
LLMProvider = Literal["openai", "ollama", "echo"]
TTSEngine = Literal["edge", "openai", "browser"]
ASREngine = Literal["local", "openai", "browser"]
AudioFormat = Literal["pcm_s16le", "mp3"]
CameraPreset = Literal["face", "bust", "full"]

BACKEND_DIR = Path(__file__).resolve().parent.parent
CHARACTERS_DIR = BACKEND_DIR / "characters"


#: The origins this app is reachable at while developing or running normally.
#: Exported so the CORS middleware, the WebSocket check and the same-origin guard
#: on settings writes all agree on one list instead of drifting apart.
LOCAL_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    #: Origins allowed to call the REST API. Covers both the Vite dev server
    #: (5173) and the backend's own port, on `localhost` and on `127.0.0.1`,
    #: because which one a browser shows in the address bar is up to the user -
    #: and a mismatched pair is the classic "why is my WebSocket rejected".
    cors_origins: list[str] = Field(default_factory=lambda: list(LOCAL_CORS_ORIGINS))
    #: Extra origins allowed to open the WebSocket, on top of `cors_origins`.
    #: `["*"]` disables the check entirely. Same-origin connections are always
    #: allowed, so a LAN address or a real domain needs no configuration here.
    ws_allowed_origins: list[str] = Field(default_factory=list)
    #: Serve ``frontend/dist`` (if it exists) from the API port.
    serve_frontend: bool = True
    frontend_dist: str = "../frontend/dist"
    #: Maximum number of simultaneously connected clients.
    max_sessions: int = 8


class LogConfig(BaseModel):
    level: Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: str = "logs/backend.log"
    colorize: bool = True
    #: Optional second sink that writes one JSON object per line (log
    #: aggregators read this happily). Empty disables it.
    json_file: str = ""


class FallbackTarget(BaseModel):
    """One backup provider, tried when the configured one does not answer.

    Deliberately small: where it is, what model it serves, and - for a gateway
    that wants one - its own credential. The default is a single local Ollama,
    which is the offline path this app already recommends.
    """

    provider: LLMProvider = "ollama"
    model: str = "llama3.2:3b"
    base_url: str = "http://127.0.0.1:11434/v1"
    #: Left empty on purpose. A link never inherits the *primary* provider's key:
    #: a placeholder such as ``"ollama"`` resolves to the stored key of whatever
    #: provider the UI configured, so inheriting it would send a Groq credential
    #: to a local server. Empty means "send a fixed, non-secret literal".
    api_key: str = ""


class LLMConfig(BaseModel):
    """Language model used to generate the reply stream."""

    provider: LLMProvider = "openai"
    model: str = "llama3.1:8b"
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    temperature: float = 0.8
    #: Nucleus sampling. Slightly below 1 keeps replies coherent, which also
    #: makes them less likely to loop.
    top_p: float = 0.9
    max_tokens: int = 512
    #: Penalties are the cheapest fix for a model that parrots the user or
    #: repeats itself: `presence` pushes the model off topics it already used,
    #: `frequency` damps tokens it has used a lot. Both default to a mild value
    #: because too much of either produces rambling.
    presence_penalty: float = 0.35
    frequency_penalty: float = 0.35
    timeout: float = 90.0
    #: Rolling window of messages (excluding the system prompt) sent to the LLM.
    max_context_messages: int = 24
    #: Extra provider specific body fields, e.g. ``{"top_p": 0.9}``.
    extra_body: dict[str, Any] = Field(default_factory=dict)
    #: When the configured provider is unreachable at startup, switch this
    #: process to the offline "echo" brain instead of failing every turn. Off by
    #: default: silently answering with canned text is worse than a clear warning
    #: unless the user asked for it (`VTUBER_LLM__FALLBACK_TO_ECHO=true`).
    fallback_to_echo: bool = False
    #: Before falling back to *anything*, look for a real model server on the
    #: well-known local ports (Ollama 11434, LM Studio 1234, llama.cpp 8080, ...)
    #: and adopt the first one that answers with at least one model. This is what
    #: turns "the app replies with a script" into "the app found your model".
    #: Only local endpoints are probed - nothing is ever sent to the cloud by it.
    auto_discover: bool = True
    #: Ordered backups used when the configured provider fails **before the first
    #: token** of a reply. A reply that has already started streaming is never
    #: spliced from two models: the seam is audible. The offline "echo" brain is
    #: never a link here - a scripted answer is the failure this list exists to
    #: avoid (`fallback_to_echo` stays the explicit opt-in).
    #: Set `VTUBER_LLM__FALLBACK_CHAIN=[]` to disable, or give the whole list as
    #: JSON, e.g. `[{"provider": "ollama", "model": "llama3.1:8b"}]`.
    fallback_chain: list[FallbackTarget] = Field(default_factory=lambda: [FallbackTarget()])
    #: Seconds a link is skipped after it failed, so a dead endpoint costs one
    #: timeout per minute instead of one per turn. The primary is retried first
    #: the moment the cooldown lapses, so a recovered cloud model is used again
    #: without restarting the app.
    fallback_cooldown: float = 60.0
    # -- reply-quality guard ---------------------------------------------- #
    #: Detect the two failure modes that make a companion feel broken - a reply
    #: that only parrots the user's words back, and a reply that repeats the
    #: previous one - and regenerate **once**. Only the first sentence is judged,
    #: so the guard can never talk over audio that has already been spoken.
    quality_guard: bool = True
    #: Share of the reply's content words that may come from the user's message
    #: before it counts as parroting.
    echo_threshold: float = 0.75
    #: Share of content words that may repeat the previous assistant reply.
    repeat_threshold: float = 0.8
    #: Appended to the character's system prompt (global style rules).
    system_prompt_suffix: str = (
        "Never use markdown, emoji, stage directions or roleplay asterisks in "
        "your replies: everything you write is spoken out loud by a "
        "text-to-speech engine. Keep answers short (one to three sentences) "
        "unless the user asks for detail."
    )

    def effective_api_key(self) -> str:
        """The key to actually send on the wire.

        An inline key (an environment variable, ``.env``) always wins. A
        *placeholder* (``"ollama"``, ``"none"``) means "no authentication", so
        the key stored by the settings UI takes over - which is what lets the UI
        install a Groq key without rewriting ``.env``.
        """
        from .secrets_store import get_llm_api_key

        configured = (self.api_key or "").strip()
        if not is_placeholder(configured):
            return configured
        return get_llm_api_key() or ""

    @field_validator("fallback_chain", mode="before")
    @classmethod
    def _coerce_chain(cls, value: Any) -> Any:
        """Accept the two shapes a person actually writes down.

        ``["ollama"]`` - just provider names, each on that provider's defaults -
        is what we get from a config patch; ``[{"provider": ..., "model": ...}]``
        is the full form. Anything else is handed to pydantic to reject with its
        own message rather than being guessed at.
        """
        if isinstance(value, str):
            names = [name.strip() for name in value.split(",")]
            return [{"provider": name} for name in names if name]
        if isinstance(value, (list, tuple)):
            return [{"provider": item} if isinstance(item, str) else item for item in value]
        return value


class TTSConfig(BaseModel):
    """Speech synthesis + the transport used to ship audio to the browser."""

    engine: TTSEngine = "edge"
    voice: str = "en-US-AriaNeural"
    #: Speak each reply in the language it is written in by switching to a voice
    #: that speaks it (Edge only - see ``app/tts/voices.py``). With it on, one
    #: persona answers English, Bengali and Banglish *out loud* instead of
    #: reading two of them with English phonemes. A voice that already matches
    #: the sentence's language always wins, so this never overrides a choice.
    auto_voice: bool = True
    #: edge-tts style rate/pitch/volume modifiers, reused by the OpenAI engine
    #: where possible. ``"+10%"``, ``"-5%"``, ``"+20Hz"`` ...
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    #: ``pcm_s16le`` -> the browser receives ready-to-play samples (lowest
    #: latency, needs a server side decoder). ``mp3`` -> compressed passthrough.
    format: AudioFormat = "pcm_s16le"
    sample_rate: int = 24000
    #: Size of each binary WebSocket frame.
    chunk_ms: int = 200
    #: OpenAI compatible TTS settings (engine == "openai").
    model: str = "tts-1"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""

    @field_validator("rate", "pitch", "volume")
    @classmethod
    def _normalise_modifier(cls, value: str) -> str:
        value = value.strip()
        if value in ("", "0", "0%", "+0%", "+0Hz"):
            return value
        # Accept "10%" and normalise to "+10%".
        if value[0] not in "+-":
            return f"+{value}"
        return value


class TranscriptConfig(BaseModel):
    """Input-side filtering of what the microphone produced.

    See :mod:`app.pipeline.transcript` for the reasoning. Dropping a *real*
    utterance is worse than accepting noise, so every rule is narrow: only
    silence-like output, known ASR hallucinations, the avatar's own voice coming
    back through the speakers, and (opt-in) an exact double-submit are rejected.
    A bare "no" or "ok" still gets a turn.
    """

    enabled: bool = True
    #: Reject a transcript that is a near-copy of something the avatar just said:
    #: the classic "speakers on, microphone hears the TTS" loop.
    echo_guard: bool = True
    #: Similarity (0..1) at or above which a transcript counts as an echo.
    echo_similarity: float = 0.72
    #: How many of the avatar's most recent sentences to compare against.
    echo_window: int = 4
    #: Reject a transcript that repeats the previous accepted one. Off by default
    #: because repeating yourself when you think you were not heard is normal.
    dedupe: bool = False
    dedupe_similarity: float = 0.95
    #: Transcripts shorter than this are dropped as noise. 2 keeps every real
    #: answer ("no", "ok", "হ্যাঁ") and removes single stray characters.
    min_chars: int = 2
    #: A *plausible* short phrase ("thank you") is only treated as a
    #: hallucination when the audio it came from was shorter than this many
    #: seconds - a genuine "thank you" takes longer than that to say.
    short_utterance_seconds: float = 0.9
    #: Extra hallucination phrases on top of the built-in list.
    extra_noise_phrases: list[str] = Field(default_factory=list)


class ASRConfig(BaseModel):
    """Speech recognition of the microphone stream coming from the browser."""

    engine: ASREngine = "local"
    model: str = "base.en"
    device: str = "auto"  # auto | cpu | cuda
    compute_type: str = "int8"  # int8 | int8_float16 | float16 | float32
    language: str | None = "en"
    beam_size: int = 1
    #: OpenAI compatible transcription endpoint.
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    #: Optional vocabulary hint.
    prompt: str = ""
    #: Drop utterances shorter than this (seconds of speech) - avoids hallucinated
    #: transcripts from clicks and room noise.
    min_speech_seconds: float = 0.35
    #: What to do with a transcript once we have it (echo / noise / duplicate).
    transcript: TranscriptConfig = Field(default_factory=TranscriptConfig)


class LipsyncConfig(BaseModel):
    """Tuning for the frontend blendshape driver.

    Two modes, and the second one is the default because it looks better on
    almost every model:

    ``mouth``
        The mouth is one value: "open by this much". The server still ships the
        ``volumes`` envelope (one normalised RMS value per ``slice_ms``), and the
        client opens the ``aa`` blendshape by the part of that envelope above
        ``mouth_threshold``. No visemes, no phoneme guessing, nothing to jitter -
        which is exactly why it reads as natural on stylised avatars.
    ``visemes``
        ``aa``/``ih``/``ou``/``ee``/``oh`` changes driven by a grapheme-to-viseme
        timeline (see ``app/lipsync``). More expressive on a model with good
        mouth shapes, more fragile everywhere else.
    """

    #: ``mouth`` (amplitude only) or ``visemes`` (full mouth-shape timeline).
    mode: Literal["mouth", "visemes"] = "mouth"

    enabled: bool = True
    #: Exponential smoothing time-constants (ms). Attack = rise of a mouth
    #: shape, release = fall. Frame-rate independent (see LipSyncController).
    #: A release a little slower than the attack is what makes a mouth look like
    #: it is *closing* rather than being switched off between syllables.
    attack_ms: float = 40.0
    release_ms: float = 120.0
    #: Global multiplier applied to every viseme weight.
    viseme_gain: float = 1.0
    #: Modulate the mouth opening with the instantaneous audio amplitude.
    amplitude_modulation: bool = True
    amplitude_floor: float = 0.45
    #: Server side RMS window used to build the `volumes` array.
    slice_ms: int = 20

    # -- `mouth` mode ----------------------------------------------------- #
    #: Envelope level below which the mouth stays shut (0..1). Raise it in a
    #: noisy room so room tone does not make the avatar chew.
    mouth_threshold: float = 0.12
    #: Extra drop below `mouth_threshold` needed to *close* again. Prevents
    #: flutter when the level sits right on the threshold.
    mouth_hysteresis: float = 0.05
    #: How far a loud syllable opens the mouth. The envelope is normalised to
    #: its own peak, so 1.9 used to map a normal vowel to a fully open jaw; 0.7
    #: gives conversational speech a natural, partial opening.
    mouth_scale: float = 0.72
    #: Ceiling for the resulting openness, 0..1. A VRoid `aa` at 1.0 is a shout,
    #: not a sentence, so speech is capped short of it.
    mouth_max: float = 0.68
    #: Minimum time (ms) the mouth stays open/closed once it has switched, so
    #: syllable-rate audio cannot make it flap faster than the eye accepts.
    mouth_min_dwell_ms: float = 60.0


class MemoryConfig(BaseModel):
    """Long-term memory of the person using the app.

    One JSON profile, no dependencies, no model required. Disable it and the
    companion still works - it simply stops claiming to remember anything.
    """

    enabled: bool = True
    #: Relative to ``backend/``. Human-editable JSON.
    path: str = "memory/profile.json"
    #: Re-summarise the conversation every N turns (see the orchestrator). Each
    #: refresh costs one small model call, and only runs once the reply it
    #: belongs to has finished playing.
    summarise_every: int = 8
    #: Inject the rendered profile block into the system prompt.
    inject: bool = True


class AvatarConfig(BaseModel):
    character: str = "default"
    #: Empty -> the frontend falls back to its bundled placeholder model.
    vrm_url: str = ""
    camera_preset: CameraPreset = "bust"
    pixel_ratio: int = 2
    enable_shadows: bool = False
    #: Show the FPS/CPU render stats overlay.
    show_stats: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VTUBER_",
        env_nested_delimiter="__",
        # `.env` holds the shared, documented defaults; `.env.local` holds
        # machine-specific overrides and wins, because a later file in the tuple
        # takes precedence. Environment variables beat both.
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    asr: ASRConfig = Field(default_factory=ASRConfig)
    lipsync: LipsyncConfig = Field(default_factory=LipsyncConfig)
    avatar: AvatarConfig = Field(default_factory=AvatarConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    def public_dict(self) -> dict[str, Any]:
        """Serialisable configuration with secrets masked (for the web UI).

        The real value never crosses this boundary. A stored key is reported as a
        short fingerprint (``gsk_1f2e****9c3a``) - enough to recognise *which*
        key is in use, useless to anyone else - and an endpoint that needs no
        authentication reports an empty string rather than its placeholder
        (``"ollama"``), which used to read as "a key is configured".
        """
        from .secrets_store import get_llm_api_key

        data = self.model_dump(mode="json")
        for section in ("tts", "asr"):
            data[section]["api_key"] = mask_secret(data.get(section, {}).get("api_key"))

        llm = data.setdefault("llm", {})
        fingerprint = mask_secret(llm.get("api_key")) or mask_secret(get_llm_api_key())
        llm["api_key"] = fingerprint
        llm["api_key_set"] = bool(fingerprint)
        # A backup link may legitimately carry a key of its own (a second
        # gateway); it gets the same treatment as every other credential.
        for link in llm.get("fallback_chain") or []:
            if isinstance(link, dict):
                link["api_key"] = mask_secret(link.get("api_key"))
        return data


# --------------------------------------------------------------------------- #
#  Runtime patching
# --------------------------------------------------------------------------- #
#: Keys the web UI is allowed to change on a live session.
PATCHABLE_SECTIONS = ("llm", "tts", "asr", "lipsync", "avatar", "memory")


def _strip_masked_key(item: Any) -> Any:
    """Translate a masked fingerprint in a fallback link back to "no key".

    The UI only ever receives ``gsk_1f2e****9c3a``, so echoing that value means
    "unchanged". Storing the asterisks as a literal key would silently break the
    link, so empty is the honest translation.
    """
    if isinstance(item, dict) and "****" in str(item.get("api_key", "")):
        return {**item, "api_key": ""}
    return item


def apply_patch(settings: Settings, patch: dict[str, Any]) -> Settings:
    """Return a *new* :class:`Settings` with ``patch`` merged in.

    ``patch`` has the shape ``{"tts": {"voice": "..."}, "llm": {"model": "x"}}``.
    Unknown sections/keys are ignored (never trust the client), and the masked
    placeholder ``********`` keeps the current secret.
    """
    if not patch:
        return settings

    merged = settings.model_dump(mode="json")
    for section in PATCHABLE_SECTIONS:
        incoming = patch.get(section)
        if not isinstance(incoming, dict):
            continue
        target = merged.setdefault(section, {})
        for key, value in incoming.items():
            if key not in target:
                continue  # ignore unknown keys
            if isinstance(value, str) and "****" in value:
                # A masked fingerprint (`gsk_1f2e****9c3a`) means "unchanged":
                # the client has never seen the real value, so it cannot send it.
                continue
            if key == "fallback_chain" and isinstance(value, list):
                target[key] = [_strip_masked_key(item) for item in value]
                continue
            target[key] = value

    try:
        return Settings(**merged)
    except ValidationError as exc:  # pragma: no cover - defensive
        raise ValueError(f"invalid config patch: {exc.errors()}") from exc


# --------------------------------------------------------------------------- #
#  Character / persona files
# --------------------------------------------------------------------------- #
class ExpressionMapping(BaseModel):
    """Maps an LLM facing emotion name to a VRM preset expression."""

    preset: str = "neutral"
    weight: float = 0.7
    hold_ms: int = 2200
    #: When true the frontend suppresses blinking while the emotion is active.
    override_blink: bool = False


class CharacterConfig(BaseModel):
    name: str = "Ai"
    #: Short description shown in the UI.
    tagline: str = "Your local AI companion"
    language: str = "en"
    greeting: str = "Hi! I'm here - what would you like to talk about?"
    system_prompt: str = "You are a friendly, curious AI companion living in a 3D avatar."
    tts_voice: str = ""
    tts_rate: str = ""
    emotions: dict[str, ExpressionMapping] = Field(
        default_factory=lambda: {
            "neutral": ExpressionMapping(preset="neutral", weight=0.0, hold_ms=0),
            "happy": ExpressionMapping(preset="happy", weight=0.75, hold_ms=2600),
            "angry": ExpressionMapping(preset="angry", weight=0.6, hold_ms=1800),
            "sad": ExpressionMapping(preset="sad", weight=0.65, hold_ms=2600),
            "relaxed": ExpressionMapping(preset="relaxed", weight=0.55, hold_ms=2600),
            "surprised": ExpressionMapping(preset="surprised", weight=0.8, hold_ms=1500, override_blink=True),
            # VRM has no "thinking" or "shy" preset, so both borrow `relaxed`
            # with a lower weight and lean on a gesture (head tilt / blush) for
            # the distinction. They are first-class names regardless: the model
            # can tag them and the frontend treats them as real emotions.
            "thinking": ExpressionMapping(preset="relaxed", weight=0.35, hold_ms=1800),
            "shy": ExpressionMapping(preset="relaxed", weight=0.5, hold_ms=2400),
        }
    )
    #: Sentence -> gesture hints emitted by :mod:`app.emotion`.
    actions: dict[str, str] = Field(
        default_factory=lambda: {
            "happy": "nod",
            "surprised": "tilt",
            "angry": "shake",
            "sad": "sigh",
            "thinking": "think",
            "shy": "blush",
        }
    )

    def resolve_voice(self, fallback: str) -> str:
        return self.tts_voice.strip() or fallback


#: Fields the settings UI is allowed to edit. Everything else in a persona file
#: (the emotion map, the gesture map) stays a hand-edited ``.yaml`` concern: it
#: is tuned per avatar, not per conversation, and a text box is the wrong tool.
EDITABLE_CHARACTER_FIELDS: tuple[str, ...] = (
    "name",
    "tagline",
    "language",
    "greeting",
    "system_prompt",
    "tts_voice",
    "tts_rate",
)

#: Longest accepted value per editable field. The system prompt is sent on every
#: single turn, so this keeps a paste accident from becoming a very expensive one.
_CHARACTER_FIELD_LIMITS: dict[str, int] = {
    "name": 60,
    "tagline": 160,
    "language": 24,
    "greeting": 600,
    "system_prompt": 8000,
    "tts_voice": 80,
    "tts_rate": 8,
}

#: Fields where "" is a *value*, not "throw my edit away".
#:
#: Emptying the greeting box means "do not speak on connect", which is a real
#: request; an empty name or voice is not. Every other field treats "" as a reset.
_EMPTY_MEANS_EMPTY = frozenset({"greeting"})


def character_override_path(name: str) -> Path:
    """Where a saved edit of persona ``name`` lives.

    Deliberately *not* the ``.yaml`` itself. The shipped file is heavily commented
    and read by humans, and round-tripping it through a YAML dumper would throw
    all of that away; writing beside it also makes "reset" a single unlink and
    keeps the edit out of version control (both ignore files cover ``*.user.json``).
    """
    return CHARACTERS_DIR / f"{_character_slug(name)}.user.json"


def _character_slug(name: str) -> str:
    """A persona id that is safe to turn into a filename."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", (name or "").strip()) or "default"


def character_exists(name: str) -> bool:
    return (CHARACTERS_DIR / f"{_character_slug(name)}.yaml").exists()


def load_character_override(name: str) -> dict[str, str]:
    """The saved edit for ``name`` - editable, string-valued fields only."""
    path = character_override_path(name)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable character override {}: {}", path.name, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if key in EDITABLE_CHARACTER_FIELDS
        and isinstance(value, str)
        and (value or key in _EMPTY_MEANS_EMPTY)
    }


def save_character_override(name: str, fields: dict[str, Any]) -> dict[str, str]:
    """Validate and persist an edit; returns the override now in force.

    An empty value normally *removes* the field rather than storing ``""``,
    because "" and "unchanged" are not the same thing. The greeting is the one
    exception (see :data:`_EMPTY_MEANS_EMPTY`): clearing that box has to actually
    silence her, so the emptiness itself is stored.
    """
    current = load_character_override(name)
    for key, value in fields.items():
        if key not in EDITABLE_CHARACTER_FIELDS:
            raise ValueError(f"'{key}' is not an editable persona field")
        if not isinstance(value, str):
            raise ValueError(f"'{key}' must be a string")
        cleaned = _clean_character_value(key, value)
        if cleaned or key in _EMPTY_MEANS_EMPTY:
            current[key] = cleaned
        else:
            current.pop(key, None)

    path = character_override_path(name)
    if not current:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        logger.info("persona '{}' reset to its shipped file", name)
        return {}

    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a session reading the file mid-save sees either the old
    # persona or the new one, never half of one.
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    logger.info("persona '{}' edited: {}", name, ", ".join(sorted(current)))
    return current


def clear_character_override(name: str) -> bool:
    """Forget a saved edit. True when there was one."""
    path = character_override_path(name)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    logger.info("persona '{}' reset to its shipped file", name)
    return True


def _clean_character_value(key: str, value: str) -> str:
    """Normalise whitespace, strip control characters, enforce the length cap."""
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if key != "system_prompt":
        text = " ".join(text.split())  # one-liners stay one-liners
    text = "".join(char for char in text if char == "\n" or unicodedata.category(char)[0] != "C")
    limit = _CHARACTER_FIELD_LIMITS.get(key, 200)
    if len(text) > limit:
        raise ValueError(f"'{key}' is limited to {limit} characters (got {len(text)})")
    return text


def _shipped_character(name: str) -> CharacterConfig:
    """``characters/<name>.yaml`` exactly as it was written, no edits applied."""
    path = CHARACTERS_DIR / f"{_character_slug(name)}.yaml"
    if not path.exists():
        return CharacterConfig()
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return CharacterConfig(**raw)


def load_character(name: str) -> CharacterConfig:
    """Load ``characters/<name>.yaml``, then apply any saved UI edit."""
    base = _shipped_character(name)
    overrides = load_character_override(name)
    if not overrides:
        return base
    # Re-validated rather than patched in place: a saved edit can then never turn
    # a working persona into a broken one (and a *corrupt* one is ignored).
    try:
        return CharacterConfig(**{**base.model_dump(), **overrides})
    except ValidationError as exc:
        logger.warning("ignoring the saved edits for persona '{}': {}", name, exc)
        return base


def character_detail(name: str) -> dict[str, Any]:
    """Everything the persona editor needs: effective, shipped, and what differs."""
    shipped = _shipped_character(name)
    overrides = load_character_override(name)
    return {
        "id": name,
        "editable": list(EDITABLE_CHARACTER_FIELDS),
        "overridden": sorted(overrides),
        "shipped": {field: getattr(shipped, field) for field in EDITABLE_CHARACTER_FIELDS},
        "character": load_character(name).model_dump(mode="json"),
    }


def list_characters() -> list[dict[str, str]]:
    """Discover available personas for the settings UI."""
    import yaml

    found: list[dict[str, str]] = []
    if CHARACTERS_DIR.exists():
        for file in sorted(CHARACTERS_DIR.glob("*.yaml")):
            try:
                raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
            except Exception as exc:  # malformed user file
                logger.warning("skipping unreadable character file {}: {}", file.name, exc)
                continue
            # An edited persona is presented as it now reads, not as it shipped.
            override = load_character_override(file.stem)
            found.append(
                {
                    "id": file.stem,
                    "name": str(override.get("name") or raw.get("name", file.stem)),
                    "tagline": str(override.get("tagline") or raw.get("tagline", "")),
                    "language": str(override.get("language") or raw.get("language", "en")),
                }
            )
    if not found:
        found.append({"id": "default", "name": "Ai", "tagline": "", "language": "en"})
    return found


# --------------------------------------------------------------------------- #
#  Public entry points
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process wide settings (cached - call once per process)."""
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def dump_settings(settings: Settings, path: Path) -> None:
    path.write_text(json.dumps(settings.public_dict(), indent=2), encoding="utf-8")


__all__ = [
    "ASRConfig",
    "AvatarConfig",
    "CharacterConfig",
    "LLMConfig",
    "LipsyncConfig",
    "MemoryConfig",
    "ServerConfig",
    "Settings",
    "TTSConfig",
    "apply_patch",
    "get_settings",
    "list_characters",
    "load_character",
    "reload_settings",
]
