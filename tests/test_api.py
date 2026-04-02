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


def test_create_and_fetch_pull_request(tmp_path, worker_registry_path):
    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    create_response = client.post(
        "/pull-requests",
        json={"intent_id": "intent-1", "envelope": {"branch": "feature/control-plane"}},
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
    assert pull_request.intent_id == "intent-1"
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


def test_create_pull_request_ignores_client_supplied_status(tmp_path, worker_registry_path):
    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    create_response = client.post(
        "/pull-requests",
        json={
            "worker_id": "spoofed-worker",
            "lane": "spoofed-lane",
            "intent_id": "intent-1",
            "repo_access_set": ["spoofed-repo"],
            "envelope": {"branch": "feature/ignore-status"},
            "status": "completed",
        },
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
    assert pull_request.intent_id == "intent-1"
    assert list(pull_request.repo_access_set) == ["repo-a", "repo-b"]
    assert pull_request.envelope == {"branch": "feature/ignore-status"}

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

    response = client.post(
        "/pull-requests",
        json={"intent_id": "intent-1", "envelope": {"branch": "feature/no-auth"}},
    )

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
        json={"intent_id": "intent-1", "envelope": {"branch": "feature/bad-token"}},
        headers=auth_headers("wrong-token"),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid worker token"}


def test_unauthenticated_request_returns_401_when_api_key_set(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}", api_key="secret-key")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.post("/leases/expire")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}


def test_healthz_does_not_require_auth(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}", api_key="secret-key")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200


def test_non_timeline_public_routes_still_require_api_key(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}", api_key="secret-key")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/not-a-route")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}


def test_authenticated_request_succeeds(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}", api_key="secret-key")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        "/leases/expire",
        headers={"Authorization": "Bearer secret-key"},
    )

    assert response.status_code == 200
    assert response.json() == {"expired_count": 0}


def test_pull_step_issues_lease_for_ready_step(tmp_path):
    from gws.models import Case, IntentVersion, Step, StepStatus

    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief")
        case = Case(intent_id="intent-1", intent_version=1, title="Case", goal="Goal")
        step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
        session.add_all([intent, case, step])
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.post("/lanes/coder/pull", json={"worker_id": "worker-1", "ttl_seconds": 60})

    assert response.status_code == 200
    data = response.json()
    assert "lease_id" in data
    assert "step_id" in data


def test_pull_step_returns_404_when_no_ready_steps(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.post("/lanes/coder/pull", json={"worker_id": "worker-1"})

    assert response.status_code == 404


def test_heartbeat_lease_extends_deadline(tmp_path):
    from gws.control_plane import ControlPlaneService
    from gws.models import Case, IntentVersion, Step, StepStatus

    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief")
        case = Case(intent_id="intent-1", intent_version=1, title="Case", goal="Goal")
        step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
        session.add_all([intent, case, step])
        session.commit()
        lease = ControlPlaneService(session).issue_lease(step.id, "worker-1", 60)
        lease_id = lease.id

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(f"/leases/{lease_id}/heartbeat", json={"ttl_seconds": 120})

    assert response.status_code == 200
    assert "heartbeat_deadline" in response.json()


def test_expire_leases_returns_count(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.post("/leases/expire")

    assert response.status_code == 200
    assert response.json() == {"expired_count": 0}
