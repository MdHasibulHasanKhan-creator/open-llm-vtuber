"""Shared test isolation.

The one thing every test needs is a memory profile that is *not* the real
``backend/memory/profile.json``. Long-term memory is on by default (that is the
feature), so without this an ordinary ``pytest`` run would quietly rewrite the
developer's own remembered facts. ``Settings`` is built directly in the tests -
``Settings(_env_file=None)`` only disables the ``.env`` file, environment
variables still apply - so an environment variable is the clean way to redirect
it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_memory(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Point every test's memory at a throwaway file."""
    monkeypatch.setenv("VTUBER_MEMORY__PATH", str(tmp_path / "profile.json"))


@pytest.fixture(autouse=True)
def _isolate_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Point the API-key store at a throwaway file.

    ``Settings.public_dict()`` now reports whether an LLM key is *stored*, so
    without this a plain ``pytest`` run would read the developer's real
    ``backend/secrets.json`` - and a masking assertion would silently depend on
    whose machine it ran on.
    """
    from app import secrets_store

    monkeypatch.setattr(secrets_store, "SECRETS_PATH", tmp_path / "secrets.json")
    monkeypatch.setattr(secrets_store, "_cache_key", None)
    monkeypatch.setattr(secrets_store, "_cache", {})
