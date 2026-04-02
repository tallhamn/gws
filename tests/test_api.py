from fastapi.testclient import TestClient

from gws.api import create_app
from gws.config import Settings
from gws.db import Base, make_engine, make_session_factory
from gws.models import PullRequest


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
        json={"envelope": {"branch": "feature/control-plane"}},
        headers=auth_headers("token-coder-1"),
    )

    assert create_response.status_code == 202
    assert create_response.json() == {"pull_request_id": 1}

    session_factory, _ = make_session_factory(settings.database_url)
    with session_factory() as session:
        pull_request = session.get(PullRequest, 1)

    assert pull_request is not None
    assert pull_request.worker_id == "coder-1"
    assert pull_request.lane == "coder"
    assert list(pull_request.repo_access_set) == ["repo-a", "repo-b"]
    assert pull_request.envelope == {"branch": "feature/control-plane"}

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
            "envelope": {"branch": "feature/ignore-status"},
            "status": "completed",
        },
        headers=auth_headers("token-coder-1"),
    )

    assert create_response.status_code == 202
    assert create_response.json() == {"pull_request_id": 1}

    fetch_response = client.get("/pull-requests/1")

    assert fetch_response.status_code == 200
    assert fetch_response.json() == {"status": "pending"}


def test_create_pull_request_rejects_missing_authorization(tmp_path, worker_registry_path):
    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.post("/pull-requests", json={"envelope": {"branch": "feature/no-auth"}})

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid authorization header"}


def test_create_pull_request_rejects_invalid_bearer_token(tmp_path, worker_registry_path):
    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        "/pull-requests",
        json={"envelope": {"branch": "feature/bad-token"}},
        headers=auth_headers("wrong-token"),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid worker token"}
