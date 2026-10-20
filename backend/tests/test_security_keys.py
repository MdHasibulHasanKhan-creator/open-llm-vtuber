"""Security invariants for the API key.

These are regression tests for the things that must **never** happen, so they are
written as "assert the key is absent" rather than "assert the mask looks like X".
A fake key is enough: the property under test is that a credential cannot come
back out of any endpoint, not that any particular key is stored.

``conftest.py`` points the secret store at a throwaway file, so these never touch
the developer's real ``backend/secrets.json``.

check-secrets: allow-file -- every key-shaped string in this file is a deliberate
fake fixture. Scanning it would only train people to ignore the scanner.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.secrets_store import get_llm_api_key, set_secret
from app.server.app import create_app

#: Key-shaped, so the redaction patterns would catch it, but entirely fake.
FAKE_KEY = "gsk_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"


@pytest.fixture()
def client():
    settings = Settings(_env_file=None)
    settings.llm.provider = "openai"
    # A closed port: preflight fails in milliseconds instead of reaching the
    # network, and no provider is contacted by the test suite.
    settings.llm.base_url = "http://127.0.0.1:9/v1"
    settings.llm.model = "qwen/qwen3.8-27b"
    settings.llm.api_key = "ollama"  # a placeholder, i.e. "no authentication"
    settings.llm.auto_discover = False
    settings.llm.fallback_to_echo = False
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.server.serve_frontend = False
    settings.log.level = "ERROR"
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
#  The value never comes back out
# --------------------------------------------------------------------------- #
def test_saving_a_key_returns_only_a_fingerprint(client):
    saved = client.put("/api/llm/key", json={"api_key": FAKE_KEY}).json()

    assert saved["ok"] is True
    assert saved["changed"] is True
    assert saved["api_key_set"] is True
    assert FAKE_KEY not in str(saved)
    assert saved["api_key"].endswith(FAKE_KEY[-4:])
    assert get_llm_api_key() == FAKE_KEY


def test_config_endpoint_never_carries_the_key(client):
    client.put("/api/llm/key", json={"api_key": FAKE_KEY})

    response = client.get("/api/config")
    assert FAKE_KEY not in response.text
    llm = response.json()["config"]["llm"]
    assert llm["api_key_set"] is True
    assert FAKE_KEY not in llm["api_key"]
    assert llm["api_key"].startswith("gsk_")  # the brand prefix, not the secret


def test_status_endpoint_reports_location_not_value(client):
    client.put("/api/llm/key", json={"api_key": FAKE_KEY})

    response = client.get("/api/llm/status")
    assert FAKE_KEY not in response.text
    payload = response.json()
    assert payload["key_source"] == "secrets.json"
    # A localhost endpoint needs no authentication, so nothing is required here.
    assert payload["requires_key"] is False


def test_remote_endpoints_are_flagged_as_needing_a_key():
    from app.server.http_routes import _is_local

    assert _is_local("http://127.0.0.1:11434/v1") is True
    assert _is_local("http://localhost:11434/v1") is True
    assert _is_local("https://api.groq.com/openai/v1") is False


def test_echoing_the_fingerprint_keeps_the_stored_key(client):
    client.put("/api/llm/key", json={"api_key": FAKE_KEY})
    fingerprint = client.get("/api/llm/status").json()["api_key"]

    result = client.put("/api/llm/key", json={"api_key": fingerprint}).json()

    assert result["changed"] is False
    assert get_llm_api_key() == FAKE_KEY


def test_clearing_the_key(client):
    client.put("/api/llm/key", json={"api_key": FAKE_KEY})

    cleared = client.delete("/api/llm/key").json()

    assert cleared["api_key_set"] is False
    assert cleared["api_key"] == ""
    assert get_llm_api_key() == ""


def test_a_placeholder_is_not_reported_as_a_key(client):
    llm = client.get("/api/config").json()["config"]["llm"]
    assert llm["api_key"] == ""
    assert llm["api_key_set"] is False


# --------------------------------------------------------------------------- #
#  Cross-origin writes are refused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("origin", ["https://evil.example", "http://attacker.test:8080"])
def test_cross_origin_key_writes_are_rejected(client, origin):
    put = client.put("/api/llm/key", json={"api_key": FAKE_KEY}, headers={"Origin": origin})
    deleted = client.delete("/api/llm/key", headers={"Origin": origin})

    assert put.status_code == 403
    assert deleted.status_code == 403
    assert get_llm_api_key() == ""  # nothing was written


def test_same_origin_key_write_is_allowed(client):
    for origin in ("http://127.0.0.1:8000", "http://localhost:5173"):
        response = client.put(
            "/api/llm/key",
            json={"api_key": FAKE_KEY},
            headers={"Origin": origin, "Host": "127.0.0.1:8000"},
        )
        assert response.status_code == 200, origin


def test_missing_origin_is_allowed(client):
    """A CLI or an uptime probe sends no Origin header; a browser always does."""
    assert client.put("/api/llm/key", json={"api_key": FAKE_KEY}).status_code == 200


def test_every_state_changing_endpoint_is_same_origin_only(client):
    """The routes that must never answer a cross-site request.

    Each one either writes something the user cannot get back (memory), spends a
    resource, or walks the machine: probing localhost ports is a port scanner, and
    a test round-trip spends API quota with the key of whoever is at the keyboard.
    """
    foreign = {"Origin": "https://evil.example"}

    assert client.delete("/api/memory", headers=foreign).status_code == 403
    assert client.get("/api/memory", headers=foreign).status_code == 403
    assert client.post("/api/llm/discover", headers=foreign).status_code == 403
    assert client.post("/api/config/validate", json={"llm": {}}, headers=foreign).status_code == 403
    # Guarded *before* the probe is attempted, so this costs no network timeout.
    assert client.post("/api/llm/test", json={"llm": {}}, headers=foreign).status_code == 403


def test_the_same_state_changing_endpoints_answer_our_own_ui(client):
    own = {"Origin": "http://localhost:5173", "Host": "127.0.0.1:8000"}

    assert client.get("/api/memory", headers=own).status_code == 200
    assert client.post("/api/config/validate", json={"llm": {}}, headers=own).status_code == 200
    assert client.get("/api/llm/status", headers=own).status_code == 200


def test_the_test_button_never_hides_behind_the_fallback_chain(client):
    """`/llm/test` must report the proposed setting, not a rescue by the backup.

    The fixture points at a closed port, so a chain that was still enabled would
    turn "your key is broken" into "OK" whenever a local Ollama happens to be
    running - the exact lie the button exists to prevent.
    """
    payload = client.post("/api/llm/test", json={"llm": {}}).json()

    assert payload["ok"] is False
    assert payload["provider"] == "openai"
    assert payload["base_url"] == "http://127.0.0.1:9/v1"


def test_status_reports_the_fallback_chain(client):
    payload = client.get("/api/llm/status").json()

    assert payload["fallback_enabled"] is True
    assert payload["fallback_chain"] == [
        {"provider": "ollama", "model": "llama3.2:3b", "base_url": "http://127.0.0.1:11434/v1"}
    ]
    assert payload["fallback_cooldown"] == 60.0


def test_config_masks_a_backup_links_own_key():
    """A chain link may carry its own gateway credential; it is masked too."""
    from app.config import FallbackTarget

    settings = Settings(_env_file=None)
    settings.llm.fallback_chain = [FallbackTarget(api_key=FAKE_KEY)]

    reported = settings.public_dict()["llm"]["fallback_chain"][0]["api_key"]

    assert FAKE_KEY not in reported
    assert reported.endswith(FAKE_KEY[-4:])
    assert "*" in reported


def test_echoing_a_masked_link_key_leaves_the_link_without_one():
    """The UI only ever sees the fingerprint, so echoing it means "unchanged"."""
    from app.config import FallbackTarget, apply_patch

    settings = Settings(_env_file=None)
    settings.llm.fallback_chain = [FallbackTarget(api_key=FAKE_KEY)]
    fingerprint = settings.public_dict()["llm"]["fallback_chain"][0]["api_key"]

    patched = apply_patch(
        settings,
        {"llm": {"fallback_chain": [{"provider": "ollama", "api_key": fingerprint}]}},
    )

    assert patched.llm.fallback_chain[0].api_key == ""


# --------------------------------------------------------------------------- #
#  Masking and redaction
# --------------------------------------------------------------------------- #
def test_mask_secret_never_reveals_the_value():
    from app.security import mask_secret

    masked = mask_secret(FAKE_KEY)

    assert FAKE_KEY not in masked
    assert "****" in masked
    assert masked.startswith("gsk_")
    assert masked.endswith(FAKE_KEY[-4:])


def test_mask_secret_ignores_placeholders_and_short_values():
    from app.security import mask_secret

    assert mask_secret("") == ""
    assert mask_secret("ollama") == ""
    assert mask_secret("none") == ""
    assert mask_secret("abc") == ""


def test_redaction_scrubs_key_shapes():
    from app.security import redact

    assert "gsk_" not in redact("using gsk_0123456789abcdefghijklmn")
    assert "sk-" not in redact("OpenAI sk-proj-abcdefghijklmnopqrstuvwxyz")
    assert "AIza" not in redact("google AIzaSyA1234567890abcdefghijklmnopqrs")
    assert "REDACTED" in redact("Authorization: Bearer abcdef1234567890")
    assert "REDACTED" in redact('{"api_key": "topsecretvalue"}')


def test_redaction_scrubs_a_registered_value_verbatim(monkeypatch):
    from app import security

    # Something no pattern would recognise, which is exactly why exact values are
    # registered at startup as well.
    monkeypatch.setattr(security, "_KNOWN_SECRETS", {"hunter2-but-longer"})

    assert "hunter2-but-longer" not in security.redact("leaked hunter2-but-longer here")


def test_secret_store_refuses_unknown_names():
    with pytest.raises(ValueError):
        set_secret("something_else", "value")
