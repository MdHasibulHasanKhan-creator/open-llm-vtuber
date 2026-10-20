"""One connected client.

Responsibilities
----------------
* own the WebSocket and serialise every write through a lock (text and binary
  frames from different tasks must never interleave)
* validate inbound messages against the protocol models
* buffer microphone PCM between ``audio.start`` and ``audio.end``
* host the :class:`ConversationOrchestrator` and expose it as a ``Transport``
* apply runtime configuration patches coming from the settings UI
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from fastapi import WebSocket
from loguru import logger
from pydantic import TypeAdapter, ValidationError

from .asr import create_asr
from .asr.factory import available_engines as asr_engines
from .config import PATCHABLE_SECTIONS, CharacterConfig, Settings, list_characters, load_character
from .llm.factory import available_providers as llm_providers
from .pipeline.orchestrator import ConversationOrchestrator
from .schemas import ClientMessage, Msg, SessionState
from .tts.factory import available_engines as tts_engines
from .utils.audio import DecodedAudio, from_pcm16

#: Reject absurd utterance buffers (30 MB ~= 15 min of 16 kHz mono PCM).
MAX_AUDIO_BYTES = 30 * 1024 * 1024

_client_message_adapter: TypeAdapter[Any] = TypeAdapter(ClientMessage)


class ClientSession:
    """A live conversation with one browser tab."""

    def __init__(self, websocket: WebSocket, settings: Settings, session_id: str | None = None) -> None:
        self.websocket = websocket
        self.settings = settings
        self.id = session_id or uuid.uuid4().hex[:12]
        self.created_at = time.time()
        self.state: SessionState = "idle"

        self.character: CharacterConfig = load_character(settings.avatar.character)
        self._send_lock = asyncio.Lock()

        # Microphone accumulation.
        self._audio = bytearray()
        self._capture_rate = 16000
        self._capturing = False

        self.orchestrator = ConversationOrchestrator(
            settings=settings,
            character=self.character,
            transport=self,
            asr=create_asr(settings.asr),
            session_id=self.id,
        )

    # ------------------------------------------------------------------ #
    #  Transport (duck-typed by the orchestrator)
    # ------------------------------------------------------------------ #
    async def send_json(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.websocket.send_text(
                json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            )

    async def send_bytes(self, payload: bytes) -> None:
        async with self._send_lock:
            await self.websocket.send_bytes(payload)

    async def set_state(self, state: SessionState, detail: str = "") -> None:
        self.state = state
        await self.send_json(Msg.state(state, detail))

    # ------------------------------------------------------------------ #
    #  Connection lifecycle
    # ------------------------------------------------------------------ #
    async def send_ready(self) -> None:
        """Handshake: configuration, persona and capabilities in one message."""
        await self.send_json(
            Msg.ready(
                session_id=self.id,
                config=self.settings.public_dict(),
                character=self.character.model_dump(mode="json"),
                capabilities={
                    "llm": llm_providers(),
                    "tts": tts_engines(),
                    "asr": asr_engines(),
                    "characters": list_characters(),
                    "audio": {
                        "transport": self.settings.tts.format,
                        "sample_rate": self.settings.tts.sample_rate,
                        "chunk_ms": self.settings.tts.chunk_ms,
                        "capture_sample_rate": 16000,
                    },
                    "visemes": ["aa", "ih", "ou", "ee", "oh", "nn"],
                    "expressions": [
                        "neutral", "happy", "angry", "sad", "relaxed", "surprised",
                        "blink", "blinkLeft", "blinkRight",
                        "lookUp", "lookDown", "lookLeft", "lookRight",
                    ],
                },
            )
        )

    async def close(self) -> None:
        logger.info("closing session {}", self.id)
        await self.orchestrator.aclose()

    # ------------------------------------------------------------------ #
    #  Inbound frames
    # ------------------------------------------------------------------ #
    async def handle_text(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_json(Msg.error("Malformed JSON frame", code="protocol"))
            return

        try:
            message = _client_message_adapter.validate_python(payload)
        except ValidationError as exc:
            logger.warning("invalid client message: {}", exc.errors()[:2])
            await self.send_json(Msg.error("Unsupported or invalid message", code="protocol"))
            return

        logger.log("TRACE", "<- {} {}", self.id, message.type)

        match message.type:
            case "hello":
                logger.info(
                    "client {} v{} (client_side_asr={}, greet={})",
                    message.client, message.version, message.client_side_asr, message.greet,
                )
                await self.send_json(
                    Msg.custom("hello.ack", session_id=self.id, server_time=time.time())
                )
                # The client is loaded and rendering: now it is safe to greet.
                if message.greet:
                    await self.orchestrator.start()

            case "ping":
                await self.send_json(Msg.pong(message.t, time.time()))

            case "chat":
                await self.orchestrator.submit_text(message.text, source=message.source)

            case "audio.start":
                self._capture_rate = max(8000, min(48000, int(message.sample_rate or 16000)))
                self._audio = bytearray()
                self._capturing = True
                await self.set_state("listening")

            case "audio.end":
                self._capturing = False
                await self._flush_audio()

            case "interrupt":
                await self.orchestrator.interrupt(heard=message.heard)

            case "config":
                await self.apply_config(message.payload)

            case "character.reload":
                await self.reload_character()

            case "reset":
                await self.orchestrator.reset()

    async def handle_binary(self, data: bytes) -> None:
        """Raw PCM from the microphone."""
        if not self._capturing:
            # Tolerate clients that start streaming without an explicit
            # `audio.start` (start capturing lazily).
            self._capturing = True
        if len(self._audio) + len(data) > MAX_AUDIO_BYTES:
            logger.warning("session {}: utterance buffer overflow, dropping", self.id)
            self._audio = bytearray()
            self._capturing = False
            await self.send_json(Msg.error("Utterance too long - buffer dropped", code="audio"))
            return
        self._audio.extend(data)

    async def _flush_audio(self) -> None:
        if not self._audio:
            await self.set_state("idle")
            return

        audio = DecodedAudio(from_pcm16(bytes(self._audio)), self._capture_rate)
        self._audio = bytearray()
        logger.debug(
            "session {}: {} bytes of microphone audio ({:.2f}s @ {}Hz)",
            self.id, audio.samples.nbytes, audio.duration, self._capture_rate,
        )
        await self.orchestrator.submit_audio(audio)

    # ------------------------------------------------------------------ #
    #  Runtime configuration
    # ------------------------------------------------------------------ #
    async def apply_config(self, payload: dict[str, Any]) -> None:
        if not payload:
            return

        before = self.orchestrator.settings
        try:
            updated = await self.orchestrator.update_settings(payload)
        except ValueError as exc:
            await self.send_json(Msg.error(str(exc), code="config"))
            return

        # The persona is server side state: reload it when it changed.
        if updated.avatar.character != before.avatar.character:
            self.character = load_character(updated.avatar.character)
            self.orchestrator.set_character(self.character)

        self.settings = updated
        before_dump, after_dump = before.model_dump(mode="json"), updated.model_dump(mode="json")
        changed = [section for section in PATCHABLE_SECTIONS if before_dump[section] != after_dump[section]]
        logger.info("session {} config patched: {}", self.id, changed or ["(no change)"])
        await self.send_json(
            Msg.config_ack(
                {
                    **updated.public_dict(),
                    "character": self.character.model_dump(mode="json"),
                },
                changed,
            )
        )

    async def reload_character(self) -> None:
        """Re-read the persona file after the settings UI saved an edit.

        Sent as a ``config.ack`` so the client's existing config path picks up the
        new name, language and voice - "the persona changed" and "the settings
        changed" are the same client-side operation.
        """
        name = self.settings.avatar.character
        self.character = load_character(name)
        # An edit of the persona in use must not wipe the conversation.
        self.orchestrator.set_character(self.character, keep_history=True)
        await self.send_json(
            Msg.config_ack(
                {
                    **self.settings.public_dict(),
                    "character": self.character.model_dump(mode="json"),
                },
                ["character"],
            )
        )


__all__ = ["MAX_AUDIO_BYTES", "ClientSession"]
