"""Startup checks: fail loudly, run anyway.

A voice assistant has four independent back ends (LLM, TTS, ASR, and the audio
decoder). Rather than let the user discover a typo through a cryptic error three
messages into a conversation, we probe each one at boot and log an actionable
summary. Nothing is fatal: the server always starts, and the UI shows the
warnings from ``GET /api/health``.

Every warning ends with the two things a person actually wants to know: how to
install what is missing, and how to switch to something that works without it.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from ..asr.factory import create_asr
from ..config import Settings
from ..llm.discover import adopt_if_available
from ..llm.factory import create_llm
from ..tts.factory import create_tts
from ..tts.voices import voice_for_language

#: Shown whenever the LLM cannot be reached - the single most common first-run
#: problem. It names the two fixes that actually produce replies; the offline
#: demo brain is deliberately *not* advertised here, because replying with canned
#: text is the exact failure this app used to have.
_LLM_HINT = (
    "Open Settings -> Model and either paste a free Groq API key (fast, and good at "
    "Bengali) or press \"Detect models\" to adopt a local server. For Ollama that is "
    "`ollama serve` in one terminal and `ollama pull llama3.2:3b` in another."
)
_ASR_HINT = (
    "Install it with `pip install -r requirements-local.txt`, or set "
    "VTUBER_ASR__ENGINE=browser to transcribe with the Web Speech API instead."
)


def _with_hint(detail: str, hint: str) -> str:
    """Append a fix to a failure without the two sentences colliding.

    Engine health messages are written as bare fragments ("faster-whisper is not
    installed", "unreachable (ConnectError)") because they are also used
    elsewhere; the hint is a full sentence. Joining them naively reads as
    "installed Install it with ...".
    """
    detail = detail.strip()
    if not detail:
        return hint
    if not detail.endswith((".", "!", "?", ":")):
        detail += "."
    return f"{detail} {hint}"


async def _verify_adoption(settings: Settings) -> tuple[bool, str]:
    """Re-probe the LLM after we repointed it at a discovered server."""
    llm = create_llm(settings.llm)
    try:
        return await llm.health_check()
    except Exception as exc:
        return False, str(exc)
    finally:
        await llm.aclose()


async def run_preflight(settings: Settings) -> dict[str, Any]:
    """Probe every engine. Returns a summary dict for ``/api/health``."""
    logger.info("running startup checks ...")
    report: dict[str, Any] = {}

    # ---------------------------------------------------------------- LLM
    llm = create_llm(settings.llm)
    try:
        ok, detail = await llm.health_check()
        if not ok:
            detail = _with_hint(detail, _LLM_HINT)
        report["llm"] = {"ok": ok, "detail": detail}
        # Surface the backup chain: "the cloud is down but your local model is
        # answering" is a very different situation from "nothing works", and the
        # UI can only show the difference if it is told.
        backups = [
            f"{link.provider}/{link.model}"
            for link in settings.llm.fallback_chain
            if link.provider != "echo"
        ]
        if backups:
            report["llm"]["fallbacks"] = backups
        logger.log("INFO" if ok else "WARNING", "LLM  {:<10} {}", settings.llm.provider, detail)
    except Exception as exc:
        report["llm"] = {"ok": False, "detail": _with_hint(str(exc), _LLM_HINT)}
        logger.warning("LLM  check failed: {}", exc)
    finally:
        await llm.aclose()

    # Before falling back to *anything*, look for a real model on this machine.
    # Order matters: adopting a working local server is strictly better than
    # switching to a script, and it is what turns "the app replies with canned
    # text" into "the app found my model".
    if not report["llm"]["ok"] and settings.llm.auto_discover:
        found = await adopt_if_available(settings)
        if found:
            if found["id"] == "ollama":
                settings.llm.provider = "ollama"
                settings.llm.base_url = "http://localhost:11434"
            else:
                settings.llm.provider = "openai"
                settings.llm.base_url = found["base_url"]
            settings.llm.model = found["picked_model"]
            settings.llm.api_key = found["api_key"]
            ok, detail = await _verify_adoption(settings)
            if ok:
                report["llm"] = {
                    "ok": True,
                    "detail": (
                        f"auto-detected {found['label']} at {found['base_url']} - "
                        f"using model '{found['picked_model']}' ({detail})"
                    ),
                    "auto_discovered": True,
                    "provider": settings.llm.provider,
                    "model": settings.llm.model,
                    "base_url": settings.llm.base_url,
                }
                logger.success(
                    "LLM auto-detected: {} -> {} ({})",
                    found["label"], found["picked_model"], found["base_url"],
                )
            else:
                logger.warning(
                    "found {} but could not use it ({}) - continuing", found["label"], detail
                )

    if not report["llm"]["ok"] and settings.llm.fallback_to_echo:
        # Opt-in: keep the conversation working instead of erroring on every turn.
        settings.llm.provider = "echo"
        report["llm"] = {
            "ok": True,
            "detail": "configured provider unreachable -> using the offline 'echo' brain "
            "(VTUBER_LLM__FALLBACK_TO_ECHO=false to disable)",
            "fallback": True,
            "canned": True,
        }
        logger.warning(
            "LLM unreachable -> falling back to the offline 'echo' brain. "
            "Set VTUBER_LLM__FALLBACK_TO_ECHO=false to turn this off."
        )
    elif not report["llm"]["ok"]:
        # The honest default: the server runs, but the UI must say, loudly, that
        # replies will not be answers until a model is connected.
        report["llm"]["canned"] = False
        report["llm"]["action_required"] = True
        logger.warning(
            "LLM is not reachable and no local model was found - open Settings -> Model to "
            "connect one (a free Groq key, or `ollama serve` + `ollama pull llama3.2:3b`)."
        )
    if settings.llm.provider == "echo":
        report["llm"]["canned"] = True

    # ---------------------------------------------------------------- TTS
    tts = create_tts(settings.tts)
    try:
        ok, detail = await tts.health_check()
        report["tts"] = {"ok": ok, "detail": detail}
        logger.log("INFO" if ok else "WARNING", "TTS  {:<10} {}", settings.tts.engine, detail)
    except Exception as exc:
        report["tts"] = {"ok": False, "detail": str(exc)}
        logger.warning("TTS  check failed: {}", exc)
    finally:
        await tts.aclose()

    # The voice is not always the configured one: with `auto_voice` the reply's
    # language picks it. Worth one line at boot, because "why is she speaking
    # Bengali?" has a better answer than "the settings lie".
    if settings.tts.engine == "edge" and settings.tts.auto_voice:
        report["tts"]["auto_voice"] = True
        logger.info(
            "TTS  auto voice follows the reply's language (bn -> {}) - "
            "VTUBER_TTS__AUTO_VOICE=false to pin {}",
            voice_for_language("bn"),
            settings.tts.voice,
        )

    # ---------------------------------------------------------------- ASR
    asr = create_asr(settings.asr)
    if asr is None:
        report["asr"] = {"ok": True, "detail": "client side (browser speech recognition)"}
        logger.info("ASR  browser    the browser transcribes and sends text")
    else:
        try:
            ok, detail = await asr.health_check()
            if not ok:
                detail = _with_hint(detail, _ASR_HINT)
            report["asr"] = {"ok": ok, "detail": detail}
            logger.log("INFO" if ok else "WARNING", "ASR  {:<10} {}", settings.asr.engine, detail)
        except Exception as exc:
            report["asr"] = {"ok": False, "detail": _with_hint(str(exc), _ASR_HINT)}
            logger.warning("ASR  check failed: {}", exc)
        finally:
            await asr.aclose()

    # ------------------------------------------------------- audio decoder
    try:
        import miniaudio  # noqa: F401

        from ..utils.audio import decode_audio  # noqa: F401

        report["audio"] = {"ok": True, "detail": "miniaudio decoder available"}
    except ImportError:
        try:
            import soundfile  # noqa: F401

            report["audio"] = {"ok": True, "detail": "soundfile decoder available"}
        except ImportError:
            report["audio"] = {
                "ok": False,
                "detail": (
                    "no decoder - set tts.format=pcm_s16le, or install miniaudio "
                    "(`pip install miniaudio`) if you need the mp3 transport"
                ),
            }
            logger.warning(
                "no mp3 decoder found (miniaudio / soundfile). Keep tts.format=pcm_s16le "
                "or install one of them."
            )

    problems = [name for name, value in report.items() if not value["ok"]]
    if problems:
        logger.warning(
            "startup checks with warnings: {} - the server still runs; see the hints above "
            "or GET /api/health",
            ", ".join(problems),
        )
    else:
        logger.success("all startup checks passed")
    return report


__all__ = ["run_preflight"]
