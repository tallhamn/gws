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
            "intent_id": "intent-1",
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
            "intent_id": "intent-1",
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


def test_unauthenticated_request_returns_401_when_api_key_set(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}", api_key="secret-key")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        "/pull-requests",
        json={
            "worker_id": "worker-1",
            "lane": "coder",
            "intent_id": "intent-1",
            "repo_access_set": ["repo-a"],
            "envelope": {},
        },
    )

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


def test_authenticated_request_succeeds(tmp_path):
    database_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}", api_key="secret-key")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        "/pull-requests",
        json={
            "worker_id": "worker-1",
            "lane": "coder",
            "intent_id": "intent-1",
            "repo_access_set": ["repo-a"],
            "envelope": {},
        },
        headers={"Authorization": "Bearer secret-key"},
    )

    assert response.status_code == 202


def test_pull_step_issues_lease_for_ready_step(tmp_path):
    from gws.db import make_session_factory
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
    from gws.db import make_session_factory
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
