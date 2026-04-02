from fastapi.testclient import TestClient

from gws.api import create_app
from gws.coordinator import PlanningCoordinator
from gws.config import Settings
from gws.control_plane import ControlPlaneService
from gws.db import Base, make_session_factory
from gws.models import (
    Case,
    IntentVersion,
    Outcome,
    OutcomePhase,
    OutcomeResult,
    Step,
    StepStatus,
    Verdict,
    WorkItem,
    WorkItemStatus,
)


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
        outcome = Outcome(intent_id="intent-1", intent_version=1, title="Case", goal="Goal", phase=OutcomePhase.READY)
        work_item = WorkItem(
            outcome=outcome,
            sequence_index=0,
            repo="repo-a",
            lane="coder",
            work_type="execute",
            status=WorkItemStatus.READY,
            allowed_paths=["auth/**"],
            forbidden_paths=[],
        )
        session.add_all([outcome, work_item])
        session.commit()
        ControlPlaneService(session).issue_lease(work_item_id=work_item.id, worker_id="coder-1", ttl_seconds=60)
        work_item_id = work_item.id
        outcome_id = outcome.id

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        f"/worker/work-items/{work_item_id}/complete",
        json={
            "touched_paths": ["auth/session.py"],
            "changed_hunks": ["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        },
        headers=auth_headers("token-coder-1"),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}

    retry_response = client.post(
        f"/worker/work-items/{work_item_id}/complete",
        json={
            "touched_paths": ["auth/session.py"],
            "changed_hunks": ["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        },
        headers=auth_headers("token-coder-1"),
    )

    assert retry_response.status_code == 200
    assert retry_response.json() == {"status": "processed"}

    with session_factory() as session:
        work_items = session.query(WorkItem).filter(WorkItem.outcome_id == outcome_id).order_by(WorkItem.id).all()
        verdicts = session.query(Verdict).all()

    assert [work_item.lane for work_item in work_items] == ["coder", "security-review"]
    assert [work_item.work_type for work_item in work_items] == ["execute", "review"]
    assert [work_item.status for work_item in work_items] == [WorkItemStatus.SUCCEEDED, WorkItemStatus.READY]
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
        outcome = Outcome(intent_id="intent-1", intent_version=1, title="Case", goal="Goal", phase=OutcomePhase.READY)
        work_item = WorkItem(
            outcome=outcome,
            sequence_index=0,
            repo="repo-a",
            lane="coder",
            work_type="execute",
            status=WorkItemStatus.READY,
            allowed_paths=["auth/**"],
            forbidden_paths=[],
        )
        session.add_all([outcome, work_item])
        session.commit()
        service = ControlPlaneService(session)
        service.issue_lease(work_item_id=work_item.id, worker_id="coder-1", ttl_seconds=60)
        service.expire_leases(now_offset_seconds=61)
        work_item_id = work_item.id

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        f"/worker/work-items/{work_item_id}/complete",
        json={
            "touched_paths": ["auth/session.py"],
            "changed_hunks": ["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        },
        headers=auth_headers("token-coder-1"),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "work item has no active lease"}

    with session_factory() as session:
        work_item = session.get(WorkItem, work_item_id)
        verdicts = session.query(Verdict).all()

    assert work_item is not None
    assert work_item.status == WorkItemStatus.READY
    assert verdicts == []


def test_full_flow_intent_to_step_completion(session):
    """Intent creation -> pull request -> plan -> lease -> execute -> submit -> verify."""
    from gws.models import PullRequest
    from gws.planner import PlannerService

    intent = IntentVersion(
        intent_id="game-1",
        intent_version=1,
        brief_text="Build a browser platformer",
        context="HTML/CSS/JS output. Pixel art style.",
        planner_guidance="Core loop before polish.",
    )
    session.add(intent)
    session.commit()

    pr = PullRequest(
        worker_id="coder1",
        lane="coder",
        intent_id="game-1",
        repo_access_set=["studio-ystackai"],
    )
    session.add(pr)
    session.commit()

    class MockPlanner:
        def synthesize(self, *, brief, lane, repo_heads, envelope, **kwargs):
            assert "lane_capabilities" in kwargs or kwargs.get("lane_capabilities") is None
            return {
                "title": "Build player movement",
                "goal": "Implement WASD controls",
                "repo": "studio-ystackai",
                "allowed_paths": ["src/**"],
                "forbidden_paths": [],
                "step_type": "execute",
            }

    planner_service = PlannerService(
        session,
        MockPlanner(),
        lane_capabilities={"coder": "Write game code."},
    )
    _case, step = planner_service.plan_pull_request(pr.id, {"studio-ystackai": "abc123"})
    assert step.status == StepStatus.READY

    service = ControlPlaneService(session)
    service.issue_lease(step_id=step.id, worker_id="coder1", ttl_seconds=900)
    assert step.status == StepStatus.LEASED

    service.apply_completed_diff(
        step_id=step.id,
        worker_id="coder1",
        touched_paths=["src/player.js"],
        changed_hunks=["+ function move() {}"],
    )
    session.refresh(step)
    assert step.status == StepStatus.SUCCEEDED


def test_full_flow_intent_to_outcome_completion(session):
    intent = IntentVersion(
        intent_id="game-2",
        intent_version=1,
        brief_text="Build a browser platformer",
        context="HTML/CSS/JS output. Pixel art style.",
        planner_guidance="Core loop before polish.",
    )
    session.add(intent)
    session.commit()

    class MockPlanner:
        def synthesize(self, *, brief, lane, repo_heads, envelope, **kwargs):
            assert "lane_capabilities" in kwargs or kwargs.get("lane_capabilities") is None
            return {
                "title": "Build player movement",
                "goal": "Implement WASD controls",
                "repo": "studio-ystackai",
                "allowed_paths": ["src/**"],
                "forbidden_paths": [],
                "step_type": "execute",
            }

    coordinator = PlanningCoordinator(
        session,
        planner_client=MockPlanner(),
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
        lane_capabilities={"coder": "Write game code."},
    )
    outcome, work_item = coordinator.plan_outcome(
        intent_id="game-2",
        worker_id="coder1",
        lane="coder",
        available_repos=["studio-ystackai"],
        repo_heads={"studio-ystackai": "abc123"},
    )
    assert outcome.phase == OutcomePhase.READY

    service = ControlPlaneService(session)
    service.issue_lease(work_item_id=work_item.id, worker_id="coder1", ttl_seconds=900)

    service.apply_attempt_completion(
        work_item_id=work_item.id,
        worker_id="coder1",
        touched_paths=["src/player.js"],
        changed_hunks=["+ function move() {}"],
    )
    session.refresh(outcome)

    assert outcome.phase is OutcomePhase.COMPLETED
    assert outcome.result is OutcomeResult.SUCCEEDED
    assert session.query(Verdict).count() == 1


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
        outcome = Outcome(intent_id="intent-1", intent_version=1, title="Case", goal="Goal", phase=OutcomePhase.READY)
        work_item = WorkItem(
            outcome=outcome,
            sequence_index=0,
            repo="repo-a",
            lane="coder",
            work_type="execute",
            status=WorkItemStatus.READY,
            allowed_paths=["auth/**"],
            forbidden_paths=[],
        )
        session.add_all([outcome, work_item])
        session.commit()
        ControlPlaneService(session).issue_lease(work_item_id=work_item.id, worker_id="coder-1", ttl_seconds=60)
        work_item_id = work_item.id

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        f"/worker/work-items/{work_item_id}/complete",
        json={
            "touched_paths": ["auth/session.py"],
            "changed_hunks": ["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        },
        headers=auth_headers("token-security-1"),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "work item lease belongs to another worker"}

    with session_factory() as session:
        work_item = session.get(WorkItem, work_item_id)
        verdicts = session.query(Verdict).all()

    assert work_item is not None
    assert work_item.status == WorkItemStatus.LEASED
    assert verdicts == []


def test_completed_diff_rejects_missing_authorization_via_api(tmp_path, worker_registry_path):
    database_path = tmp_path / "end_to_end_missing_auth.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief"))
        outcome = Outcome(intent_id="intent-1", intent_version=1, title="Case", goal="Goal", phase=OutcomePhase.READY)
        work_item = WorkItem(
            outcome=outcome,
            sequence_index=0,
            repo="repo-a",
            lane="coder",
            work_type="execute",
            status=WorkItemStatus.READY,
            allowed_paths=["auth/**"],
            forbidden_paths=[],
        )
        session.add_all([outcome, work_item])
        session.commit()
        ControlPlaneService(session).issue_lease(work_item_id=work_item.id, worker_id="coder-1", ttl_seconds=60)
        work_item_id = work_item.id

    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        f"/worker/work-items/{work_item_id}/complete",
        json={
            "touched_paths": ["auth/session.py"],
            "changed_hunks": ["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        },
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid authorization header"}

    with session_factory() as session:
        work_item = session.get(WorkItem, work_item_id)
        verdicts = session.query(Verdict).all()

    assert work_item is not None
    assert work_item.status == WorkItemStatus.LEASED
    assert verdicts == []
