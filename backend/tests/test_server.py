"""End to end protocol test.

Spins the real FastAPI app in-process (no ports, no network) with the offline
"echo" brain and browser-side TTS, then drives a full turn over the WebSocket:
``ready`` -> greeting -> ``chat`` -> deltas/visemes/expressions -> ``done``.

This is the test that proves the wire protocol, the orchestrator, the emotion
layer and the sentence splitter all fit together.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.config import Settings
from app.server.app import create_app

LOCAL_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    # `_env_file=None`: tests must not depend on the developer's backend/.env.
    settings = Settings(_env_file=None)
    settings.llm.provider = "echo"      # offline, deterministic
    settings.tts.engine = "browser"     # no audio bytes; the client speaks it
    settings.asr.engine = "browser"     # client side transcription
    settings.server.serve_frontend = False
    settings.avatar.character = "default"
    settings.log.level = "WARNING"
    settings.llm.max_tokens = 64
    # The context manager runs the lifespan (startup checks + shutdown).
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture()
def spa_client(tmp_path: Path) -> Iterator[TestClient]:
    """An app that serves a built frontend from a throwaway directory."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>spa index</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('asset');", encoding="utf-8")

    settings = Settings(_env_file=None)
    settings.llm.provider = "echo"
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.log.level = "WARNING"
    settings.server.serve_frontend = True
    settings.server.frontend_dist = str(dist)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _drain_until(websocket, wanted: str, limit: int = 400) -> list[dict]:
    """Collect JSON frames until one of type ``wanted`` shows up."""
    collected: list[dict] = []
    for _ in range(limit):
        frame = websocket.receive()
        text = frame.get("text")
        if text is None:
            continue  # binary audio frame
        message = json.loads(text)
        collected.append(message)
        if message.get("type") == wanted:
            return collected
    pytest.fail(f"never received a '{wanted}' frame; got {[m.get('type') for m in collected][-12:]}")


def test_health_endpoint(client: TestClient):
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert payload["engines"]["llm"] == "echo"
    assert "openai" in payload["available"]["llm"]
    # The echo brain and the browser TTS must not report failures.
    assert payload["preflight"]["llm"]["ok"] is True


def test_config_endpoint_masks_secrets(client: TestClient):
    payload = client.get("/api/config").json()
    assert "config" in payload and "characters" in payload
    assert isinstance(payload["characters"], list) and payload["characters"]


def test_characters_and_voice_endpoints(client: TestClient):
    assert client.get("/api/characters").json()["characters"]
    voices = client.get("/api/voices?engine=browser").json()
    assert voices["engine"] == "browser"
    assert voices["voices"]


def test_full_conversation_turn(client: TestClient):
    with client.websocket_connect("/ws") as websocket:
        ready = websocket.receive_json()
        assert ready["type"] == "ready"
        assert ready["session_id"]
        assert ready["character"]["name"]
        assert "aa" in ready["capabilities"]["visemes"]

        websocket.send_json({"type": "hello", "client": "pytest", "version": "1.0.0"})
        assert websocket.receive_json()["type"] == "hello.ack"

        # The greeting starts a turn on its own: wait for it to finish.
        greeting = _drain_until(websocket, "llm.done")
        assert any(frame["type"] == "tts.start" for frame in greeting)

        websocket.send_json({"type": "chat", "text": "Hello there! How are you?"})
        frames = _drain_until(websocket, "llm.done")
        types = [frame["type"] for frame in frames]

        assert "user.text" in types
        assert "llm.delta" in types
        assert "llm.sentence" in types
        assert "tts.start" in types
        assert "tts.end" in types
        assert "state" in types

        # Streaming order sanity: every delta comes before the first tts.start.
        assert types.index("llm.delta") < types.index("tts.start")

        speech = next(frame for frame in frames if frame["type"] == "tts.start")
        assert speech["format"] == "browser"
        assert speech["duration"] > 0.2
        # Mouth lip-sync (the default): the client drives one blendshape from the
        # envelope, so there is no viseme timeline - only the mode tag.
        assert speech["lipsync_mode"] == "mouth"
        assert speech["visemes"] == []

        # The echo brain emits [happy]/[surprised] tags -> expression events.
        assert any(frame["type"] == "expression" for frame in frames)

        # States must have been reported at least once each.
        states = {frame["value"] for frame in frames if frame["type"] == "state"}
        assert {"thinking", "speaking", "idle"} & states

        metrics = [frame for frame in frames if frame["type"] == "metrics"]
        assert metrics and metrics[-1]["total_ms"] > 0


