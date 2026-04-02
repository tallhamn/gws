from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from gws.api import create_app
from gws.config import Settings
from gws.db import Base, make_session_factory
from gws.models import (
    Attempt,
    AttemptResultStatus,
    Case,
    IntentVersion,
    Lease,
    Step,
    StepStatus,
    Verdict,
    VerdictResult,
)


def test_public_timeline_returns_live_and_recent_events(tmp_path):
    database_path = tmp_path / "timeline.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        intent = IntentVersion(intent_id="intent-vector-room", intent_version=1, brief_text="Build Vector Room")
        case = Case(
            intent_id="intent-vector-room",
            intent_version=1,
            title="Build command room shell",
            goal="Create the command room shell and interaction layer",
        )
        succeeded_step = Step(
            case=case,
            repo="studio-tactical-vector",
            lane="coder",
            step_type="execute",
            status=StepStatus.SUCCEEDED,
            allowed_paths=["drops/vector-room/**"],
            forbidden_paths=[],
        )
        failed_step = Step(
            case=case,
            repo="studio-tactical-vector",
            lane="musician",
            step_type="execute",
            status=StepStatus.FAILED,
            allowed_paths=["drops/vector-room/audio/**"],
            forbidden_paths=[],
        )
        live_step = Step(
            case=case,
            repo="studio-tactical-vector",
            lane="coder",
            step_type="execute",
            status=StepStatus.LEASED,
            allowed_paths=["drops/vector-room/**"],
            forbidden_paths=[],
        )
        session.add_all([intent, case, succeeded_step, failed_step, live_step])
        session.commit()

        succeeded_lease = Lease(
            step_id=succeeded_step.id,
            worker_id="coder1",
            lane="coder",
            issued_at=now - timedelta(minutes=30),
            heartbeat_deadline=now - timedelta(minutes=20),
            expires_at=now - timedelta(minutes=20),
            expired_at=now - timedelta(minutes=20),
        )
        failed_lease = Lease(
            step_id=failed_step.id,
            worker_id="musician",
            lane="musician",
            issued_at=now - timedelta(minutes=18),
            heartbeat_deadline=now - timedelta(minutes=12),
            expires_at=now - timedelta(minutes=12),
            expired_at=now - timedelta(minutes=12),
        )
        live_lease = Lease(
            step_id=live_step.id,
            worker_id="coder2",
            lane="coder",
            issued_at=now - timedelta(minutes=4),
            heartbeat_deadline=now + timedelta(minutes=11),
            expires_at=now + timedelta(minutes=11),
            expired_at=None,
        )
        session.add_all([succeeded_lease, failed_lease, live_lease])
        session.commit()

        succeeded_attempt = Attempt(
            step_id=succeeded_step.id,
            lease_id=succeeded_lease.id,
            worker_id="coder1",
            repo="studio-tactical-vector",
            result_status=AttemptResultStatus.ACCEPTED,
            artifact_refs=[],
            submitted_diff_ref="inline",
            created_at=now - timedelta(minutes=29),
        )
        failed_attempt = Attempt(
            step_id=failed_step.id,
            lease_id=failed_lease.id,
            worker_id="musician",
            repo="studio-tactical-vector",
            result_status=AttemptResultStatus.REJECTED,
            artifact_refs=[],
            submitted_diff_ref="inline",
            created_at=now - timedelta(minutes=17),
        )
        live_attempt = Attempt(
            step_id=live_step.id,
            lease_id=live_lease.id,
            worker_id="coder2",
            repo="studio-tactical-vector",
            result_status=AttemptResultStatus.PENDING,
            artifact_refs=[],
            submitted_diff_ref=None,
            created_at=now - timedelta(minutes=4),
        )
        session.add_all([succeeded_attempt, failed_attempt, live_attempt])
        session.commit()

        session.add_all(
            [
                Verdict(attempt_id=succeeded_attempt.id, result=VerdictResult.PASS, created_at=now - timedelta(minutes=19)),
                Verdict(
                    attempt_id=failed_attempt.id,
                    result=VerdictResult.FAIL_AND_REPLAN,
                    created_at=now - timedelta(minutes=11),
                ),
            ]
        )
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/intent-vector-room/timeline")

    assert response.status_code == 200
    data = response.json()
    assert data["intent"]["intent_id"] == "intent-vector-room"
    assert data["now_building"]["worker_id"] == "coder2"
    assert data["now_building"]["lease_status"] == "live"
    assert data["timeline_events"][0]["sequence_label"] == "1. Concept brief locked"
    assert data["timeline_events"][0]["title"] == "Concept brief locked"
    assert [event["outcome"] for event in data["timeline_events"][1:]] == ["succeeded", "failed", "live"]


def test_public_timeline_returns_404_for_unknown_intent(tmp_path):
    database_path = tmp_path / "timeline-missing.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/missing-intent/timeline")

    assert response.status_code == 404
    assert response.json() == {"detail": "Intent not found"}


def test_public_timeline_returns_quiet_now_building_when_no_active_lease(tmp_path):
    database_path = tmp_path / "timeline-idle.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(IntentVersion(intent_id="intent-idle", intent_version=1, brief_text="Build idle drop"))
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/intent-idle/timeline")

    assert response.status_code == 200
    data = response.json()
    assert data["now_building"]["lease_status"] == "idle"
    assert data["timeline_events"][0]["title"] == "Concept brief locked"
