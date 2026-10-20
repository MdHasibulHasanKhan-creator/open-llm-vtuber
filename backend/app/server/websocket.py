"""The ``/ws`` endpoint: one WebSocket per browser tab.

Frame handling
--------------
``await websocket.receive()`` returns raw frames, which lets us mix JSON text
frames and PCM binary frames on the same socket (see ``docs/PROTOCOL.md``).
Binary frames are appended to the session's utterance buffer; everything else
is dispatched by :class:`~app.session.ClientSession`.

Connection handling
-------------------
Three checks happen before a session is created, in this order:

1. **Origin** (:mod:`app.server.origins`). A rejected origin is *accepted* and
   then closed with 1008 plus a readable ``error`` frame - a browser cannot read
   anything from a handshake that was refused outright, and uvicorn reports that
   as the opaque ``403 Forbidden`` people keep filing bugs about.
2. **Capacity** (``server.max_sessions``) -> 1013, "try again later".
3. Anything else is a normal session.

``/ws`` is the canonical path; ``/`` is accepted as an alias because pointing a
client at the bare origin is an easy mistake to make, and a catch-all route
answers every other path with a 1008 that names the correct URL instead of a
403.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from ..schemas import Msg
from ..session import ClientSession
from .origins import WS_POLICY_VIOLATION, effective_origins, is_origin_allowed

#: session id -> live session (also used by /api/health).
sessions: dict[str, ClientSession] = {}

KNOWN_WS_PATHS = ("/ws", "/")


async def websocket_endpoint(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings

    # ---------------------------------------------------------------- origin
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    allowed_origins = effective_origins(settings)
    permitted, reason = is_origin_allowed(origin, host, allowed_origins)

    if not permitted:
        logger.warning(
            "refusing WebSocket from origin {!r} (host={!r}): {}. Add it to "
            "VTUBER_SERVER__WS_ALLOWED_ORIGINS, or use [\"*\"] to disable the check.",
            origin,
            host,
            reason,
        )
        # Accept first so the client gets a body it can actually read, then close.
        await websocket.accept()
        await websocket.send_text(
            _json(
                Msg.error(
                    f"Origin {origin!r} is not allowed to open a WebSocket. "
                    f"Allowed: {', '.join(allowed_origins) or '(same-origin only)'}.",
                    fatal=True,
                    code="origin",
                )
            )
        )
        await websocket.close(code=WS_POLICY_VIOLATION)
        return

    await websocket.accept()

    # -------------------------------------------------------------- capacity
    if len(sessions) >= settings.server.max_sessions:
        logger.warning("refusing connection: {} sessions already open", len(sessions))
        await websocket.send_text(
            _json(Msg.error("The server is at capacity - close another tab.", fatal=True, code="busy"))
        )
        await websocket.close(code=1013)  # try again later
        return

    # Each session gets its own copy so a runtime patch never leaks between tabs.
    session = ClientSession(websocket, settings.model_copy(deep=True))
    sessions[session.id] = session
    logger.info(
        "client connected: {} (path={}, origin={}, {} open)",
        session.id,
        websocket.url.path,
        origin or "-",
        len(sessions),
    )
    logger.debug("origin check passed: {}", reason)

    try:
        await session.send_ready()

        while True:
            frame = await websocket.receive()
            frame_type = frame.get("type")

            if frame_type == "websocket.disconnect":
                logger.debug("session {} disconnected", session.id)
                break

            text = frame.get("text")
            if text is not None:
                await session.handle_text(text)
                continue

            data = frame.get("bytes")
            if data is not None:
                await session.handle_binary(data)

    except WebSocketDisconnect:
        logger.info("session {} closed by the client", session.id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("session {} crashed", session.id)
        with contextlib.suppress(Exception):
            await session.send_json(Msg.error("Internal server error", fatal=True))
    finally:
        sessions.pop(session.id, None)
        with contextlib.suppress(Exception):
            await session.close()
        logger.info("client disconnected: {} ({} open)", session.id, len(sessions))


async def websocket_unknown_path(websocket: WebSocket) -> None:
    """Answer a WebSocket handshake on an unknown path with a usable error.

    Without this the request falls through to the static-file mount, which
    cannot speak WebSocket: uvicorn then logs ``"WebSocket /foo" 403 /
    connection rejected`` and the client sees nothing but a failed handshake.
    """
    path = websocket.url.path
    logger.warning(
        "WebSocket handshake on unknown path {!r} - the endpoint is {}", path, KNOWN_WS_PATHS[0]
    )
    # Same reasoning as above: accept, explain, close.
    await websocket.accept()
    await websocket.send_text(
        _json(
            Msg.error(
                f"Unknown WebSocket path {path!r}. Connect to {KNOWN_WS_PATHS[0]} "
                f"(or {KNOWN_WS_PATHS[1]} for the server root).",
                fatal=True,
                code="path",
            )
        )
    )
    await websocket.close(code=WS_POLICY_VIOLATION)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def close_all_sessions() -> None:
    """Called on server shutdown."""
    for session in list(sessions.values()):
        with contextlib.suppress(Exception):
            await session.close()
    sessions.clear()


__all__ = ["KNOWN_WS_PATHS", "close_all_sessions", "sessions", "websocket_endpoint", "websocket_unknown_path"]
