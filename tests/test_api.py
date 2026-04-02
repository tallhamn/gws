from fastapi.testclient import TestClient

from gws.api import create_app
from gws.config import Settings
from gws.db import Base, make_engine


def test_healthz_returns_ok(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_and_fetch_pull_request(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    create_response = client.post(
        "/pull-requests",
        json={
            "worker_id": "worker-1",
            "lane": "control",
            "repo_access_set": ["repo-a", "repo-b"],
            "envelope": {"branch": "feature/control-plane"},
        },
    )

    assert create_response.status_code == 202
    assert create_response.json() == {"pull_request_id": 1}

    fetch_response = client.get("/pull-requests/1")

    assert fetch_response.status_code == 200
    assert fetch_response.json() == {"status": "pending"}


def test_get_pull_request_returns_not_found_for_unknown_id(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/pull-requests/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Pull request not found"}


def test_create_pull_request_ignores_client_supplied_status(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    create_response = client.post(
        "/pull-requests",
        json={
            "worker_id": "worker-2",
            "lane": "control",
            "repo_access_set": ["repo-a"],
            "envelope": {"branch": "feature/ignore-status"},
            "status": "completed",
        },
    )

    assert create_response.status_code == 202
    assert create_response.json() == {"pull_request_id": 1}

    fetch_response = client.get("/pull-requests/1")

    assert fetch_response.status_code == 200
    assert fetch_response.json() == {"status": "pending"}
