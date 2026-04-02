from fastapi.testclient import TestClient

from gws.api import create_app
from gws.config import Settings
from gws.control_plane import ControlPlaneService
from gws.db import Base, make_session_factory
from gws.models import Case, IntentVersion, Step, StepStatus, Verdict


def test_completed_diff_appends_security_review_step_via_api(tmp_path):
    database_path = tmp_path / "end_to_end.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
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
    )

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}

    retry_response = client.post(
        f"/steps/{step_id}/complete",
        json={
            "touched_paths": ["auth/session.py"],
            "changed_hunks": ["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        },
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


def test_completed_diff_rejects_expired_lease_via_api(tmp_path):
    database_path = tmp_path / "end_to_end_expired.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
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
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "step has no active lease"}

    with session_factory() as session:
        step = session.get(Step, step_id)
        verdicts = session.query(Verdict).all()

    assert step is not None
    assert step.status == StepStatus.READY
    assert verdicts == []


def test_full_flow_intent_to_step_completion(session):
    """Intent creation -> pull request -> plan -> lease -> execute -> submit -> verify."""
    from gws.models import IntentVersion, PullRequest, Step, StepStatus
    from gws.control_plane import ControlPlaneService
    from gws.planner import PlannerService

    # 1. Create intent with context
    intent = IntentVersion(
        intent_id="game-1",
        intent_version=1,
        brief_text="Build a browser platformer",
        context="HTML/CSS/JS output. Pixel art style.",
        planner_guidance="Core loop before polish.",
    )
    session.add(intent)
    session.commit()

    # 2. Worker creates pull request
    pr = PullRequest(
        worker_id="coder1",
        lane="coder",
        intent_id="game-1",
        repo_access_set=["studio-ystackai"],
    )
    session.add(pr)
    session.commit()

    # 3. Plan (with mock planner that returns valid plan)
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
        session, MockPlanner(),
        lane_capabilities={"coder": "Write game code."},
    )
    case, step = planner_service.plan_pull_request(
        pr.id, {"studio-ystackai": "abc123"},
    )
    assert step.status == StepStatus.READY

    # 4. Issue lease
    service = ControlPlaneService(session)
    lease = service.issue_lease(step_id=step.id, worker_id="coder1", ttl_seconds=900)
    assert step.status == StepStatus.LEASED

    # 5. Submit completed diff
    service.apply_completed_diff(
        step_id=step.id,
        touched_paths=["src/player.js"],
        changed_hunks=["+ function move() {}"],
    )
    session.refresh(step)
    assert step.status == StepStatus.SUCCEEDED
