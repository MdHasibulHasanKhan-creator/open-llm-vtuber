"""REST endpoints.

The WebSocket carries the conversation; these routes exist so the UI can be
built and configured without one:

    GET  /api/health          liveness + which engines are active
    GET  /api/config          current (secrets masked) configuration
    POST /api/config/validate dry-run a config patch - never raises
    GET  /api/characters      available personas
    GET  /api/character       one persona, as edited, next to what it shipped as
    PUT  /api/character       save an edit to a persona (writes a file)
    DELETE /api/character      throw the edits away - back to the shipped .yaml
    GET  /api/voices          voices for a TTS engine
    GET  /api/vrm-models      .vrm files shipped in frontend/public/models
    GET  /api/llm/status      which provider is active, and where its key lives
    PUT  /api/llm/key         store a key - write-only, returns a fingerprint
    DELETE /api/llm/key       forget the stored key
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from loguru import logger

from .. import __version__
from ..asr.factory import available_engines as asr_engines
from ..config import (
    BACKEND_DIR,
    Settings,
    apply_patch,
    character_detail,
    character_exists,
    clear_character_override,
    list_characters,
    save_character_override,
)
from ..llm import ChatMessage, LLMError, create_llm
from ..llm.discover import discover as discover_endpoints
from ..llm.discover import free_presets, list_models
from ..llm.factory import available_providers as llm_providers
from ..memory import ProfileStore
from ..secrets_store import SECRETS_PATH, delete_secret, set_secret
from ..security import mask_secret
from ..tts.factory import available_engines as tts_engines
from ..tts.factory import create_tts
from .origins import effective_origins, is_origin_allowed

router = APIRouter(prefix="/api", tags=["api"])

#: Voice lists are expensive (edge-tts fetches ~1000 entries) -> cache per engine.
_voice_cache: dict[str, list[dict[str, str]]] = {}


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _memory_path(settings: Settings) -> Path:
    path = Path(settings.memory.path)
    return path if path.is_absolute() else BACKEND_DIR / path


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    from .websocket import sessions

    return {
        "status": "ok",
        "version": __version__,
        "uptime_s": round(time.time() - request.app.state.started_at, 1),
        "engines": {
            "llm": _settings(request).llm.provider,
            "tts": _settings(request).tts.engine,
            "asr": _settings(request).asr.engine,
            "model": _settings(request).llm.model,
        },
        "available": {
            "llm": llm_providers(),
            "tts": tts_engines(),
            "asr": asr_engines(),
        },
        "sessions": len(sessions),
        "preflight": getattr(request.app.state, "preflight", {}),
    }


@router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    return {"config": _settings(request).public_dict(), "characters": list_characters()}


@router.post("/config/validate")
async def validate_config(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Check a patch built by the settings UI before sending it for real."""
    require_same_origin(request)
    try:
        candidate = apply_patch(_settings(request), payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "config": candidate.public_dict()}


@router.get("/characters")
async def characters() -> dict[str, Any]:
    return {"characters": list_characters()}


# --------------------------------------------------------------------------- #
#  Persona editing ("make her yours")
# --------------------------------------------------------------------------- #
def _require_known_character(name: str) -> str:
    """Reject an unknown id before it can become a filename."""
    if not character_exists(name):
        raise HTTPException(status_code=404, detail=f"unknown persona '{name}'")
    return name


@router.get("/character")
async def get_character(
    request: Request,
    id: str = Query(default="default", description="persona id, e.g. 'default'"),
) -> dict[str, Any]:
    """The persona as it reads now, next to the values it shipped with."""
    require_same_origin(request)
    return character_detail(_require_known_character(id))


