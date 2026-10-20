"""Unit tests for the WebSocket origin policy (`app/server/origins.py`).

The policy is deliberately split out of the endpoint so the rules can be checked
without a live socket: they are the difference between a working browser and the
opaque "403 Forbidden" everyone wastes an afternoon on.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.server.origins import effective_origins, is_origin_allowed

LOCAL_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]


@pytest.fixture()
def settings() -> Settings:
    return Settings(_env_file=None)


def test_defaults_cover_both_dev_and_backend_origins(settings: Settings):
    assert effective_origins(settings) == LOCAL_ORIGINS


def test_ws_allowed_origins_overrides_cors_origins(settings: Settings):
    settings.server.ws_allowed_origins = ["https://my.site"]
    assert effective_origins(settings) == ["https://my.site"]


def test_trailing_slashes_and_case_are_ignored(settings: Settings):
    allowed = effective_origins(settings)
    allowed_ok, _ = is_origin_allowed("HTTP://LocalHost:5173/", "testserver", allowed)
    assert allowed_ok


@pytest.mark.parametrize("origin", LOCAL_ORIGINS)
def test_local_origins_are_allowed(settings: Settings, origin: str):
    allowed, reason = is_origin_allowed(origin, "testserver", effective_origins(settings))
    assert allowed, reason


def test_missing_origin_is_allowed(settings: Settings):
    allowed, reason = is_origin_allowed(None, "testserver", effective_origins(settings))
    assert allowed
    assert "non-browser" in reason


def test_same_origin_works_for_any_host(settings: Settings):
    """LAN addresses, tunnels and real domains must work with no configuration."""
    for host in ("192.168.1.20:8000", "vtuber.example.com", "localhost:8765"):
        allowed, reason = is_origin_allowed(f"http://{host}", host, [])
        assert allowed, f"{host}: {reason}"


def test_https_origin_matches_http_host(settings: Settings):
    """A TLS-terminating proxy forwards Host but keeps the public Origin."""
    allowed, _ = is_origin_allowed("https://vtuber.example.com", "vtuber.example.com", [])
    assert allowed


def test_foreign_origin_is_rejected(settings: Settings):
    allowed, reason = is_origin_allowed("http://evil.example.com", "testserver", effective_origins(settings))
    assert not allowed
    assert "not allowed" in reason


def test_wildcard_allows_everything(settings: Settings):
    allowed, _ = is_origin_allowed("http://evil.example.com", "testserver", ["*"])
    assert allowed


def test_port_mismatch_is_not_same_origin(settings: Settings):
    allowed, _ = is_origin_allowed("http://localhost:9999", "localhost:8000", [])
    assert not allowed


def test_null_origin_is_not_same_origin(settings: Settings):
    """`file://` pages send the literal string "null"."""
    allowed, _ = is_origin_allowed("null", "localhost:8000", [])
    assert not allowed
