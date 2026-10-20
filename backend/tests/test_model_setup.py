"""The REST surface that fixes "why are the replies a script?".

Four routes matter here: the free-provider presets, the local model discovery,
the connection test, and the memory viewer. They are what turns a paragraph of
documentation into a button someone will actually press.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.server.app import create_app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    settings = Settings(_env_file=None)
    settings.llm.provider = "echo"
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.server.serve_frontend = False
    settings.log.level = "ERROR"
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_presets_are_offered_with_a_free_option_first(client: TestClient):
    payload = client.get("/api/llm/presets").json()
    presets = payload["presets"]
    assert presets
    assert presets[0]["id"] == "ollama"
    assert presets[0]["needs_key"] is False
    assert any(preset["needs_key"] for preset in presets), "there must be a keyed option too"


def test_discovery_reports_every_endpoint_it_knows_even_when_none_answer(client: TestClient):
    payload = client.post("/api/llm/discover").json()
    endpoints = payload["endpoints"]
    assert endpoints, "the UI needs the full list to render 'not running' rows"
    for endpoint in endpoints:
        assert {"id", "label", "base_url", "reachable", "models", "detail"} <= set(endpoint)
    assert payload["configured"]["provider"] == "echo"


def test_connection_test_reports_success_for_a_working_engine(client: TestClient):
    """The offline brain always answers, so this path is deterministic."""
    payload = client.post("/api/llm/test", json={"llm": {"provider": "echo"}}).json()
    assert payload["ok"] is True
    assert payload["error"] == ""
    assert payload["reply"]
    assert payload["latency_ms"] >= 0


def test_connection_test_reports_failure_instead_of_raising(client: TestClient):
    payload = client.post(
        "/api/llm/test",
        json={"llm": {"provider": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "none"}},
    ).json()
    assert payload["ok"] is False
    assert payload["error"]


def test_a_bad_patch_is_reported_not_raised(client: TestClient):
    """A wrong provider name is a *result* of this route, not an exception."""
    response = client.post("/api/llm/test", json={"llm": {"provider": "not-a-provider"}})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]


def test_memory_route_reports_the_profile(client: TestClient):
    payload = client.get("/api/memory").json()
    assert payload["enabled"] is True
    assert "path" in payload
    assert set(payload["memory"]) >= {"name", "likes", "dislikes", "topics"}


def test_memory_can_be_forgotten(client: TestClient):
    payload = client.delete("/api/memory").json()
    assert payload["ok"] is True
    assert payload["memory"]["turns"] == 0
    assert payload["memory"]["likes"] == []
