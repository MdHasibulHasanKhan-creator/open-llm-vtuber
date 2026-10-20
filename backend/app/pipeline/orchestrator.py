"""The conversation turn orchestration.

One "turn" is: user text -> LLM token stream -> sentence splitting ->
TTS (one worker) -> decode/analyse -> WebSocket frames.

                  +-----------+        +-----------+        +-----------+
   user text ---> |   LLM     |--tok-->|  sentence |--sq--> |   TTS     |
                  | streaming |        |  splitter |        |  worker   |
                  +-----------+        +-----------+        +-----+-----+
                                                                  |
                                          tts.start + binary PCM   v
   client  <--------------------------------------------------  transport

Design notes
------------
* **Latency first.** The first sentence is synthesised while the model is still
  writing the second one. The queue is bounded (``maxsize``) which applies
  back-pressure so we never synthesise far ahead of playback.
* **Ordering.** A single TTS worker consumes the queue, so audio always leaves in
  the order the sentences were produced.
* **Barge-in.** :meth:`interrupt` cancels the turn task; the worker is torn down
  and ``interrupted`` is sent so the client can flush its audio queue.
* **Instant emotion.** Expressions are emitted as soon as a sentence is known,
  *before* its audio has been synthesised, because a face that reacts 300 ms
  after the reply reads as broken.
* **Quality guard.** The reply's first sentence is judged before it is queued
  (see :mod:`app.pipeline.quality`): a reply that only parrots the user, or that
  repeats the previous one, is thrown away and regenerated once. Because the
  rejected attempt is never queued, nothing has been spoken and the client is
  told to discard the streaming preview.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from loguru import logger

from ..asr import ASREngine
from ..config import BACKEND_DIR, CharacterConfig, Settings, apply_patch
from ..emotion import parse_emotion
from ..llm import ChatMessage, LLMEngine, LLMError, create_llm
from ..memory import ProfileStore
from ..schemas import Msg, SessionState, TtsEnd, TtsStart
from ..tts import TTSEngine, TTSError, create_tts
from ..utils.audio import DecodedAudio
from .language import (
    BANGLISH,
    LANGUAGE_NAMES,
    detect_language,
    locale_for,
    primary_subtag,
)
from .quality import is_echo, is_repetition
from .sentence_splitter import SentenceSplitter
from .speech import prepare_speech
from .transcript import TranscriptVerdict
from .transcript import evaluate as evaluate_transcript

#: How many finished sentences may wait for the TTS worker. Two is enough to
#: keep the worker busy without racing ahead of the audio the user is hearing.
QUEUE_SIZE = 3

#: Instructions that teach the model our animation protocol. Appended to every
#: character's own system prompt.
ANIMATION_PROMPT = """
You are speaking through a 3D avatar. Your replies are read out loud by a
text-to-speech engine and your emotions drive the avatar's face.

* Write plain, natural spoken language. No markdown, no bullet lists, no emoji.
* You may start a sentence with ONE emotion tag, chosen from:
  [neutral] [happy] [sad] [angry] [surprised] [thinking] [relaxed] [shy]
  Put it at the very beginning of the sentence it applies to.
* You may also use short gesture notes in round brackets, e.g. (laughs),
  (sighs), (nods). They are never spoken.
* Keep replies conversational: one to three short sentences unless asked more.
""".strip()

#: Rules that fix the two complaints every companion LLM gets: it answers by
#: repeating the question, and it drifts into a loop. Kept in their own constant
#: so they can be unit tested and so a character can be given a different set.
CONVERSATION_PROMPT = """
How to hold a conversation:

* Answer the *intent*, never the words. If someone says "I am so tired today",
  a good reply is "Rough night? Want to talk about it, or should I just keep you
  company?" - not "You are so tired today".
* Never begin by repeating, quoting or paraphrasing what the user just said.
  Do not open with "You said", "So you're saying", "It sounds like you", or the
  user's own sentence echoed back.
* Do not reuse a phrasing, joke or sentence you have already used in this
  conversation. If you have already asked a question, ask a different one or
  make a statement instead.
* Ask at most one question per reply, and only when you actually want the
  answer. Statements, reactions and small opinions are better than interviews.
