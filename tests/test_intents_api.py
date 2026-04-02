import pytest
from fastapi.testclient import TestClient

from gws.api import create_app
from gws.config import Settings


@pytest.fixture()
def client():
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")
    app = create_app(settings)
    return TestClient(app)


def test_create_intent(client):
    resp = client.post("/intents", json={
        "intent_id": "game-1",
        "brief_text": "Build a side-scrolling platformer",
        "context": "Browser game. HTML/CSS/JS.",
        "planner_guidance": "Core loop first.",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["intent_id"] == "game-1"
    assert data["intent_version"] == 1


def test_create_intent_increments_version(client):
    client.post("/intents", json={
        "intent_id": "game-1",
        "brief_text": "Build a platformer",
    })
    resp = client.post("/intents", json={
        "intent_id": "game-1",
        "brief_text": "Build a platformer with boss fights",
    })
    assert resp.status_code == 201
    assert resp.json()["intent_version"] == 2


def test_create_intent_minimal(client):
    resp = client.post("/intents", json={
        "intent_id": "game-2",
        "brief_text": "Build something",
    })
    assert resp.status_code == 201
    assert resp.json()["intent_version"] == 1


def test_get_intent(client):
    client.post("/intents", json={
        "intent_id": "game-1",
        "brief_text": "Build a platformer",
        "context": "Browser game.",
    })
    resp = client.get("/intents/game-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["intent_id"] == "game-1"
    assert data["brief_text"] == "Build a platformer"
    assert data["context"] == "Browser game."
    assert data["intent_version"] == 1


def test_get_intent_not_found(client):
    resp = client.get("/intents/nonexistent")
    assert resp.status_code == 404