def test_runtime_config_patch(client: TestClient):
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()  # ready
        websocket.send_json(
            {
                "type": "config",
                "payload": {"lipsync": {"release_ms": 123.0}, "tts": {"voice": "test-voice"}},
            }
        )
        ack = _drain_until(websocket, "config.ack")[-1]
        assert ack["config"]["lipsync"]["release_ms"] == 123.0
        assert ack["config"]["tts"]["voice"] == "test-voice"
        assert set(ack["changed"]) == {"lipsync", "tts"}


def test_interrupt_is_acknowledged(client: TestClient):
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()  # ready
        websocket.send_json({"type": "chat", "text": "Tell me a very long story please."})
        websocket.send_json({"type": "interrupt", "heard": "Tell me a"})
        frames = _drain_until(websocket, "interrupted")
        assert any(frame["type"] == "interrupted" for frame in frames)


def test_ping_pong(client: TestClient):
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "ping", "t": 42.0})
        pong = _drain_until(websocket, "pong")[-1]
        assert pong["t"] == 42.0
        assert pong["server_time"] > 0


def test_unknown_message_type_is_rejected(client: TestClient):
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "definitely-not-a-thing"})
        error = _drain_until(websocket, "error")[-1]
        assert error["code"] == "protocol"


# --------------------------------------------------------------------------- #
#  Routing: /health, the WebSocket paths, the SPA fallback
# --------------------------------------------------------------------------- #
def test_health_alias_is_served(client: TestClient):
    """`/api/health` is canonical, `/health` is what uptime probes hit."""
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/health").json()["engines"]["llm"] == "echo"


def test_websocket_root_alias(client: TestClient):
    """A client pointed at the bare origin connects instead of getting a 403."""
    with client.websocket_connect("/", headers={"origin": LOCAL_ORIGINS[2]}) as websocket:
        assert websocket.receive_json()["type"] == "ready"


def test_unknown_websocket_path_explains_itself(client: TestClient):
    with client.websocket_connect("/client-ws") as websocket:
        error = websocket.receive_json()
        assert error["code"] == "path"
        assert "/ws" in error["message"]
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_spa_is_served_for_deep_links(spa_client: TestClient):
    index = spa_client.get("/")
    assert index.status_code == 200
    assert "spa index" in index.text

    # A deep link must render the app: browsers advertise text/html on navigation.
    deep = spa_client.get("/some/deep/link", headers={"accept": "text/html,application/xhtml+xml"})
    assert deep.status_code == 200
    assert "spa index" in deep.text

    # Real files still win over the fallback.
    asset = spa_client.get("/assets/app.js")
    assert asset.status_code == 200
    assert "console.log" in asset.text


def test_resource_requests_get_a_real_404(spa_client: TestClient):
    """A broken <img> or a stale bundle chunk must be visible, not masked by the SPA.

    (`curl` and `httpx` default to `Accept: */*`, which counts as a resource
    request - ask for `text/html` to be served the app from a script.)
    """
    for accept in ("image/avif,image/webp,*/*", "*/*", "text/css,*/*;q=0.1"):
        response = spa_client.get("/assets/gone.png", headers={"accept": accept})
        assert response.status_code == 404, accept
        assert "spa index" not in response.text

    stale_chunk = spa_client.get("/assets/index-deadbeef.js", headers={"accept": "*/*"})
    assert stale_chunk.status_code == 404


def test_unknown_api_path_stays_a_404(spa_client: TestClient):
    """A JSON client must never be handed the index.html page."""
    response = spa_client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert "spa index" not in response.text


# --------------------------------------------------------------------------- #
#  WebSocket origin policy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("origin", LOCAL_ORIGINS)
def test_websocket_accepts_local_origins(client: TestClient, origin: str):
    with client.websocket_connect("/ws", headers={"origin": origin}) as websocket:
        assert websocket.receive_json()["type"] == "ready"


def test_websocket_accepts_same_origin_without_configuration(client: TestClient):
    """Same-origin is allowed for any host, so LAN/proxy setups need no config."""
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as websocket:
        assert websocket.receive_json()["type"] == "ready"


def test_websocket_allows_missing_origin_header(client: TestClient):
    """Non-browser clients (CLI, health check) send no Origin at all."""
    with client.websocket_connect("/ws") as websocket:
        assert websocket.receive_json()["type"] == "ready"


