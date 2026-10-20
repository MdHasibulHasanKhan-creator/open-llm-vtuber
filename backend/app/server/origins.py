"""WebSocket origin policy.

Browsers attach an ``Origin`` header to the WebSocket handshake, and a page from
another site cannot forge it, which makes it the right place to decide who may
open a session. Two rules keep it usable in practice:

* **Same-origin is always allowed.** The request's ``Host`` is compared with the
  origin, so ``http://192.168.1.20:8000`` or ``https://vtuber.example.com`` work
  with no configuration at all - including behind a reverse proxy, where the
  server has no idea what its public name is.
* **Anything else must be listed.** ``server.ws_allowed_origins`` falls back to
  ``server.cors_origins`` (which defaults to the four local addresses), and
  ``["*"]`` disables the check for people who know what they are doing.

A *missing* ``Origin`` header is allowed on purpose: non-browser clients (a CLI,
a load balancer health check, the test suite) do not send one, and a browser
always does - so allowing the empty case costs nothing.

Rejections happen *after* ``accept()`` and close with 1008 plus an ``error``
frame. Rejecting before ``accept()`` is what makes uvicorn log the famously
unhelpful ``WebSocket / 403 / connection rejected`` - the browser never learns
why, and neither does the person reading the terminal.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..config import Settings

#: Close code 1008 (policy violation) - "this origin may not open a session".
WS_POLICY_VIOLATION = 1008


def effective_origins(settings: Settings) -> list[str]:
    """The origins the WebSocket accepts, ignoring same-origin."""
    configured = settings.server.ws_allowed_origins or settings.server.cors_origins
    return [origin.strip().rstrip("/") for origin in configured if origin.strip()]


def is_origin_allowed(
    origin: str | None,
    host: str | None,
    allowed: list[str],
) -> tuple[bool, str]:
    """Decide whether a handshake may proceed.

    Returns ``(allowed, reason)`` so the caller can log something actionable
    instead of a bare "403".
    """
    if origin is None or not origin.strip():
        return True, "no Origin header (non-browser client)"

    origin = origin.strip()
    if origin == "*":
        return False, "wildcard Origin is not a real browser origin"

    if "*" in allowed:
        return True, "ws_allowed_origins contains '*'"

    normalised = origin.rstrip("/").lower()
    if any(normalised == entry.rstrip("/").lower() for entry in allowed):
        return True, "listed in the allowed origins"

    if host and _same_origin(origin, host):
        return True, f"same origin as Host {host}"

    return False, "origin is not allowed and does not match the request Host"


def _same_origin(origin: str, host: str) -> bool:
    """True when ``origin`` and the ``Host`` header describe the same server."""
    try:
        split = urlsplit(origin)
    except ValueError:
        return False
    origin_host = split.netloc.lower().rstrip("/")
    if not origin_host:
        return False
    return origin_host == host.strip().lower().rstrip("/")


__all__ = ["WS_POLICY_VIOLATION", "effective_origins", "is_origin_allowed"]
