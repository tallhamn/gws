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


def test_worker_lease_issues_lease_for_ready_work_item(tmp_path, worker_registry_path):
    from gws.models import IntentVersion, Outcome, OutcomePhase, WorkItem, WorkItemStatus

    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief")
        outcome = Outcome(
            intent_id="intent-1", intent_version=1, title="Outcome", goal="Goal", phase=OutcomePhase.READY
        )
        work_item = WorkItem(
            outcome=outcome,
            sequence_index=0,
            repo="repo-a",
            lane="coder",
            work_type="execute",
            status=WorkItemStatus.READY,
        )
        session.add_all([intent, outcome, work_item])
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
    assert "work_item_id" in data


def test_worker_lease_scopes_ready_work_to_requested_repo_and_intent(tmp_path, worker_registry_path):
    from gws.models import IntentVersion, Outcome, OutcomePhase, WorkItem, WorkItemStatus

    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        first_intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="first brief")
        second_intent = IntentVersion(intent_id="intent-2", intent_version=1, brief_text="second brief")
        first_outcome = Outcome(
            intent_id="intent-1", intent_version=1, title="First Outcome", goal="First Goal", phase=OutcomePhase.READY
        )
        second_outcome = Outcome(
            intent_id="intent-2", intent_version=1, title="Second Outcome", goal="Second Goal", phase=OutcomePhase.READY
        )
        first_work_item = WorkItem(
            outcome=first_outcome,
            sequence_index=0,
            repo="repo-a",
            lane="coder",
            work_type="execute",
            description="repo-a work",
            status=WorkItemStatus.READY,
        )
        second_work_item = WorkItem(
            outcome=second_outcome,
            sequence_index=0,
            repo="repo-b",
            lane="coder",
            work_type="execute",
            description="repo-b work",
            status=WorkItemStatus.READY,
        )
        session.add_all([first_intent, second_intent, first_outcome, second_outcome, first_work_item, second_work_item])
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        "/worker/lease",
        json={"ttl_seconds": 60, "intent_id": "intent-2", "repo_heads": {"repo-b": "abc123"}},
        headers=auth_headers("token-coder-1"),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["work_item_id"] == second_work_item.id
    assert data["repo"] == "repo-b"
    assert data["title"] == "Second Outcome"


def test_worker_lease_returns_404_when_no_ready_work_items(tmp_path, worker_registry_path):
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
    from gws.models import IntentVersion, Outcome, OutcomePhase, WorkItem, WorkItemStatus

    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief")
        outcome = Outcome(
            intent_id="intent-1", intent_version=1, title="Outcome", goal="Goal", phase=OutcomePhase.READY
        )
        work_item = WorkItem(
            outcome=outcome,
            sequence_index=0,
            repo="repo-a",
            lane="coder",
            work_type="execute",
            status=WorkItemStatus.READY,
        )
        session.add_all([intent, outcome, work_item])
        session.commit()
        lease = ControlPlaneService(session).issue_lease(work_item_id=work_item.id, worker_id="coder-1", ttl_seconds=60)
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
    from gws.models import IntentVersion, Outcome, OutcomePhase, WorkItem, WorkItemStatus

    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief")
        outcome = Outcome(
            intent_id="intent-1", intent_version=1, title="Outcome", goal="Goal", phase=OutcomePhase.READY
        )
        work_item = WorkItem(
            outcome=outcome,
            sequence_index=0,
            repo="repo-a",
            lane="coder",
            work_type="execute",
            status=WorkItemStatus.READY,
        )
        session.add_all([intent, outcome, work_item])
        session.commit()
        lease = ControlPlaneService(session).issue_lease(work_item_id=work_item.id, worker_id="coder-1", ttl_seconds=60)
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


def test_worker_can_extend_lease(tmp_path, worker_registry_path):
    from gws.control_plane import ControlPlaneService
    from gws.models import IntentVersion, Outcome, OutcomePhase, WorkItem, WorkItemStatus

    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief")
        outcome = Outcome(
            intent_id="intent-1", intent_version=1, title="Outcome", goal="Goal", phase=OutcomePhase.READY
        )
        work_item = WorkItem(
            outcome=outcome,
            sequence_index=0,
            repo="repo-a",
            lane="coder",
            work_type="execute",
            status=WorkItemStatus.READY,
        )
        session.add_all([intent, outcome, work_item])
        session.commit()
        lease = ControlPlaneService(session).issue_lease(work_item_id=work_item.id, worker_id="coder-1", ttl_seconds=60)
        lease_id = lease.id

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        f"/worker/leases/{lease_id}/extend",
        json={"ttl_seconds": 120, "reason": "close to finishing validation"},
        headers=auth_headers("token-coder-1"),
    )

    assert response.status_code == 200
    assert response.json()["lease_id"] == lease_id
    assert "heartbeat_deadline" in response.json()