* Have a point of view: share a preference, a small guess or a gentle tease.
  React to the *person* ("that sounds exhausting", "good for you") rather than
  summarising the topic back at them.
* When you do not know something, say so briefly and move the conversation on.
""".strip()

#: System note added to the *second* attempt when the first was rejected by the
#: quality guard. Short and imperative: it is a correction, not a conversation.
_RETRY_NUDGE = (
    "Your previous attempt was rejected: it repeated the user's own words back "
    "at them instead of answering. Reply to the *meaning* of their message and "
    "add something they do not already have - a reaction, an opinion, a guess or "
    "a question. Do not quote or paraphrase their message."
)

#: Prompt for the rolling memory summary (one small call every N turns).
_SUMMARY_PROMPT = (
    "You maintain a short memory of a conversation between a user and their "
    "companion. In at most two sentences, third person, record only durable, "
    "useful context: what they are working on, how they feel about it, and any "
    "preference or plan that is still relevant. No greetings, no pleasantries, "
    "no repetition of the recent turns verbatim. Plain text only."
)


@dataclass(slots=True)
class _Attempt:
    """What one LLM attempt produced. A rejected attempt is never spoken."""

    text: str = ""
    ttft_ms: float | None = None
    sentences: int = 0
    #: The guard rejected the first sentence and the attempt was thrown away.
    aborted: bool = False


class Transport(Protocol):
    """Everything the orchestrator needs from the connection layer."""

    async def send_json(self, message: dict) -> None: ...

    async def send_bytes(self, payload: bytes) -> None: ...

    async def set_state(self, state: SessionState, detail: str = "") -> None: ...


class ConversationOrchestrator:
    """Owns the LLM/TTS engines and the per-session conversation history."""

    def __init__(
        self,
        *,
        settings: Settings,
        character: CharacterConfig,
        transport: Transport,
        asr: ASREngine | None = None,
        session_id: str = "",
    ) -> None:
        self.session_id = session_id
        self.settings = settings
        self.character = character
        self.transport = transport
        self.asr = asr

        self.llm: LLMEngine = create_llm(settings.llm)
        self.tts: TTSEngine = create_tts(settings.tts)

        #: Long-term memory of the person (single-user profile JSON). ``None``
        #: when disabled: every use site already checks for that.
        self.memory: ProfileStore | None = None
        if settings.memory.enabled:
            path = Path(settings.memory.path)
            if not path.is_absolute():
                path = BACKEND_DIR / path
            self.memory = ProfileStore(path)

        self.history: deque[ChatMessage] = deque(maxlen=max(2, settings.llm.max_context_messages))
        self._turn_task: asyncio.Task[None] | None = None
        self._summary_task: asyncio.Task[None] | None = None
        self._speech_counter = 0
        self._turn_counter = 0
        self._greeted = False
        self._closed = False

    # ------------------------------------------------------------------ #
    #  Prompting
    # ------------------------------------------------------------------ #
    @property
    def system_prompt(self) -> str:
        parts = [self.character.system_prompt.strip(), ANIMATION_PROMPT, CONVERSATION_PROMPT]
        if directive := self._language_directive():
            parts.append(directive)
        if self.memory is not None and self.settings.memory.inject:
            if block := self.memory.describe():
                parts.append(block)
        if suffix := self.settings.llm.system_prompt_suffix.strip():
            parts.append(suffix)
        return "\n\n".join(part for part in parts if part)

    def _language_directive(self) -> str:
        """Tell the model which language to answer in, from the user's own text.

        The persona *promises* to mirror the user's language. That promise is
        worthless unless the model is told which language it just read, so the
        script - or the romanisation - of the most recent user message is named
        explicitly.
        """
        base = (self.character.language or "en").strip().lower()
        last_user = next((m.content for m in reversed(self.history) if m.role == "user"), "")
        detected = detect_language(last_user)
        base_code = base.split("-", 1)[0]

        if detected == BANGLISH:
            # Romanised Bengali is the one case where "mirror them" needs a nudge
            # *towards* Bengali script: the voice that reads the reply is a
            # Bengali one, and it can only pronounce Bengali letters.
            return (
                "The user is writing Bengali in Latin letters (Banglish: \"ami bhalo "
                "achi\"). Answer in Bengali - write it in Bengali script (বাংলা), "
                "keeping words that are naturally English in English. Do not answer "
                "in English, and do not switch languages until they do."
            )
        if detected and detected != base_code and detected in LANGUAGE_NAMES:
            name = LANGUAGE_NAMES[detected]
            return (
                f"The user just wrote in {name}. Reply in {name}, mirroring how they "
                "mix in English words - do not switch back to another language unless "
                "they do. Keep the same language for every turn until they change."
            )
        if base_code == "ja":
            return "Reply in Japanese (日本語) using polite, spoken Japanese."
        if base_code == "bn":
            return (
                "Reply in the language the user writes in - Bengali (বাংলা) or English, "
                "matching their mix. Never answer in a third language."
            )
        return "Reply in the language the user writes in, and match it every turn."

    def speech_language(self, user_text: str) -> str:
        """Which language the *reply* to ``user_text`` will be spoken in.

        Taken from the user's own message rather than the reply, for two reasons:
        it is already known when the TTS worker starts (the first sentence is not)
        and it cannot flicker between sentences mid-reply, which is what would
        happen if we re-detected on every fragment. The persona's language is the
        fallback for inputs with no letters at all ("???").
        """
        return detect_language(user_text) or (self.character.language or "en").strip() or "en"

    def _voice_for(self, language: str) -> str:
        """The voice to synthesise a ``language`` sentence with.

        A configured voice - persona or settings - that already speaks the
        language is never overridden; only a *mismatch* is, and only when the
        engine can offer a voice for it (Edge can, the browser picks its own).
        """
        configured = self.character.resolve_voice(self.settings.tts.voice)
        if not self.settings.tts.auto_voice:
            return configured
        if primary_subtag(configured) == primary_subtag(language):
            return configured
        return self.tts.voice_for_language(language) or configured


    def _messages(self, nudge: str = "") -> list[ChatMessage]:
        """System prompt + history, optionally with a corrective system note.

        The note goes *after* the system prompt and before the history so the
        model reads the failed attempt as part of the conversation it must not
        repeat.
        """
        messages = [ChatMessage("system", self.system_prompt)]
        if nudge:
            messages.append(ChatMessage("system", nudge))
        messages.extend(self.history)
        return messages

    def _sync_context(self) -> None:
        """Hand the engine the memory snapshot.

        Real providers ignore it (the block is already in the system prompt);
        the offline brain uses it to remember your name and your likes. A custom
        engine that does not implement the hook must never break a turn.
        """
        setter = getattr(self.llm, "set_context", None)
        if not callable(setter):
            return
        setter(
            {
                "profile": self.memory.snapshot() if self.memory is not None else None,
                "language": (self.character.language or "en"),
            }
        )

    # ------------------------------------------------------------------ #
    #  Long-term memory
    # ------------------------------------------------------------------ #
    def _observe_user(self, text: str) -> None:
        """Learn from one user message. Never allowed to break a turn."""
        if self.memory is None:
            return
        try:
            learned = self.memory.observe(text)
        except Exception as exc:
            logger.debug("memory update skipped: {}", exc)
            return
        if learned:
            logger.info("memory: learned {}", ", ".join(learned))

    def _maybe_summarise(self) -> None:
        """Kick off a rolling-summary refresh every ``memory.summarise_every`` turns."""
        if self.memory is None or self._turn_counter == 0:
            return
        every = self.settings.memory.summarise_every
        if every <= 0 or self._turn_counter % every != 0:
            return
        if self._summary_task is not None and not self._summary_task.done():
            return
        self._summary_task = asyncio.create_task(
            self._refresh_summary(),
            name=f"summary-{self.session_id}",
        )

    async def _refresh_summary(self) -> None:
        """Compress the recent conversation into the profile's ``summary`` field.

        Runs *after* the reply it belongs to is queued, and never raises: a
        summary that fails is a lost nicety, not a broken conversation.
        """
        assert self.memory is not None
        recent = [
            message
            for message in list(self.history)[-16:]
            if not message.content.startswith("(interrupted)")
        ]
        if not recent:
            return

        if self.llm.name == "echo":
            # No model: build something honest from the topics we tracked rather
            # than asking the canned brain to summarise itself.
            topics = self.memory.profile.topics[:6]
            if topics:
                self.memory.set_summary(f"They have been talking about {', '.join(topics)}.")
            return

        transcript = "\n".join(f"{m.role}: {m.content}" for m in recent)
        try:
            parts: list[str] = []
            prompt = [ChatMessage("system", _SUMMARY_PROMPT), ChatMessage("user", transcript)]
            async with contextlib.aclosing(
                self.llm.stream_chat(prompt, temperature=0.2, max_tokens=120)
            ) as stream:
                async for delta in stream:
                    parts.append(delta)
            summary = re.sub(r"[\[<][^\]>]{1,24}[\]>]", "", "".join(parts)).strip()
            if summary:
                self.memory.set_summary(summary)
                logger.debug("memory summary refreshed ({} chars)", len(summary))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("memory summary skipped: {}", exc)

    def _last_assistant_reply(self) -> str | None:
        for message in reversed(self.history):
            if message.role == "assistant":
                return message.content
        return None

    def _recent_lines(self, role: str, limit: int) -> list[str]:
        """The last ``limit`` things ``role`` said, newest first.

        Partial utterances (recorded as ``(interrupted) ...``) are skipped: only
        the tail was ever heard, so comparing a transcript against them would
        both under- and over-trigger the echo guard.
        """
        if limit <= 0:
            return []
        out: list[str] = []
        for message in reversed(self.history):
            if message.role != role:
                continue
            if message.content.startswith("(interrupted)"):
                continue
            out.append(message.content)
            if len(out) >= limit:
                break
        return out

    def _filter_transcript(self, text: str, duration: float | None = None) -> TranscriptVerdict:
        """Run one transcript past the input gate (see ``pipeline/transcript``)."""
        config = self.settings.asr.transcript
        return evaluate_transcript(
            text,
            config,
            duration=duration,
            recent_assistant=self._recent_lines("assistant", config.echo_window),
            recent_user=self._recent_lines("user", 1),
        )

    # ------------------------------------------------------------------ #
    #  Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Send the greeting so the avatar says hello on connect (once)."""
        if self._greeted:
            return
        self._greeted = True

        code = self.character.language
        logger.info(
            "session {} ready - character={} lang={} llm={}/{} tts={}/{}",
            self.session_id, self.character.name, code,
            self.settings.llm.provider, self.settings.llm.model,
            self.settings.tts.engine, self.character.resolve_voice(self.settings.tts.voice),
        )
        if greeting := self.character.greeting.strip():
            await self.submit_text(greeting, internal=True)

    async def update_settings(self, patch: dict) -> Settings:
        """Apply a runtime config patch, rebuilding engines when needed."""
        before = self.settings
        self.settings = apply_patch(before, patch)

        if self.settings.llm != before.llm:
            await self.llm.aclose()
            self.llm = create_llm(self.settings.llm)
            logger.info("llm reconfigured -> {}/{}", self.settings.llm.provider, self.settings.llm.model)
        if self.settings.tts != before.tts:
            await self.tts.aclose()
            self.tts = create_tts(self.settings.tts)
            logger.info("tts reconfigured -> {}/{}", self.settings.tts.engine, self.settings.tts.voice)
        if self.settings.memory != before.memory:
            if self.settings.memory.enabled:
                path = Path(self.settings.memory.path)
                if not path.is_absolute():
                    path = BACKEND_DIR / path
                self.memory = ProfileStore(path)
            else:
                self.memory = None
            logger.info("memory reconfigured -> {}", "on" if self.memory else "off")
        return self.settings

    def set_character(self, character: CharacterConfig, *, keep_history: bool = False) -> None:
        """Swap the persona.

        A *switch* to a different persona starts a fresh conversation (the new one
        was not there for it). A saved *edit* of the persona in use does not - see
        ``keep_history`` - because clearing the chat to change a greeting would be
        a rude surprise.
        """
        self.character = character
        if not keep_history:
            self.history.clear()
        logger.info("character {} -> {} ({})", "edited" if keep_history else "switched", character.name, character.language)

    async def aclose(self) -> None:
        self._closed = True
        await self.interrupt()
        if self._summary_task is not None and not self._summary_task.done():
            self._summary_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._summary_task
        await self.llm.aclose()
        await self.tts.aclose()

    def forget(self) -> None:
        """Wipe the long-term profile (the web UI's "Forget everything")."""
        if self.memory is not None:
            self.memory.clear()
            self._sync_context()

    # ------------------------------------------------------------------ #
    #  Inputs
    # ------------------------------------------------------------------ #
    async def submit_text(self, text: str, *, internal: bool = False, source: str = "typed") -> None:
        """Start a new turn from text (typed, client-side ASR, or greeting).

        ``source="asr"`` marks text that came out of the browser's speech
        recognition, so it goes through the same input gate as audio the server
        transcribes itself. Typed text is taken at face value.
        """
        text = text.strip()
        if not text:
            return

        if not internal and source == "asr":
            verdict = self._filter_transcript(text)
            if not verdict:
                logger.info("ignored client transcript {!r} ({})", text, verdict.reason)
                await self.transport.send_json(Msg.asr("", True, None, reason=verdict.reason))
                await self.transport.set_state("idle")
                return
            text = verdict.text

        if self._turn_task and not self._turn_task.done():
            # A new utterance is the strongest form of barge-in.
            await self.interrupt()

        if not internal:
            await self.transport.send_json(Msg.user_text(text))

        self._turn_counter += 1
        self._turn_task = self._spawn_turn(text, self._turn_counter, internal=internal)

    async def submit_audio(self, audio: DecodedAudio) -> None:
        """Transcribe an utterance then start a turn with the result."""
        if self.asr is None:
            await self.transport.send_json(
                Msg.error(
                    "The backend has no ASR engine configured, so audio cannot be "
                    "transcribed. Either install requirements-local.txt, set asr.engine, "
                    "or use the browser's own speech recognition.",
                    code="asr_disabled",
                )
            )
            return

        await self.interrupt()
        await self.transport.set_state("listening", "transcribing")
        started = time.perf_counter()
        try:
            text = await self.asr.transcribe(audio)
        except Exception as exc:  # ASRError or engine specific
            logger.exception("transcription failed")
            await self.transport.send_json(Msg.error(str(exc), code="asr"))
            await self.transport.set_state("idle")
            return
        took = (time.perf_counter() - started) * 1000
        logger.info("ASR {:.2f}s audio -> {!r} ({:.0f} ms)", audio.duration, text, took)

        if not text:
            await self.transport.send_json(Msg.asr("", True, audio.duration))
            await self.transport.set_state("idle")
            return

        verdict = self._filter_transcript(text, duration=audio.duration)
        if not verdict:
            logger.info(
                "ignored ASR transcript {!r} ({}, score={:.2f})",
                text, verdict.reason, verdict.score,
            )
            await self.transport.send_json(
                Msg.asr("", True, audio.duration, reason=verdict.reason)
            )
            await self.transport.set_state("idle")
            return
        text = verdict.text

        await self.transport.send_json(Msg.asr(text, True, audio.duration))
        await self.transport.send_json(Msg.user_text(text))
        self._turn_counter += 1
        self._turn_task = self._spawn_turn(text, self._turn_counter, asr_ms=took)

    async def interrupt(self, heard: str = "") -> None:
        """Barge-in: drop the current turn and tell the client to flush audio."""
        task = self._turn_task
        was_running = task is not None and not task.done()
        if was_running:
            assert task is not None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._turn_task = None

        if heard.strip():
            # Remember what the user actually heard, so the model can pick up
            # the thread instead of restarting the sentence.
            self.history.append(ChatMessage("assistant", f"(interrupted) {heard.strip()}"))

        await self.transport.send_json(Msg.interrupted())
        await self.transport.set_state("idle")
        if was_running:
            logger.info("turn interrupted by the user")

    async def reset(self) -> None:
        await self.interrupt()
        self.history.clear()
        await self.transport.send_json(Msg.history([]))
        logger.info("conversation history cleared")

    # ------------------------------------------------------------------ #
    #  The turn
    # ------------------------------------------------------------------ #
    def _spawn_turn(self, text: str, index: int, *, internal: bool = False, **kwargs) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._run_turn(text, index, internal=internal, **kwargs),
            name=f"turn-{self.session_id}-{index}",
        )
        task.add_done_callback(self._on_turn_finished)
        return task

    def _on_turn_finished(self, task: asyncio.Task[None]) -> None:
        """Surface crashes instead of letting asyncio log them silently."""
        if task.cancelled():
            return
        if error := task.exception():
            logger.opt(exception=error).error("turn task crashed")

    async def _run_turn(
        self,
        user_text: str,
        turn_index: int,
        *,
        asr_ms: float = 0.0,
        internal: bool = False,
    ) -> None:
        turn_started = time.perf_counter()
        previous_reply = self._last_assistant_reply()
        # A switch made on an earlier turn is reported before this one starts, so
        # the notice never arrives in the middle of someone speaking.
        await self._report_fallback()
        # The greeting is not something the *user* said, so it must never be
        # mined for facts about them.
        if not internal:
            self._observe_user(user_text)
        self.history.append(ChatMessage("user", user_text))
        await self.transport.set_state("thinking")

        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=QUEUE_SIZE)
        # Decided once, here, from the user's message: every sentence of the
        # reply is then spoken by the same voice. A voice that changed halfway
        # through a reply would be worse than one that is merely wrong.
        speech_language = self.speech_language(user_text)
        if primary_subtag(speech_language) != primary_subtag(self.character.language or "en"):
            # Worth a line at INFO: "the avatar answered in the wrong language"
            # and "the avatar sounded wrong" are both this decision, and this is
            # the only place that names it.
            logger.info(
                "turn {}: {} detected in the user's message - speaking with '{}'",
                turn_index,
                LANGUAGE_NAMES.get(detect_language(user_text), detect_language(user_text) or "a language"),
                self._voice_for(speech_language),
            )
        tts_task = asyncio.create_task(
            self._tts_worker(queue, turn_index, speech_language),
            name=f"tts-{self.session_id}-{turn_index}",
        )

        attempt = _Attempt()
        retried = False
        try:
            guard = self._build_guard(user_text, previous_reply)
            attempt = await self._pump(queue, turn_started, guard)
            if attempt.aborted:
                # Nothing was queued, so nothing was spoken: the user has only
                # seen the streaming preview, which we now ask the client to drop.
                await self.transport.send_json(Msg.llm_restart())
                logger.info(
                    "turn {}: rejected the first sentence (echo/repetition) - regenerating",
                    turn_index,
                )
                retried = True
                retry = await self._pump(queue, turn_started, None, nudge=_RETRY_NUDGE)
                retry.ttft_ms = attempt.ttft_ms
                attempt = retry

        except asyncio.CancelledError:
            await self._drain(queue)
            tts_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tts_task
            raise

        except (LLMError, TTSError) as exc:
            logger.warning("turn failed: {}", exc)
            await self.transport.send_json(Msg.error(str(exc), code="llm" if isinstance(exc, LLMError) else "tts"))
        except Exception as exc:
            logger.exception("unexpected failure during turn")
            await self.transport.send_json(Msg.error(f"Unexpected error: {exc}"))

        finally:
            # The worker is torn down either way; make sure it stops even if the
            # LLM raised before producing a single sentence.
            if not tts_task.cancelled():
                with contextlib.suppress(asyncio.CancelledError):
                    await queue.put(None)
                with contextlib.suppress(asyncio.CancelledError):
                    await tts_task

        reply = attempt.text.strip()
        if reply:
            self.history.append(ChatMessage("assistant", reply))
        await self.transport.send_json(Msg.llm_done(reply))
        # If the provider changed *while* replying (the cloud dropped, the local
        # backup answered), say so now - after the words, never over them.
        await self._report_fallback()

        total_ms = (time.perf_counter() - turn_started) * 1000
        await self.transport.send_json(
            Msg.metrics(
                turn=turn_index,
                asr_ms=round(asr_ms, 1),
                ttft_ms=round(attempt.ttft_ms or 0.0, 1),
                total_ms=round(total_ms, 1),
                sentences=attempt.sentences,
                history=len(self.history),
                retried=retried,
            )
        )
        await self.transport.set_state("idle")
        logger.info(
            "turn {} done in {:.0f} ms ({} sentence(s), ttft {:.0f} ms)",
            turn_index, total_ms, attempt.sentences, attempt.ttft_ms or 0.0,
        )
        # Fire-and-forget, *after* the reply is queued: refreshing the summary
        # must never delay the thing the user is listening to.
        self._maybe_summarise()

    async def _report_fallback(self) -> None:
        """Tell the user once when a reply came from a backup provider.

        Sent as a **non-fatal** error frame: the conversation worked, so this is a
        notice, not a failure - but a changed voice with no explanation is worse
        than one sentence saying why, and where to fix it. The engine clears the
        event as it is taken, so it can never repeat.
        """
        take = getattr(self.llm, "take_event", None)
        if not callable(take):
            return
        event = take()
        if event is None:
            return
        logger.warning(
            "LLM fallback: {} -> {} ({})", event.from_label, event.to_label, event.reason
        )
        await self.transport.send_json(Msg.error(event.message(), code="llm_fallback"))

    # ------------------------------------------------------------------ #
    #  Streaming one attempt
    # ------------------------------------------------------------------ #
    async def _pump(
        self,
        queue: asyncio.Queue[str | None],
        turn_started: float,
        guard: Callable[[str], bool] | None,
        *,
        nudge: str = "",
    ) -> _Attempt:
        """Stream one LLM attempt into the TTS queue.

        Returns what happened. With a `guard`, the reply's **first** sentence is
        judged before it is queued; rejecting it aborts the attempt and nothing
        is ever spoken.
        """
        attempt = _Attempt()
        splitter = SentenceSplitter()
        aborted = False

        self._sync_context()
        async with contextlib.aclosing(self.llm.stream_chat(self._messages(nudge))) as stream:
            async for delta in stream:
                if attempt.ttft_ms is None:
                    attempt.ttft_ms = (time.perf_counter() - turn_started) * 1000
                attempt.text += delta
                await self.transport.send_json(Msg.llm_delta(delta))
                for sentence in splitter.push(delta):
                    if not await self._consider(attempt, sentence, guard, queue):
                        aborted = True
                        break
                if aborted:
                    break

            if not aborted:
                # A short reply arrives in one token and is only released here.
                # The guard has to run on this path too, otherwise a one-sentence
                # parroting reply would slip through untouched.
                for sentence in splitter.flush():
                    if not await self._consider(attempt, sentence, guard, queue):
                        aborted = True
                        break

        attempt.aborted = aborted
        return attempt

    async def _consider(
        self,
        attempt: _Attempt,
        sentence: str,
        guard: Callable[[str], bool] | None,
        queue: asyncio.Queue[str | None],
    ) -> bool:
        """Judge (only) the first sentence, then hand it to the TTS queue.

        Returns `False` when the guard rejects it and the attempt must be thrown
        away - which is safe *because* nothing has been queued yet.
        """
        if guard is not None and attempt.sentences == 0 and guard(sentence):
            return False
        attempt.sentences += 1
        await queue.put(sentence)  # blocks when the TTS worker lags
        return True

    def _build_guard(self, user_text: str, previous_reply: str | None) -> Callable[[str], bool] | None:
        """The first-sentence gate, or `None` when the guard is switched off."""
        llm = self.settings.llm
        if not llm.quality_guard:
            return None

        echo_threshold = llm.echo_threshold
        repeat_threshold = llm.repeat_threshold

        def guard(sentence: str) -> bool:
            if is_echo(sentence, user_text, threshold=echo_threshold):
                logger.debug("quality: first sentence parrots the user")
                return True
            if is_repetition(sentence, previous_reply, threshold=repeat_threshold):
                logger.debug("quality: first sentence repeats the previous reply")
                return True
            return False

        return guard

    async def _drain(self, queue: asyncio.Queue[str | None]) -> None:
        """Throw away queued sentences after a cancellation."""
        while not queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()

    # ------------------------------------------------------------------ #
    #  TTS worker (one per turn -> guarantees playback order)
    # ------------------------------------------------------------------ #
    async def _tts_worker(
        self,
        queue: asyncio.Queue[str | None],
        turn_index: int,
        language: str,
    ) -> None:
        index = 0
        # One worker per turn, so a local set is exactly "once per reply".
        played_defaults: set[str] = set()
        while True:
            sentence = await queue.get()
            if sentence is None:
                break

            index += 1
            analysis = parse_emotion(sentence, self.character)
            if not analysis.spoken:
                logger.trace("sentence {!r} had nothing to speak", sentence[:40])
                continue

            await self.transport.send_json(
                Msg.sentence(index, analysis.spoken, analysis.display)
            )

            # A gesture the model wrote into the text is a deliberate beat and
            # always plays; the character's standing emotion action is a mood
            # and only needs to establish itself once per reply.
            actions = list(analysis.explicit_gestures)
            if analysis.default_action and analysis.default_action not in played_defaults:
                played_defaults.add(analysis.default_action)
                actions.append(analysis.default_action)

            expression = None
            if analysis.mapping.preset and analysis.emotion != "neutral":
                expression = {
                    "preset": analysis.mapping.preset,
                    "emotion": analysis.emotion,
                    "weight": analysis.mapping.weight,
                    "hold_ms": analysis.mapping.hold_ms,
                    "override_blink": analysis.mapping.override_blink,
                }
                # React *before* the audio is ready: a face that lags the voice
                # by a TTS round-trip looks broken.
                await self.transport.send_json(Msg.custom("expression", **expression))

            # Gestures are their own channel. They used to be nested under the
            # expression `if`, so a *neutral* sentence carrying `(nods)` sent no
            # action frame at all and the gesture only played when a client
            # re-read `tts.start.actions` - after the audio was ready.
            for action in actions:
                await self.transport.send_json(Msg.custom("action", name=action, intensity=1.0))

            prepared = await self._synthesize(analysis.spoken, expression, actions, language)
            if prepared is None:
                continue

            self._speech_counter += 1
            speech_id = self._speech_counter
            header = TtsStart(
                id=speech_id,
                index=index,
                text=analysis.spoken,
                format=prepared.transport,
                sample_rate=prepared.sample_rate,
                channels=1,
                duration=round(prepared.duration, 3),
                volumes=prepared.volumes,
                visemes=prepared.viseme_payload,
                lipsync_mode=self.settings.lipsync.mode,
                expression=expression,
                actions=actions,
                voice=prepared.voice,
                lang=locale_for(language),
            )

            await self.transport.set_state("speaking", f"sentence {index}")
            await self.transport.send_json(header.model_dump())

            if prepared.transport != "browser":
                sent = 0
                for chunk in prepared.audio_chunks(self.settings.tts.chunk_ms):
                    await self.transport.send_bytes(chunk)
                    sent += len(chunk)
                logger.trace("speech {}: sent {} bytes ({} chunks)", speech_id, sent, sent // 4096 or 1)

            await self.transport.send_json(TtsEnd(id=speech_id).model_dump())
            logger.debug(
                "speech {}: {:.2f}s, transport={}, {} visemes, expression={}",
                speech_id, prepared.duration, prepared.transport,
                len(prepared.visemes), (expression or {}).get("preset"),
            )

    async def _synthesize(
        self,
        text: str,
        expression: dict | None,
        actions: list[str],
        language: str,
    ):
        """Synthesise + analyse one sentence, reporting failures without dying."""
        try:
            started = time.perf_counter()
            result = await self.tts.synthesize(
                text,
                voice=self._voice_for(language),
                rate=self.character.tts_rate or self.settings.tts.rate,
            )
            prepared = await prepare_speech(
                result,
                text,
                tts=self.settings.tts,
                lipsync=self.settings.lipsync,
                language=language,
                gain=1.0,
            )
            logger.debug(
                "tts '{}' -> {:.2f}s audio in {:.0f} ms",
                text[:32], prepared.duration, (time.perf_counter() - started) * 1000,
            )
            return prepared
        except TTSError as exc:
            logger.error("TTS failed: {}", exc)
            # Fall back to client side speech so the conversation keeps going.
            if self.settings.tts.engine != "browser":
                logger.info("falling back to browser speech for this sentence")
                with contextlib.suppress(Exception):
                    from ..tts.browser import BrowserTTS

                    fallback = BrowserTTS(self.settings.tts)
                    result = await fallback.synthesize(text)
                    return await prepare_speech(
                        result,
                        text,
                        tts=self.settings.tts,
                        lipsync=self.settings.lipsync,
                        language=language,
                    )
            await self.transport.send_json(Msg.error(str(exc), code="tts"))
            return None


__all__ = ["ANIMATION_PROMPT", "QUEUE_SIZE", "ConversationOrchestrator", "Transport"]
