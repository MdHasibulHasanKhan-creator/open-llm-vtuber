"""FastAPI application factory.

Responsibilities:

* CORS for the Vite dev server (``npm run dev``)
* REST routes under ``/api``
* the ``/ws`` conversation socket
* optionally serving the built frontend so the whole app runs on one port
* a startup preflight that reports which engines are alive
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from .. import __version__
from ..config import BACKEND_DIR, LOCAL_CORS_ORIGINS, Settings, get_settings
from .http_routes import health as health_endpoint
from .http_routes import router as api_router
from .origins import effective_origins
from .preflight import run_preflight
from .static import SpaStaticFiles
from .websocket import close_all_sessions, websocket_endpoint, websocket_unknown_path

DESCRIPTION = """
Real-time AI VTuber backend: microphone -> speech recognition -> LLM -> neural
TTS, streamed back over one WebSocket together with a **viseme timeline** and an
RMS envelope so a Three-VRM avatar can lip-sync at 60 FPS.
"""


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Answer a bad REST payload with the same shape as every other error.

    FastAPI's default is ``{"detail": [...]}``; the WebSocket protocol uses
    ``{"type": "error", "error": ..., "code": ...}``. Returning one vocabulary
    everywhere means the client needs exactly one error path.
    """
    logger.warning("invalid request on {} {}: {}", request.method, request.url.path, exc.errors()[:2])
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            {
                "error": "Invalid request body or parameters.",
                "code": "protocol",
                "detail": exc.errors()[:3],
            }
        ),
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Never let a bug turn into a silent connection reset.

    Starlette's default re-raises and the client sees a bare 500 with no body;
    worse, uvicorn logs a traceback the user cannot act on. We log the traceback
    server-side and hand back a readable, card-shaped error instead.
    """
    logger.opt(exception=exc).error("unhandled error on {} {}", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": "The server hit an unexpected error. Please try again.",
            "code": "internal",
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.started_at = time.time()
        app.state.preflight = await run_preflight(settings)
        logger.info(
            "ready -> http://{}:{}/  (WebSocket: ws://{}:{}/ws)",
            settings.server.host, settings.server.port,
            settings.server.host, settings.server.port,
        )
        try:
            yield
        finally:
            await close_all_sessions()
            logger.info("shutdown complete")

    app = FastAPI(
        title="Open VRM VTuber",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )

    # Populate state eagerly as well: some ASGI hosts (and tests) mount the app
    # without running the lifespan, and the routes read `app.state.settings`.
    app.state.settings = settings
    app.state.started_at = time.time()
    app.state.preflight = {}

    # CORS is restricted to local origins on purpose. `["*"]` with
    # `allow_credentials=True` is the classic way a local tool becomes a
    # cross-site request proxy for every page you happen to have open - and the
    # settings routes accept an API key.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.server.cors_origins or LOCAL_CORS_ORIGINS),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Requested-With"],
    )

    app.include_router(api_router)

    # One error shape for the whole HTTP surface (the WebSocket has its own,
    # sending an `error` frame before it closes - see `error_codes`).
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)

    # `/api/health` is the canonical check; `/health` is the path people type by
    # hand (and what most uptime probes default to), so serve it too.
    app.add_api_route("/health", health_endpoint, methods=["GET"], include_in_schema=False)

    # The WebSocket. `/ws` is canonical; `/` is an alias for clients pointed at
    # the bare origin, and anything else gets a readable 1008 instead of the
    # opaque "403 Forbidden" uvicorn logs when no route matches.
    app.add_api_websocket_route("/ws", websocket_endpoint)
    app.add_api_websocket_route("/", websocket_endpoint)
    app.add_api_websocket_route("/{path:path}", websocket_unknown_path)

    logger.debug(
        "WebSocket origins allowed: {} (same-origin always allowed)",
        ", ".join(effective_origins(settings)) or "(none)",
    )

    # Serve the built frontend (single port deployment). Mounted last so
    # /api and /ws keep priority.
    if settings.server.serve_frontend:
        dist = Path(settings.server.frontend_dist)
        if not dist.is_absolute():
            dist = (BACKEND_DIR / dist).resolve()
        if dist.exists() and (dist / "index.html").exists():
            app.mount("/", SpaStaticFiles(directory=dist, html=True), name="frontend")
            logger.info("serving the built frontend from {}", dist)
        else:
            logger.debug("no built frontend at {} (run `npm run build`)", dist)

    return app


def build_app() -> FastAPI:
    """Factory target for ``uvicorn --reload`` (import string form)."""
    from ..logging_setup import setup_logging

    settings = get_settings()
    setup_logging(settings.log)
    return create_app(settings)


__all__ = ["build_app", "create_app"]