@router.put("/character")
async def put_character(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Save an edit to a persona. Returns the persona as it now reads.

    An empty string *clears* a field back to the shipped (or built-in) value;
    an unknown field name is a 422 rather than a silent no-op, because a typo in
    the UI should be loud. Same-origin only: this writes a file.
    """
    require_same_origin(request)
    name = _require_known_character(str(payload.get("id") or "default"))
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        raise HTTPException(status_code=422, detail="fields must be an object")
    try:
        save_character_override(name, fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Every open session re-reads the file on its next `character.reload`; the
    # settings UI sends one right after this returns.
    return character_detail(name)


@router.delete("/character")
async def delete_character(
    request: Request,
    id: str = Query(default="default", description="persona id, e.g. 'default'"),
) -> dict[str, Any]:
    """Throw away every saved edit: the persona goes back to its ``.yaml``."""
    require_same_origin(request)
    name = _require_known_character(id)
    clear_character_override(name)
    return character_detail(name)


# --------------------------------------------------------------------------- #
#  Model setup ("why are the replies a script?")
# --------------------------------------------------------------------------- #
@router.get("/llm/presets")
async def llm_presets() -> dict[str, Any]:
    """Ready-made provider settings, plus what is currently active.

    The point of this route is the first-run experience: instead of a paragraph
    of documentation about ``VTUBER_LLM__BASE_URL``, the UI gets a list of things
    to click. Local and free options come first.
    """
    return {"presets": free_presets()}


@router.post("/llm/discover")
async def llm_discover(request: Request) -> dict[str, Any]:
    """Probe the well-known local ports for a running model server."""
    # Same-origin only: this walks a fixed list of localhost ports, which makes
    # it a (slow) port scanner if a random page can call it.
    require_same_origin(request)
    settings = _settings(request)
    # Never let our own port masquerade as a model server (vLLM defaults to 8000,
    # which is also this app's default).
    own = {
        settings.llm.base_url,
        f"http://localhost:{settings.server.port}/v1",
        f"http://127.0.0.1:{settings.server.port}/v1",
    }
    endpoints = await discover_endpoints(exclude=own)
    return {
        "endpoints": endpoints,
        "configured": {
            "provider": settings.llm.provider,
            "base_url": settings.llm.base_url,
            "model": settings.llm.model,
        },
    }


@router.post("/llm/test")
async def llm_test(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Round-trip a real completion through the *proposed* settings.

    Never raises for a bad connection: the whole point is to report "this does
    not work, here is why" to someone clicking a button, rather than to have them
    discover it three messages into a conversation.
    """
    require_same_origin(request)
    settings = _settings(request)
    patch = (payload or {}).get("llm") if isinstance(payload, dict) else None
    try:
        candidate = apply_patch(settings, {"llm": patch} if isinstance(patch, dict) else {})
    except ValueError as exc:
        # A bad provider name is a *result* of this endpoint, not an exception:
        # the UI just wants to render "that configuration is wrong".
        return {"ok": False, "error": str(exc), "reply": "", "latency_ms": 0.0, "models": []}

    # The whole point is to answer "does *this* setting work". With the fallback
    # chain left on, a broken cloud key would be silently rescued by the local
    # backup and the button would report success for a configuration that is
    # broken in every real turn.
    candidate.llm.fallback_chain = []

    started = time.perf_counter()
    reply = ""
    error = ""
    llm = create_llm(candidate.llm)
    try:
        probe = [
            ChatMessage("system", "You are a connectivity test. Answer with one short word."),
            ChatMessage("user", "Reply with exactly: OK"),
        ]
        async with contextlib.aclosing(
            llm.stream_chat(probe, temperature=0.0, max_tokens=16)
        ) as stream:
            async for delta in stream:
                reply += delta
                if len(reply) >= 60:
                    break
    except LLMError as exc:
        error = str(exc)
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
    finally:
        await llm.aclose()

    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    # Probe the model list with the same key the turn would use, so a stored key
    # is respected here too (and a placeholder never gets sent).
    models = await list_models(candidate.llm.base_url, api_key=candidate.llm.effective_api_key())
    return {
        "ok": not error,
        "error": error,
        "error_kind": _classify_llm_error(error) if error else "",
        "reply": reply.strip()[:200],
        "latency_ms": latency_ms,
        "provider": candidate.llm.provider,
        "base_url": candidate.llm.base_url,
        "model": candidate.llm.model,
        "models": models[:80],
    }


def _classify_llm_error(message: str) -> str:
    """Turn an error string into a small machine-readable kind.

    The UI uses this to pick an icon and a suggested fix; the human-readable
    sentence is still ``error``. Kept as string matching because that is exactly
    how the message is produced - see ``OpenAICompatibleLLM._friendly_error``.
    """
    text = (message or "").lower()
    if "(401)" in text:
        return "auth"
    if "(403)" in text:
        return "forbidden"
    if "(404)" in text:
        return "not_found"
    if "(429)" in text:
        return "rate_limit"
    if "cannot reach" in text or "connecterror" in text:
        return "offline"
    if "did not answer within" in text or "timeout" in text:
        return "timeout"
    if "server error" in text:
        return "provider_error"
    return "unknown"


# --------------------------------------------------------------------------- #
#  API key (write-only)
# --------------------------------------------------------------------------- #
def require_same_origin(request: Request) -> None:
    """Reject a settings write that came from another site.

    CORS already stops a cross-origin ``fetch`` from *reading* a reply, and the
    preflight stops it from sending a JSON body - but a request with no body, or
    one sent as ``text/plain``, is a "simple request" that the browser sends
    anyway. Checking ``Origin`` here is what closes that, and it reuses the exact
    policy the WebSocket already enforces (:mod:`app.server.origins`) instead of
    inventing a second one.

    A *missing* ``Origin`` is allowed on purpose: a CLI, an uptime probe and the
    test suite send none, and a browser cannot be tricked into omitting it.
    """
    settings = _settings(request)
    allowed, reason = is_origin_allowed(
        request.headers.get("origin"),
        request.headers.get("host"),
        effective_origins(settings),
    )
    if not allowed:
        logger.warning("rejected cross-origin settings write: {}", reason)
        raise HTTPException(status_code=403, detail=f"cross-origin request rejected ({reason})")


@router.put("/llm/key")
async def llm_set_key(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Store an API key. **Write-only**: the value never comes back out.

    Returns the fingerprint of what was stored (``gsk_1f2e****9c3a``) so the UI
    can show "saved" without ever holding the key. Sending an empty string
    clears it.
    """
    require_same_origin(request)
    raw = (payload or {}).get("api_key") if isinstance(payload, dict) else None
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(status_code=422, detail="api_key must be a string")

    value = (raw or "").strip()
    # A client that echoes back the masked value is saying "unchanged", not
    # "store this fingerprint as my key".
    if "****" in value:
        return {
            "ok": True,
            "changed": False,
            "api_key": mask_secret(_stored_key()),
            "api_key_set": bool(_stored_key()),
        }

    try:
        set_secret("llm_api_key", value)
    except ValueError as exc:  # pragma: no cover - the name is a constant
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    fingerprint = mask_secret(value)
    logger.info("LLM API key {} via the settings API", "cleared" if not value else "updated")
    return {"ok": True, "changed": True, "api_key": fingerprint, "api_key_set": bool(value)}


@router.delete("/llm/key")
async def llm_clear_key(request: Request) -> dict[str, Any]:
    """Forget the stored key (the provider then sees an unauthenticated request)."""
    require_same_origin(request)
    delete_secret("llm_api_key")
    return {"ok": True, "api_key": "", "api_key_set": False}


def _stored_key() -> str:
    from ..secrets_store import get_llm_api_key

    return get_llm_api_key()


@router.get("/llm/status")
async def llm_status(request: Request) -> dict[str, Any]:
    """Where the key lives and whether this machine currently has one.

    Deliberately reports *location and presence*, never the value - this route is
    what the "key saved" badge in the UI reads.
    """
    settings = _settings(request)
    fingerprint = mask_secret(settings.llm.effective_api_key())
    chain = [link for link in settings.llm.fallback_chain if link.provider != "echo"]
    return {
        "provider": settings.llm.provider,
        "model": settings.llm.model,
        "base_url": settings.llm.base_url,
        "api_key": fingerprint,
        "api_key_set": bool(fingerprint),
        "key_source": _key_source(settings.llm.api_key),
        "secrets_file": SECRETS_PATH.name,
        "requires_key": not _is_local(settings.llm.base_url),
        # What happens if the provider above does not answer. Reported so the UI
        # can say "answers with your local model" instead of leaving the user to
        # wonder what a fallback chain is and whether they have one.
        "fallback_chain": [
            {"provider": link.provider, "model": link.model, "base_url": link.base_url}
            for link in chain
        ],
        "fallback_enabled": bool(chain),
        "fallback_cooldown": settings.llm.fallback_cooldown,
    }


def _key_source(configured: str) -> str:
    from ..security import is_placeholder

    if not is_placeholder(configured):
        return "env"
    return "secrets.json" if _stored_key() else "none"


def _is_local(base_url: str) -> bool:
    host = (base_url or "").lower()
    return any(marker in host for marker in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))


# --------------------------------------------------------------------------- #
#  Long-term memory
# --------------------------------------------------------------------------- #
@router.get("/memory")
async def get_memory(request: Request) -> dict[str, Any]:
    """What the companion remembers. Reads the file directly, not a session."""
    # Same-origin: this is the user's private profile. CORS already stops another
    # site from *reading* the reply, and this stops the request being served at
    # all - one rule for every route that touches personal data.
    require_same_origin(request)
    settings = _settings(request)
    if not settings.memory.enabled:
        return {"enabled": False, "path": "", "memory": {}}
    store = ProfileStore(_memory_path(settings))
    return {
        "enabled": True,
        "path": str(store.path),
        "memory": store.as_dict(),
    }


@router.delete("/memory")
async def delete_memory(request: Request) -> dict[str, Any]:
    """Forget everything. Sessions pick the change up on their next turn."""
    # Destructive and irreversible: never on a cross-site request. This is the
    # one route where a CSRF would be a data *loss*, not a data leak.
    require_same_origin(request)
    settings = _settings(request)
    if not settings.memory.enabled:
        return {"ok": False, "detail": "memory is disabled"}
    store = ProfileStore(_memory_path(settings))
    store.clear()
    return {"ok": True, "memory": store.as_dict()}


@router.get("/voices")
async def voices(
    request: Request,
    engine: str | None = Query(default=None, description="edge | openai | browser"),
    locale: str | None = Query(default=None, description="filter, e.g. 'ja' or 'en-US'"),
    limit: int = Query(default=400, ge=1, le=2000),
) -> dict[str, Any]:
    settings = _settings(request)
    engine_name = (engine or settings.tts.engine).lower()
    if engine_name not in tts_engines():
        raise HTTPException(status_code=400, detail=f"unknown TTS engine '{engine_name}'")

    if engine_name not in _voice_cache:
        config = settings.tts.model_copy(deep=True)
        config.engine = engine_name  # type: ignore[assignment]
        tts = create_tts(config)
        try:
            _voice_cache[engine_name] = await tts.list_voices()
        except Exception as exc:
            logger.warning("could not list {} voices: {}", engine_name, exc)
            _voice_cache[engine_name] = []
        finally:
            await tts.aclose()

    found = _voice_cache[engine_name]
    if locale:
        needle = locale.lower()
        found = [v for v in found if needle in v.get("locale", "").lower()]
    return {"engine": engine_name, "count": len(found), "voices": found[:limit]}


@router.get("/vrm-models")
async def vrm_models() -> dict[str, Any]:
    """Discover locally dropped avatars (``frontend/public/models/*.vrm``)."""
    roots = [
        BACKEND_DIR.parent / "frontend" / "public" / "models",
        BACKEND_DIR.parent / "frontend" / "dist" / "models",
    ]
    found: list[dict[str, Any]] = []
    for root in roots:
        if not root.exists():
            continue
        for file in sorted(root.glob("*.vrm")):
            found.append(
                {
                    "name": file.stem,
                    "url": f"/models/{file.name}",
                    "size_mb": round(file.stat().st_size / 1_048_576, 1),
                    "source": str(root),
                }
            )
    return {"models": found}


@router.get("/vrm-models/{name}/info")
async def vrm_model_info(name: str) -> dict[str, Any]:
    """Basic VRM header inspection (version + title) without a 3D engine.

    Useful to sanity check an uploaded model. We only parse the JSON chunk of
    the GLB container, so this is cheap even for 40 MB avatars.
    """
    path: Path | None = None
    for root in (
        BACKEND_DIR.parent / "frontend" / "public" / "models",
        BACKEND_DIR.parent / "frontend" / "dist" / "models",
    ):
        candidate = root / f"{name}.vrm" if not name.endswith(".vrm") else root / name
        if candidate.exists():
            path = candidate
            break
    if path is None:
        raise HTTPException(status_code=404, detail="model not found")

    import json
    import struct

    with path.open("rb") as handle:
        magic, _version, _length = struct.unpack("<III", handle.read(12))
        if magic != 0x46546C67:  # 'glTF'
            raise HTTPException(status_code=400, detail="not a GLB/VRM container")
        chunk_length, chunk_type = struct.unpack("<II", handle.read(8))
        if chunk_type != 0x4E4F534A:  # 'JSON'
            raise HTTPException(status_code=400, detail="missing JSON chunk")
        gltf = json.loads(handle.read(chunk_length).decode("utf-8"))

    extensions = gltf.get("extensions", {})
    vrm1 = extensions.get("VRMC_vrm") or {}
    vrm0 = extensions.get("VRM") or {}

    # VRM 1.0: expressions.preset.*  |  VRM 0.x: blendShapeMaster.blendShapeGroups[].presetName
    presets: set[str] = set((vrm1.get("expressions", {}) or {}).get("preset", {}).keys())
    for group in (vrm0.get("blendShapeMaster", {}) or {}).get("blendShapeGroups", []) or []:
        if group.get("presetName"):
            presets.add(str(group["presetName"]))

    return {
        "name": path.stem,
        "size_mb": round(path.stat().st_size / 1_048_576, 1),
        "meta_version": "1" if vrm1 else ("0" if vrm0 else "unknown"),
        "title": (vrm1.get("meta", {}) or {}).get("name") or vrm0.get("title", ""),
        "preset_expressions": sorted(presets),
    }
