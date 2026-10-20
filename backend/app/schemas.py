"""Wire protocol shared between the FastAPI backend and the web client.

The protocol is a thin JSON layer over one WebSocket. Design rules:

* **Text frames** are always JSON control/state messages with a ``type`` field.
* **Binary frames** are raw audio. A binary frame always belongs to the most
  recent :class:`TtsStart` message, which is guaranteed to have been written
  before it (WebSocket preserves ordering). This avoids base64 bloat and keeps
  audio latency minimal.
* Every message is small, flat and easy to log.

See ``docs/PROTOCOL.md`` for the full reference and sequence diagrams.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
#  Client -> Server
# --------------------------------------------------------------------------- #


class HelloMessage(BaseModel):
    """Second frame sent by a client, once the avatar is loaded and rendering.

    The backend waits for it before starting the greeting, so the first words
    are never spoken into an empty stage (loading a VRM can take a moment).
    """

    type: Literal["hello"] = "hello"
    client: str = "web"
    version: str = "1.0.0"
    #: Sample rate the browser will send microphone PCM at.
    sample_rate: int = 16000
    #: True when the client transcribes locally (Web Speech API) and will use
    #: ``chat`` messages instead of ``audio.*``.
    client_side_asr: bool = False
    #: Set to false to stay silent on connect.
    greet: bool = True


class ChatMessage(BaseModel):
    """A typed utterance (from the text box or client-side ASR)."""

    type: Literal["chat"] = "chat"
    text: str
    #: ``typed`` (the text box) is taken at face value - the user meant it.
    #: ``asr`` (the browser's own speech recognition) is filtered by the
    #: transcript gate exactly like audio the server transcribes itself.
    source: Literal["typed", "asr"] = "typed"


class AudioStart(BaseModel):
    """Start of an utterance; binary PCM frames follow."""

    type: Literal["audio.start"] = "audio.start"
    sample_rate: int = 16000


class AudioEnd(BaseModel):
    """End of an utterance -> run ASR, then the LLM."""

    type: Literal["audio.end"] = "audio.end"


class InterruptMessage(BaseModel):
    """Barge-in: stop speaking immediately and abandon the current turn."""

    type: Literal["interrupt"] = "interrupt"
    #: Optional partial transcript the user already heard, stored in history.
    heard: str = ""


class ConfigMessage(BaseModel):
    """Runtime configuration patch (voice, model, lip-sync tuning, ...)."""

    type: Literal["config"] = "config"
    payload: dict[str, Any] = Field(default_factory=dict)


class ResetMessage(BaseModel):
    """Clear the conversation history for this session."""

    type: Literal["reset"] = "reset"


class CharacterReloadMessage(BaseModel):
    """Re-read the persona file after the settings UI saved an edit.

    Deliberately *not* a config patch: the persona is server-side state that lives
    in a file, and re-sending ``avatar.character`` would reload nothing (the id has
    not changed). This says "the file under you changed - read it again".
    """

    type: Literal["character.reload"] = "character.reload"


class PingMessage(BaseModel):
    type: Literal["ping"] = "ping"
    t: float = 0.0


ClientMessage = Annotated[
    HelloMessage
    | ChatMessage
    | AudioStart
    | AudioEnd
    | InterruptMessage
    | ConfigMessage
    | ResetMessage
    | CharacterReloadMessage
    | PingMessage,
    Field(discriminator="type"),
]

# --------------------------------------------------------------------------- #
#  Server -> Client
# --------------------------------------------------------------------------- #

SessionState = Literal["idle", "listening", "thinking", "speaking", "error"]


class TtsViseme(BaseModel):
    """One mouth shape occupying ``[start, end]`` seconds of the utterance.

    ``viseme`` is one of ``aa|ih|ou|ee|oh`` (VRM 1.0 mouth presets) or ``nn``
    ("mouth closed / neutral", produced by consonants and pauses).
    """

    start: float
    end: float
    viseme: str
    weight: float


class TtsStart(BaseModel):
    """Header for one spoken sentence. Binary audio frames follow until
    :class:`TtsEnd`.

    ``visemes`` is sent in a compact form - each entry is
    ``[start_seconds, end_seconds, viseme_name, weight]`` - because a sentence
    can easily contain 40 of them and this is the hottest path of the protocol.
    """

    type: Literal["tts.start"] = "tts.start"
    id: int
    #: Sentence index inside the current turn.
    index: int
    text: str
    #: ``pcm_s16le`` | ``mp3`` | ``browser`` (client speaks it itself).
    format: str
    sample_rate: int
    channels: int = 1
    duration: float
    #: Per-slice normalised RMS envelope (mirrors Open-LLM-VTuber's `volumes`).
    volumes: list[float] = Field(default_factory=list)
    #: BCP-47 locale of `text` (`bn-BD`, `en-US`), from the *turn's* language.
    #: The browser's `speechSynthesis` needs it: without it a Bengali sentence is
    #: read by an English voice, which is the "it sounds wrong in Bengali" bug.
    lang: str = ""
    #: ``[[start, end, "aa", 0.85], ...]`` - see :meth:`VisemeFrame.as_list`.
    #: Empty in ``mouth`` lip-sync mode, where the client drives one blendshape
    #: from ``volumes`` instead.
    visemes: list[list[float | str]] = Field(default_factory=list)
    #: Which interpretation the client should use: ``mouth`` (default) or
    #: ``visemes``. Sent per sentence so a mid-reply config patch cannot strand a
    #: sentence without lip-sync.
    lipsync_mode: str = "mouth"
    #: Emotion detected for this sentence, already mapped to a VRM preset.
    expression: dict[str, Any] | None = None
    #: Gesture hints, e.g. ``["nod"]``.
    actions: list[str] = Field(default_factory=list)
    #: Voice actually used (informative).
    voice: str = ""


class TtsEnd(BaseModel):
    type: Literal["tts.end"] = "tts.end"
    id: int


class ExpressionEvent(BaseModel):
    type: Literal["expression"] = "expression"
    preset: str
    #: The *emotion* that produced this preset, before it was mapped onto a VRM
    #: preset. Two emotions can share a preset - `shy` and `thinking` are both
    #: `relaxed` - and the client needs to tell them apart, because `shy` adds a
    #: blush and `thinking` does not.
    emotion: str = "neutral"
    weight: float = 0.7
    hold_ms: int = 2200
    override_blink: bool = False


class ActionEvent(BaseModel):
    type: Literal["action"] = "action"
    name: str
    intensity: float = 1.0


class _Msg:
    """Tiny builder namespace so the server never hand-writes a dict key."""

    @staticmethod
    def ready(
        session_id: str,
        config: dict[str, Any],
        character: dict[str, Any],
        capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "type": "ready",
            "session_id": session_id,
            "config": config,
            "character": character,
            "capabilities": capabilities,
        }

    @staticmethod
    def state(state: SessionState, detail: str = "") -> dict[str, Any]:
        return {"type": "state", "value": state, "detail": detail}

    @staticmethod
    def asr(text: str, final: bool, duration: float | None = None, reason: str = "") -> dict[str, Any]:
        """A transcript result.

        ``reason`` is empty when the transcript was accepted, and otherwise a
        code from :data:`app.pipeline.transcript.REASON_LABELS` explaining why
        the gate dropped it - so an empty ``text`` can be told apart from a
        microphone that simply heard nothing.
        """
        return {
            "type": "asr.final" if final else "asr.partial",
            "text": text,
            "duration": duration,
            "reason": reason,
        }

    @staticmethod
    def user_text(text: str) -> dict[str, Any]:
        return {"type": "user.text", "text": text}

    @staticmethod
    def llm_delta(text: str) -> dict[str, Any]:
        return {"type": "llm.delta", "text": text}

    @staticmethod
    def llm_restart() -> dict[str, Any]:
        """Throw away the streaming preview sent so far and start again.

        Sent when the quality guard rejects a reply's first sentence: the client
        clears its streaming bubble so it never shows text that is not spoken.
        """
        return {"type": "llm.restart"}

    @staticmethod
    def llm_done(text: str, usage: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"type": "llm.done", "text": text, "usage": usage or {}}

    @staticmethod
    def sentence(index: int, text: str, display_text: str) -> dict[str, Any]:
        """One completed sentence of the reply (for the chat bubble)."""
        return {"type": "llm.sentence", "index": index, "text": text, "display_text": display_text}

    @staticmethod
    def interrupted() -> dict[str, Any]:
        return {"type": "interrupted"}

    @staticmethod
    def config_ack(config: dict[str, Any], changed: list[str]) -> dict[str, Any]:
        return {"type": "config.ack", "config": config, "changed": changed}

    @staticmethod
    def metrics(**values: Any) -> dict[str, Any]:
        return {"type": "metrics", **values}

    @staticmethod
    def error(message: str, fatal: bool = False, code: str = "internal") -> dict[str, Any]:
        return {"type": "error", "message": message, "fatal": fatal, "code": code}

    @staticmethod
    def pong(t: float, server_time: float) -> dict[str, Any]:
        return {"type": "pong", "t": t, "server_time": server_time}

    @staticmethod
    def history(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "history", "messages": messages}

    @staticmethod
    def custom(message_type: str, **values: Any) -> dict[str, Any]:
        return {"type": message_type, **values}


Msg = _Msg

__all__ = [
    "ActionEvent",
    "AudioEnd",
    "AudioStart",
    "CharacterReloadMessage",
    "ChatMessage",
    "ClientMessage",
    "ConfigMessage",
    "ExpressionEvent",
    "HelloMessage",
    "InterruptMessage",
    "Msg",
    "PingMessage",
    "ResetMessage",
    "SessionState",
    "TtsEnd",
    "TtsStart",
    "TtsViseme",
]
