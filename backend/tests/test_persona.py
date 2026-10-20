"""Persona editing: the saved override, and the routes that write it.

The user-visible promise is "she is yours to shape, and you cannot break her":

* an edit lands beside the shipped ``.yaml`` (which keeps its comments and stays
  the reference for **Reset**),
* an edit can never turn a working persona into a broken one - values are cleaned
  and re-validated, and a corrupt file is ignored rather than fatal,
* the running session re-reads the file without losing the conversation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.config import (
    CharacterConfig,
    Settings,
    character_detail,
    character_exists,
    clear_character_override,
    list_characters,
    load_character,
    load_character_override,
    save_character_override,
)
from app.server.app import create_app

ORIGIN = "http://127.0.0.1:8000"
FOREIGN = "http://evil.example"


@pytest.fixture()
def characters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private ``characters/`` directory with the shipped default copied in."""
    directory = tmp_path / "characters"
    directory.mkdir()
    shipped = config.CHARACTERS_DIR / "default.yaml"
    (directory / "default.yaml").write_text(shipped.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(config, "CHARACTERS_DIR", directory)
    return directory


@pytest.fixture()
def client(characters: Path) -> Iterator[TestClient]:
    settings = Settings(_env_file=None)
    settings.llm.provider = "echo"
    settings.tts.engine = "browser"
    settings.asr.engine = "browser"
    settings.server.serve_frontend = False
    settings.avatar.character = "default"
    settings.log.level = "WARNING"
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
#  The store
# --------------------------------------------------------------------------- #
def test_an_edit_lands_beside_the_yaml_and_is_picked_up(characters: Path):
    save_character_override("default", {"name": "Tuki", "tagline": "Small, loud, yours"})

    saved = characters / "default.user.json"
    assert saved.exists()
    assert json.loads(saved.read_text(encoding="utf-8")) == {
        "name": "Tuki",
        "tagline": "Small, loud, yours",
    }
    persona = load_character("default")
    assert persona.name == "Tuki"
    assert persona.tagline == "Small, loud, yours"
    # Untouched fields still come from the shipped file.
    assert "Ai" in persona.system_prompt


def test_the_shipped_file_is_never_rewritten(characters: Path):
    before = (characters / "default.yaml").read_text(encoding="utf-8")
    save_character_override("default", {"greeting": "Hi again."})
    assert (characters / "default.yaml").read_text(encoding="utf-8") == before


def test_an_empty_value_clears_the_field_back_to_shipped(characters: Path):
    save_character_override("default", {"name": "Tuki"})
    assert load_character("default").name == "Tuki"

    save_character_override("default", {"name": ""})

    assert load_character("default").name == "Ai"
    assert load_character_override("default") == {}
    # Nothing left to store: the override file goes away entirely.
    assert not (characters / "default.user.json").exists()


def test_clearing_reports_whether_there_was_anything_to_clear(characters: Path):
    assert clear_character_override("default") is False
    save_character_override("default", {"name": "Tuki"})
    assert clear_character_override("default") is True
    assert load_character("default").name == "Ai"


def test_a_silenced_greeting_is_remembered(characters: Path):
    """"Empty the box" means "do not speak on connect" for the greeting."""
    save_character_override("default", {"greeting": ""})
    assert load_character_override("default") == {"greeting": ""}
    assert load_character("default").greeting == ""
    # ...while an empty *name* is just "no edit".
    save_character_override("default", {"name": ""})
    assert load_character_override("default") == {"greeting": ""}
    assert load_character("default").name == "Ai"


def test_unknown_and_mistyped_fields_are_rejected(characters: Path):
    with pytest.raises(ValueError):
        save_character_override("default", {"emotions": "nope"})
    with pytest.raises(ValueError):
        save_character_override("default", {"name": 42})


def test_values_are_cleaned_and_capped(characters: Path):
    save_character_override("default", {"tagline": "  tidy\u0000   me  "})
    assert load_character("default").tagline == "tidy me"

    with pytest.raises(ValueError):
        save_character_override("default", {"name": "x" * 61})
    with pytest.raises(ValueError):
        save_character_override("default", {"system_prompt": "x" * 8001})


def test_the_system_prompt_keeps_its_newlines(characters: Path):
    prompt = "You are Ai.\n\nBe warm.\n* And curious."
    save_character_override("default", {"system_prompt": prompt})
    assert load_character("default").system_prompt == prompt


def test_a_corrupt_override_is_ignored_rather_than_fatal(characters: Path):
    (characters / "default.user.json").write_text("{not json", encoding="utf-8")
    assert load_character_override("default") == {}
    assert load_character("default").name == "Ai"


def test_keys_outside_the_editable_set_are_ignored(characters: Path):
    (characters / "default.user.json").write_text(
        json.dumps({"emotions": "not-a-mapping", "name": "Tuki"}), encoding="utf-8"
    )
    assert load_character_override("default") == {"name": "Tuki"}
    assert load_character("default").name == "Tuki"
    # The shipped emotion map is still intact.
    assert "happy" in load_character("default").emotions


def test_list_characters_shows_the_edited_persona(characters: Path):
    save_character_override("default", {"name": "Tuki", "tagline": "Mine"})
    found = list_characters()
    assert found == [{"id": "default", "name": "Tuki", "tagline": "Mine", "language": "en"}]


def test_a_missing_persona_file_is_the_built_in_character(characters: Path):
    assert not character_exists("nope")
    assert load_character("nope") == CharacterConfig()
    assert character_detail("nope")["overridden"] == []


def test_the_detail_reports_what_differs(characters: Path):
    save_character_override("default", {"name": "Tuki"})
    detail = character_detail("default")
    assert detail["id"] == "default"
    assert detail["overridden"] == ["name"]
    assert detail["character"]["name"] == "Tuki"
    assert detail["shipped"]["name"] == "Ai"
    # The editor offers exactly the fields the store accepts.
    assert set(detail["editable"]) >= {"name", "greeting", "system_prompt", "tts_voice"}


# --------------------------------------------------------------------------- #
#  The routes
# --------------------------------------------------------------------------- #
def test_character_round_trip(client: TestClient):
    read = client.get("/api/character?id=default", headers={"origin": ORIGIN})
    assert read.status_code == 200
    assert read.json()["character"]["name"] == "Ai"

    saved = client.put(
        "/api/character",
        json={"id": "default", "fields": {"name": "Tuki", "greeting": ""}},
        headers={"origin": ORIGIN},
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["character"]["name"] == "Tuki"
    # An emptied greeting is a *value* ("stay quiet on connect"), not a reset.
    assert body["character"]["greeting"] == ""
    assert body["overridden"] == ["greeting", "name"]

    reset = client.request(
        "DELETE", "/api/character?id=default", headers={"origin": ORIGIN}
    )
    assert reset.status_code == 200
    assert reset.json()["character"]["name"] == "Ai"
    assert reset.json()["overridden"] == []


def test_an_unknown_field_is_a_422_not_a_silent_no_op(client: TestClient):
    response = client.put(
        "/api/character",
        json={"id": "default", "fields": {"nope": "x"}},
        headers={"origin": ORIGIN},
    )
    assert response.status_code == 422


def test_an_unknown_persona_is_a_404(client: TestClient):
    assert client.get("/api/character?id=ghost", headers={"origin": ORIGIN}).status_code == 404
    assert (
        client.put(
            "/api/character",
            json={"id": "ghost", "fields": {"name": "x"}},
            headers={"origin": ORIGIN},
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/character?id=default", None),
        ("put", "/api/character", {"id": "default", "fields": {"name": "X"}}),
        ("delete", "/api/character?id=default", None),
    ],
)
def test_another_site_cannot_edit_the_persona(client: TestClient, method: str, path: str, body):
    response = client.request(method.upper(), path, json=body, headers={"origin": FOREIGN})
    assert response.status_code == 403
    # ...and nothing was written.
    assert load_character_override("default") == {}


# --------------------------------------------------------------------------- #
#  Live apply
# --------------------------------------------------------------------------- #
def _handshake(websocket) -> dict:  # type: ignore[no-untyped-def]
    """`ready` arrives unprompted; `hello` + its ack completes the handshake."""
    ready = json.loads(websocket.receive_text())
    assert ready["type"] == "ready"
    hello = {
        "type": "hello",
        "client": "pytest",
        "version": "1.0.0",
        "sample_rate": 16000,
        "client_side_asr": True,
        "greet": False,
    }
    websocket.send_text(json.dumps(hello))
    while True:
        message = json.loads(websocket.receive_text())
        if message["type"] == "hello.ack":
            return ready


def test_a_running_session_reloads_an_edited_persona(client: TestClient):
    with client.websocket_connect("/ws", headers={"origin": ORIGIN}) as websocket:
        ready = _handshake(websocket)
        assert ready["character"]["name"] == "Ai"

        # The settings UI saves an edit, then asks the server to re-read it.
        save_character_override("default", {"name": "Tuki", "language": "bn"})
        websocket.send_text(json.dumps({"type": "character.reload"}))

        ack = json.loads(websocket.receive_text())
        assert ack["type"] == "config.ack"
        assert ack["changed"] == ["character"]
        assert ack["config"]["character"]["name"] == "Tuki"
        assert ack["config"]["character"]["language"] == "bn"
