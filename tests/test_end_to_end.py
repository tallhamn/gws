from fastapi.testclient import TestClient

from gws.api import create_app
from gws.config import Settings
from gws.control_plane import ControlPlaneService
from gws.db import Base, make_session_factory
from gws.models import Case, IntentVersion, Step, StepStatus, Verdict


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_completed_diff_appends_security_review_step_via_api(tmp_path, worker_registry_path):
    database_path = tmp_path / "end_to_end.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief"))
        case = Case(intent_id="intent-1", intent_version=1, title="Case", goal="Goal")
        step = Step(
            case=case,
            repo="repo-a",
            lane="coder",
            step_type="execute",
            status=StepStatus.READY,
            allowed_paths=["auth/**"],
            forbidden_paths=[],
        )
        session.add_all([case, step])
        session.commit()
        ControlPlaneService(session).issue_lease(step.id, worker_id="coder-1", ttl_seconds=60)
        step_id = step.id
        case_id = case.id

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        f"/steps/{step_id}/complete",
        json={
            "touched_paths": ["auth/session.py"],
            "changed_hunks": ["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        },
        headers=auth_headers("token-coder-1"),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}

    retry_response = client.post(
        f"/steps/{step_id}/complete",
        json={
            "touched_paths": ["auth/session.py"],
            "changed_hunks": ["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        },
        headers=auth_headers("token-coder-1"),
    )

    assert retry_response.status_code == 200
    assert retry_response.json() == {"status": "processed"}

    with session_factory() as session:
        steps = session.query(Step).filter(Step.case_id == case_id).order_by(Step.id).all()
        verdicts = session.query(Verdict).all()

    assert [step.lane for step in steps] == ["coder", "security-review"]
    assert [step.step_type for step in steps] == ["execute", "review"]
    assert [step.status for step in steps] == [StepStatus.SUCCEEDED, StepStatus.READY]
    assert len(verdicts) == 1


def test_completed_diff_rejects_expired_lease_via_api(tmp_path, worker_registry_path):
    database_path = tmp_path / "end_to_end_expired.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief"))
        case = Case(intent_id="intent-1", intent_version=1, title="Case", goal="Goal")
        step = Step(
            case=case,
            repo="repo-a",
            lane="coder",
            step_type="execute",
            status=StepStatus.READY,
            allowed_paths=["auth/**"],
            forbidden_paths=[],
        )
        session.add_all([case, step])
        session.commit()
        service = ControlPlaneService(session)
        service.issue_lease(step.id, worker_id="coder-1", ttl_seconds=60)
        service.expire_leases(now_offset_seconds=61)
        step_id = step.id

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        f"/steps/{step_id}/complete",
        json={
            "touched_paths": ["auth/session.py"],
            "changed_hunks": ["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        },
        headers=auth_headers("token-coder-1"),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "step has no active lease"}

    with session_factory() as session:
        step = session.get(Step, step_id)
        verdicts = session.query(Verdict).all()

    assert step is not None
    assert step.status == StepStatus.READY
    assert verdicts == []


def test_completed_diff_rejects_non_owner_worker_via_api(tmp_path, worker_registry_path):
    database_path = tmp_path / "end_to_end_forbidden.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief"))
        case = Case(intent_id="intent-1", intent_version=1, title="Case", goal="Goal")
        step = Step(
            case=case,
            repo="repo-a",
            lane="coder",
            step_type="execute",
            status=StepStatus.READY,
            allowed_paths=["auth/**"],
            forbidden_paths=[],
        )
        session.add_all([case, step])
        session.commit()
        ControlPlaneService(session).issue_lease(step.id, worker_id="coder-1", ttl_seconds=60)
        step_id = step.id

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        f"/steps/{step_id}/complete",
        json={
            "touched_paths": ["auth/session.py"],
            "changed_hunks": ["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        },
        headers=auth_headers("token-security-1"),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "step lease belongs to another worker"}

    with session_factory() as session:
        step = session.get(Step, step_id)
        verdicts = session.query(Verdict).all()

    assert step is not None
    assert step.status == StepStatus.LEASED
    assert verdicts == []
