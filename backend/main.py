#!/usr/bin/env python
"""Open VRM VTuber - backend entry point.

    python main.py                     # http://127.0.0.1:8000
    python main.py --port 8765         # custom port
    python main.py --reload            # auto-reload while developing
    python main.py --host 0.0.0.0      # expose on the LAN (use HTTPS in prod!)

Configuration lives in ``.env`` (see ``.env.example``), persona in
``characters/*.yaml``.

``--ws auto`` picks uvicorn's current WebSocket implementation (the
``websockets``-backed one, sans-io) instead of the deprecated alias, which used
to print a UvicornDeprecationWarning on every start.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python main.py` from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn
from loguru import logger

from app import __version__
from app.config import BACKEND_DIR, Settings, get_settings
from app.logging_setup import setup_logging
from app.security import install_secrets
from app.server.app import create_app

BANNER = r"""
  ___                 _   _  _____  __   _   _ _____ _____
 / _ \ _ __   ___ _ _| | | ||  _  \ \ \ / | | |_   _|  __ \
| (_) | '_ \ / _ \ '_ \ |_| || |_) | \ V /| |_| | | | | |_) |
 \___/| .__/ \___/| .__/\__,_||  _ <   |_|  \__,_| |_| |____/
      |_|         |_|         |_| \_\      real-time VRM VTuber  v{version}
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="open-vrm-vtuber",
        description="Real-time AI VTuber backend (FastAPI + WebSocket).",
    )
    parser.add_argument("--host", default=None, help="bind address (default: from .env)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: from .env)")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
        help="console log level",
    )
    parser.add_argument("--no-frontend", action="store_true", help="do not serve frontend/dist")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    settings: Settings = get_settings()
    if args.host:
        settings.server.host = args.host
    if args.port:
        settings.server.port = args.port
    if args.log_level:
        settings.log.level = args.log_level
    if args.no_frontend:
        settings.server.serve_frontend = False

    # Register the real secret values so the redaction layer can scrub them
    # verbatim, then start logging (the sinks themselves also pattern-match).
    install_secrets([settings.llm.effective_api_key(), settings.tts.api_key, settings.asr.api_key])
    setup_logging(settings.log)
    print(BANNER.format(version=__version__))
    logger.info("starting Open VRM VTuber v{} from {}", __version__, BACKEND_DIR)

    url = f"http://{settings.server.host}:{settings.server.port}"
    logger.info("open {} in your browser", url)

    if args.reload:
        # reload requires the import-string form so uvicorn can re-import.
        uvicorn.run(
            "app.server.app:build_app",
            factory=True,
            host=settings.server.host,
            port=settings.server.port,
            reload=True,
            reload_dirs=[str(BACKEND_DIR)],
            log_level=settings.log.level.lower(),
            ws="auto",
        )
    else:
        uvicorn.run(
            create_app(settings),
            host=settings.server.host,
            port=settings.server.port,
            log_level=settings.log.level.lower(),
            ws="auto",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
