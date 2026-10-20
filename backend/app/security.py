"""Secret hygiene: mask a key for display, and scrub keys out of every log line.

Two small jobs, both load-bearing.

* :func:`mask_secret` turns a credential into something safe to show a human
  (``gsk_1f2e****9c3a``). The full value never leaves the backend.
* :func:`redact` replaces anything that *looks* like a credential. Scrubbing only
  the one key we know about is not enough: provider error bodies routinely echo
  the request's ``Authorization`` header back at you, so patterns are scrubbed
  too - and the log sink applies this to the finished line, which means
  tracebacks are covered as well.

Never log the return value of :func:`mask_secret` next to the real value, and
never widen these patterns to something that would match ordinary prose.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ["install_secrets", "is_placeholder", "mask_secret", "placeholder_keys", "redact"]

#: Values that mean "this endpoint wants no authentication". They are *not*
#: secrets, and treating them as one is how you end up sending
#: ``Authorization: Bearer ollama`` to a paid hosted provider.
_PLACEHOLDERS: frozenset[str] = frozenset(
    {"", "none", "null", "ollama", "lm-studio", "not-needed", "not needed", "changeme"}
)

#: Everything we recognise as a credential, scrubbed in this order. The last two
#: are structural (a JSON/YAML field or an auth header) rather than brand
#: specific, because a self-hosted gateway can use any prefix it likes.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Branded prefixes. Long enough that ordinary words cannot match.
    (re.compile(r"\b(?:gsk_|sk-or-|sk-|xai-|hf_|glpat-)[A-Za-z0-9_\-]{12,}"), "***REDACTED***"),
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"), "***REDACTED***"),
    # "Authorization: Bearer <anything>"
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{6,}"), r"\1 ***REDACTED***"),
    # A field named like a secret: {"api_key": "..."} / api_key=... / "token": "..."
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|apikey|authorization|access[_-]?token|secret[_-]?key)[\"']?"
            r"\s*[:=]\s*[\"']?)([^\"'\s,})]{6,})"
        ),
        r"\1***REDACTED***",
    ),
)

#: Exact values to scrub unconditionally, registered at startup. Belt and braces
#: for the case where a key travels somewhere no pattern would guess.
_KNOWN_SECRETS: set[str] = set()


def placeholder_keys() -> frozenset[str]:
    """Values that mean "no authentication" rather than "a key is configured"."""
    return _PLACEHOLDERS


def is_placeholder(value: str | None) -> bool:
    """True when ``value`` means "no key", rather than being an actual key.

    ``"ollama"`` is the shipped default, so this is what stops the app from
    sending ``Authorization: Bearer ollama`` to a paid hosted provider.
    """
    return (value or "").strip().lower() in _PLACEHOLDERS


def install_secrets(values: Iterable[str]) -> None:
    """Register exact secret values so :func:`redact` removes them verbatim.

    Called once from ``main.py`` after the settings are loaded. Values shorter
    than 8 characters are ignored: redacting a short string would mangle prose.
    """
    for value in values:
        text = (value or "").strip()
        if len(text) >= 8 and text.lower() not in _PLACEHOLDERS:
            _KNOWN_SECRETS.add(text)


def redact(text: str) -> str:
    """Return ``text`` with every recognised credential replaced.

    Safe to call on any string, including one that contains none.
    """
    if not text:
        return text
    for secret in _KNOWN_SECRETS:
        if secret in text:
            text = text.replace(secret, "***REDACTED***")
    for pattern, replacement in _PATTERNS:
        if pattern.search(text):
            text = pattern.sub(replacement, text)
    return text


def mask_secret(value: str | None, *, keep: int = 4) -> str:
    """A short, human-checkable fingerprint of ``value``.

    ``""`` when nothing is configured, so the UI can tell "no key" from
    "a key is stored" without ever receiving the key. Only the first/last few
    characters are revealed - enough to recognise *which* key is in use when you
    have two, useless to anyone else.
    """
    text = (value or "").strip()
    if text.lower() in _PLACEHOLDERS or len(text) < keep * 2 + 1:
        return ""
    prefix = text[:4] if text.startswith(("gsk_", "sk-o", "sk-", "xai", "hf_")) else text[:2]
    return f"{prefix}****{text[-keep:]}"
