from fastapi.testclient import TestClient

from gws.api import create_app
from gws.config import Settings
from gws.db import Base, make_engine, make_session_factory


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


def test_worker_lease_issues_lease_for_ready_step(tmp_path, worker_registry_path):
    from gws.models import Case, IntentVersion, Step, StepStatus

    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
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

    response = client.post(
        "/worker/lease",
        json={"ttl_seconds": 60},
        headers=auth_headers("token-coder-1"),
    )

    assert response.status_code == 200
    data = response.json()
    assert "lease_id" in data
    assert "step_id" in data


def test_worker_lease_returns_404_when_no_ready_steps(tmp_path, worker_registry_path):
    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
        planner_provider="unknown",
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        "/worker/lease",
        json={"ttl_seconds": 60},
        headers=auth_headers("token-coder-1"),
    )

    assert response.status_code == 404


def test_worker_lease_returns_503_when_jit_planning_is_unavailable(tmp_path, worker_registry_path):
    from gws.models import IntentVersion

    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
        planner_provider="unknown",
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief"))
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        "/worker/lease",
        json={"ttl_seconds": 60, "intent_id": "intent-1", "repo_heads": {"repo-a": "abc123"}},
        headers=auth_headers("token-coder-1"),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Planning unavailable"}


def test_worker_heartbeat_extends_deadline(tmp_path, worker_registry_path):
    from gws.control_plane import ControlPlaneService
    from gws.models import Case, IntentVersion, Step, StepStatus

    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief")
        case = Case(intent_id="intent-1", intent_version=1, title="Case", goal="Goal")
        step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
        session.add_all([intent, case, step])
        session.commit()
        lease = ControlPlaneService(session).issue_lease(step.id, "coder-1", 60)
        lease_id = lease.id

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        f"/worker/leases/{lease_id}/heartbeat",
        json={"ttl_seconds": 120},
        headers=auth_headers("token-coder-1"),
    )

    assert response.status_code == 200
    assert "heartbeat_deadline" in response.json()


def test_worker_heartbeat_rejects_non_owner(tmp_path, worker_registry_path):
    from gws.control_plane import ControlPlaneService
    from gws.models import Case, IntentVersion, Step, StepStatus

    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief")
        case = Case(intent_id="intent-1", intent_version=1, title="Case", goal="Goal")
        step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
        session.add_all([intent, case, step])
        session.commit()
        lease = ControlPlaneService(session).issue_lease(step.id, "coder-1", 60)
        lease_id = lease.id

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        f"/worker/leases/{lease_id}/heartbeat",
        json={"ttl_seconds": 60},
        headers=auth_headers("token-security-1"),
    )

    assert response.status_code == 403


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