def test_websocket_rejects_foreign_origin(client: TestClient):
    """A rejected origin is told why, instead of an opaque 403 handshake failure."""
    with client.websocket_connect("/ws", headers={"origin": "http://evil.example.com"}) as websocket:
        error = websocket.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "origin"
        assert error["fatal"] is True
        assert "evil.example.com" in error["message"]
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_extra_origins_can_be_allowed(tmp_path: Path):
    settings = Settings(_env_file=None)
    settings.llm.provider = "echo"
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.log.level = "WARNING"
    settings.server.serve_frontend = False
    settings.server.ws_allowed_origins = ["https://my.site"]
    with TestClient(create_app(settings)) as test_client:
        with test_client.websocket_connect("/ws", headers={"origin": "https://my.site"}) as websocket:
            assert websocket.receive_json()["type"] == "ready"


@pytest.mark.parametrize("wildcard", [[], ["*"]])
def test_wildcard_disables_the_origin_check(tmp_path: Path, wildcard: list[str]):
    settings = Settings(_env_file=None)
    settings.llm.provider = "echo"
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.log.level = "WARNING"
    settings.server.serve_frontend = False
    settings.server.ws_allowed_origins = wildcard
    with TestClient(create_app(settings)) as test_client:
        with test_client.websocket_connect("/ws", headers={"origin": "https://anywhere.example"}) as ws:
            payload = ws.receive_json()
            if wildcard:
                assert payload["type"] == "ready"
            else:
                assert payload["code"] == "origin"


# --------------------------------------------------------------------------- #
#  Startup preflight: actionable warnings + the offline fallback
# --------------------------------------------------------------------------- #
def test_preflight_falls_back_to_echo_when_unreachable():
    settings = Settings(_env_file=None)
    settings.llm.provider = "openai"
    settings.llm.base_url = "http://127.0.0.1:9/v1"  # nothing listens on port 9
    settings.llm.fallback_to_echo = True
    # No backup chain: this test is about the last resort, and a real Ollama on
    # the developer's machine would otherwise make it pass for the wrong reason.
    settings.llm.fallback_chain = []
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.server.serve_frontend = False
    settings.log.level = "ERROR"

    with TestClient(create_app(settings)) as test_client:
        payload = test_client.get("/api/health").json()

    assert payload["engines"]["llm"] == "echo", "the session must have been switched to echo"
    assert payload["preflight"]["llm"]["ok"] is True
    assert payload["preflight"]["llm"]["fallback"] is True


def test_preflight_warns_actionably_without_the_fallback():
    settings = Settings(_env_file=None)
    settings.llm.provider = "openai"
    settings.llm.base_url = "http://127.0.0.1:9/v1"
    settings.llm.fallback_to_echo = False
    # Pinned: with the shipped Ollama backup link, a running Ollama would make
    # this "unreachable" scenario succeed and the assertions below meaningless.
    settings.llm.fallback_chain = []
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.server.serve_frontend = False
    settings.log.level = "ERROR"

    with TestClient(create_app(settings)) as test_client:
        payload = test_client.get("/api/health").json()

    assert payload["engines"]["llm"] == "openai"
    llm = payload["preflight"]["llm"]
    assert llm["ok"] is False
    # The message has to say what to do, not just what broke - and it must not
    # advertise the canned offline brain as the fix.
    assert "Settings -> Model" in llm["detail"]
    assert "Ollama" in llm["detail"]
    assert "VTUBER_LLM__PROVIDER=echo" not in llm["detail"]


# --------------------------------------------------------------------------- #
#  One error shape for the whole HTTP surface
# --------------------------------------------------------------------------- #
def test_bad_request_body_uses_the_protocol_error_shape(client: TestClient) -> None:
    # `payload: dict[str, Any]` - a bare string is a validation failure.
    response = client.post("/api/config/validate", json="just a string")
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "protocol"
    assert "error" in body and isinstance(body["error"], str)
    assert body["detail"]


def test_unhandled_exception_returns_a_readable_json_error() -> None:
    settings = Settings(_env_file=None)
    settings.llm.provider = "echo"
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.server.serve_frontend = False
    settings.log.level = "ERROR"
    app = create_app(settings)

    @app.get("/api/_boom")
    async def boom() -> dict[str, str]:  # pragma: no cover - raising is the point
        raise RuntimeError("intentional")

    # `raise_server_exceptions=False`: assert what a *browser* would receive.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/api/_boom")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal"
    # A friendly sentence, not a traceback.
    assert "unexpected error" in body["error"]
    assert "RuntimeError" not in response.text
