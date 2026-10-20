"""The one place an API key is allowed to live.

A key is entered by the user (in the settings UI or by editing a local file),
is written here, and is **never** sent to the browser and never logged. See
``SECURITY.md`` for the full story; this module is the mechanism.

Precedence is deliberately explicit:

1. ``VTUBER_LLM__API_KEY`` - an environment variable always wins, so a container
   secret or a CI box can override whatever is on disk.
2. ``backend/secrets.json`` - what the settings UI writes.
3. the placeholder in the config model (``"ollama"``, ``"none"``), which means
   "this endpoint needs no authentication".

The file is written atomically (temp file + :func:`os.replace`) so a crash can
never leave a half-written secret behind, and its permissions are narrowed to
the current user on Windows on a best-effort basis - if ``icacls`` is missing or
refuses, the write still succeeds and we say so once in the log.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from loguru import logger

from .config import BACKEND_DIR

__all__ = ["SECRETS_PATH", "delete_secret", "get_secret", "secrets_loaded", "set_secret"]

#: ``backend/secrets.json``. Listed in ``.gitignore`` and ``.dockerignore``.
SECRETS_PATH: Path = BACKEND_DIR / "secrets.json"

#: The key names this file may hold. Anything else in the file is ignored, so a
#: stray entry cannot smuggle arbitrary config in through the secret store.
_ALLOWED = frozenset({"llm_api_key"})

_lock = threading.Lock()

#: (mtime_ns, size) of the last successful read, plus the parsed payload. Lets a
#: hot path (every turn builds an LLM client) avoid re-reading the file while
#: still picking up an edit made by hand.
_cache_key: tuple[int, int] | None = None
_cache: dict[str, str] = {}
_acl_warned = False


def _read_uncached() -> dict[str, str]:
    global _cache_key, _cache
    try:
        stat = SECRETS_PATH.stat()
    except OSError:
        _cache_key = None
        _cache = {}
        return {}

    stamp = (stat.st_mtime_ns, stat.st_size)
    if _cache_key == stamp:
        return _cache

    try:
        raw = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # A hand-edited file with a trailing comma should not take the app down.
        logger.warning("could not read {} ({}); ignoring it", SECRETS_PATH.name, exc)
        _cache_key = stamp
        _cache = {}
        return {}

    parsed: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in _ALLOWED and isinstance(value, str) and value.strip():
                parsed[key] = value.strip()

    _cache_key = stamp
    _cache = parsed
    return parsed


def secrets_loaded() -> dict[str, str]:
    """Every stored secret, as a copy. Never log or serialise this."""
    with _lock:
        return dict(_read_uncached())


def get_secret(name: str) -> str:
    """One stored secret, or ``""``. ``name`` outside :data:`_ALLOWED` is ``""``."""
    if name not in _ALLOWED:
        return ""
    with _lock:
        return _read_uncached().get(name, "")


def get_llm_api_key() -> str:
    """The stored LLM key, or ``""`` when only a placeholder is configured."""
    return get_secret("llm_api_key")


def set_secret(name: str, value: str) -> None:
    """Store ``value`` under ``name``. An empty value deletes the entry.

    Raises :class:`ValueError` for an unknown name so a typo fails loudly rather
    than silently writing a file nobody ever reads.
    """
    global _cache_key, _acl_warned

    if name not in _ALLOWED:
        raise ValueError(f"unknown secret {name!r}; allowed: {sorted(_ALLOWED)}")

    with _lock:
        current = dict(_read_uncached())
        text = (value or "").strip()
        if text:
            current[name] = text
        else:
            current.pop(name, None)

        SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SECRETS_PATH.with_name(SECRETS_PATH.name + ".tmp")
        tmp.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, SECRETS_PATH)

        # Invalidate the mtime cache immediately: a UI save must be visible to
        # the very next turn, not after the clock advances far enough for the
        # filesystem timestamp to differ.
        _cache_key = None
        _cache.clear()

    if not _acl_warned:
        _harden_permissions(SECRETS_PATH)
        _acl_warned = True
    logger.info("stored {}{} ({} characters)", name, "" if text else " (cleared)", len(text))


def delete_secret(name: str) -> None:
    """:func:`set_secret` with an empty value."""
    set_secret(name, "")


def _harden_permissions(path: Path) -> None:
    """Best effort: make the file readable only by the current user.

    On Windows this drops inherited ACEs and grants only the current user.
    Anywhere else (or if ``icacls`` is unavailable) this is a no-op, and the
    file relies on ordinary filesystem permissions - which is all a local
    desktop app needs. Failure is not fatal; it is logged once.
    """
    if sys.platform != "win32":
        try:
            path.chmod(0o600)
        except OSError as exc:  # pragma: no cover - platform dependent
            logger.debug("could not chmod {}: {}", path.name, exc)
        return

    user = os.environ.get("USERNAME") or ""
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        logger.debug("could not restrict permissions on {}: {}", path.name, exc)
