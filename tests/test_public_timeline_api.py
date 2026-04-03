from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from gws.api import create_app
from gws.config import Settings
from gws.db import Base, make_engine, make_session_factory
from gws.models import (
    Attempt,
    AttemptResultStatus,
    IntentVersion,
    Lease,
    Outcome,
    OutcomeEvent,
    OutcomePhase,
    OutcomeResult,
    Verdict,
    VerdictResult,
    WorkItem,
    WorkItemStatus,
)


def _seed_outcome(
    session,
    *,
    intent_id: str,
    intent_version: int,
    title: str,
    goal: str,
    phase: OutcomePhase,
    result: OutcomeResult | None = None,
    result_summary: str = "",
    completed_at: datetime | None = None,
    worker_id: str = "",
    lane: str = "coder",
    work_status: WorkItemStatus,
    lease_window: tuple[datetime, datetime, datetime | None] | None = None,
    attempt_status: AttemptResultStatus | None = None,
    verdict_result: VerdictResult | None = None,
) -> Outcome:
    outcome = Outcome(
        intent_id=intent_id,
        intent_version=intent_version,
        title=title,
        goal=goal,
        phase=phase,
        result=result,
        result_summary=result_summary,
        completed_at=completed_at,
    )
    work_item = WorkItem(
        outcome=outcome,
        sequence_index=0,
        repo="studio-tactical-vector",
        lane=lane,
        work_type="execute",
        status=work_status,
        allowed_paths=["drops/vector-room/**"],
        forbidden_paths=[],
    )
    session.add_all([outcome, work_item])
    session.flush()
    outcome.current_work_item_id = work_item.id
    session.add(
        OutcomeEvent(
            outcome=outcome,
            event_type="planning_started",
            created_at=(completed_at or datetime.now(timezone.utc).replace(tzinfo=None)) - timedelta(minutes=1),
            payload={"worker_id": worker_id},
        )
    )

    lease = None
    if lease_window is not None:
        issued_at, heartbeat_deadline, expired_at = lease_window
        lease = Lease(
            work_item_id=work_item.id,
            worker_id=worker_id,
            lane=lane,
            issued_at=issued_at,
            heartbeat_deadline=heartbeat_deadline,
            expires_at=heartbeat_deadline,
            expired_at=expired_at,
        )
        session.add(lease)
        session.flush()

    if attempt_status is not None and lease is not None:
        attempt = Attempt(
            work_item_id=work_item.id,
            lease_id=lease.id,
            worker_id=worker_id,
            repo="studio-tactical-vector",
            result_status=attempt_status,
            artifact_refs=[],
            submitted_diff_ref="inline" if attempt_status is not AttemptResultStatus.PENDING else None,
            created_at=(lease.issued_at if lease is not None else completed_at)
            or datetime.now(timezone.utc).replace(tzinfo=None),
        )
        session.add(attempt)
        session.flush()
        if verdict_result is not None:
            session.add(
                Verdict(
                    attempt_id=attempt.id,
                    result=verdict_result,
                    created_at=completed_at or attempt.created_at,
                )
            )

    if completed_at is not None:
        session.add(
            OutcomeEvent(
                outcome=outcome,
                event_type="outcome_completed",
                created_at=completed_at,
                payload={"result": (result.value if result is not None else "")},
            )
        )

    return outcome


def test_public_timeline_returns_live_and_recent_events(tmp_path):
    database_path = tmp_path / "timeline.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        session.add(
            IntentVersion(
                intent_id="intent-vector-room",
                intent_version=1,
                brief_text="Build Vector Room\n\n- keep it compact",
            )
        )
        _seed_outcome(
            session,
            intent_id="intent-vector-room",
            intent_version=1,
            title="Build command room shell",
            goal="Create the command room shell and interaction layer",
            phase=OutcomePhase.COMPLETED,
            result=OutcomeResult.SUCCEEDED,
            result_summary="Command room shell shipped",
            completed_at=now - timedelta(minutes=19),
            worker_id="coder1",
            work_status=WorkItemStatus.SUCCEEDED,
            lease_window=(now - timedelta(minutes=30), now - timedelta(minutes=20), now - timedelta(minutes=20)),
            attempt_status=AttemptResultStatus.ACCEPTED,
            verdict_result=VerdictResult.PASS,
        )
        _seed_outcome(
            session,
            intent_id="intent-vector-room",
            intent_version=1,
            title="Build score layer",
            goal="Create the score and audio layer",
            phase=OutcomePhase.COMPLETED,
            result=OutcomeResult.FAILED,
            result_summary="Score layer failed policy checks",
            completed_at=now - timedelta(minutes=11),
            worker_id="musician",
            lane="musician",
            work_status=WorkItemStatus.FAILED,
            lease_window=(now - timedelta(minutes=18), now - timedelta(minutes=12), now - timedelta(minutes=12)),
            attempt_status=AttemptResultStatus.REJECTED,
            verdict_result=VerdictResult.FAIL_AND_REPLAN,
        )
        _seed_outcome(
            session,
            intent_id="intent-vector-room",
            intent_version=1,
            title="Build live command room",
            goal="Keep building the live command room interaction layer",
            phase=OutcomePhase.RUNNING,
            worker_id="coder2",
            work_status=WorkItemStatus.LEASED,
            lease_window=(now - timedelta(minutes=4), now + timedelta(minutes=11), None),
            attempt_status=AttemptResultStatus.PENDING,
        )
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/intent-vector-room/timeline")

    assert response.status_code == 200
    data = response.json()
    assert data["intent"]["intent_id"] == "intent-vector-room"
    assert data["intent"]["intent_version"] == 1
    assert data["outcome_progress"] == {
        "total_outcomes": 3,
        "completed_outcomes": 2,
        "active_outcomes": 1,
    }
    assert "case_progress" not in data
    assert data["timeline_events"][0]["brief_teaser"] == "Build Vector Room"
    assert data["timeline_events"][0]["brief_text"] == "Build Vector Room\n\n- keep it compact"
    assert data["now_building"]["worker_id"] == "coder2"
    assert data["now_building"]["lease_status"] == "live"
    assert data["timeline_events"][0]["sequence_label"] == "1. Concept brief locked"
    assert data["timeline_events"][0]["title"] == "Concept brief locked"
    assert [event["outcome"] for event in data["timeline_events"][1:]] == ["succeeded", "failed", "live"]


def test_public_timeline_brief_teaser_uses_first_readable_line(tmp_path):
    database_path = tmp_path / "timeline-teaser.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(
            IntentVersion(
                intent_id="intent-teaser",
                intent_version=1,
                brief_text="\n# Heading\n\n- bullet one\n  \nReadable teaser line\nAnother line",
            )
        )
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/intent-teaser/timeline")

    assert response.status_code == 200
    data = response.json()
    assert data["intent"]["brief_summary"] == "Readable teaser line"
    assert (
        data["timeline_events"][0]["brief_text"]
        == "\n# Heading\n\n- bullet one\n  \nReadable teaser line\nAnother line"
    )
    assert data["timeline_events"][0]["brief_teaser"] == "Readable teaser line"


def test_public_timeline_brief_teaser_skips_common_list_prefixes(tmp_path):
    database_path = tmp_path / "timeline-list-prefixes.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(
            IntentVersion(
                intent_id="intent-list-prefixes",
                intent_version=1,
                brief_text="\n* item one\n+ item two\n1. ordered item\nReadable teaser line",
            )
        )
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/intent-list-prefixes/timeline")

    assert response.status_code == 200
    data = response.json()
    assert data["timeline_events"][0]["brief_teaser"] == "Readable teaser line"


def test_public_timeline_brief_teaser_preserves_numeric_and_literal_dash_lines(tmp_path):
    database_path = tmp_path / "timeline-literal-lines.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(
            IntentVersion(
                intent_id="intent-literal-lines",
                intent_version=1,
                brief_text="2026.04 launch polish\n-keep this literal\n- item one",
            )
        )
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/intent-literal-lines/timeline")

    assert response.status_code == 200
    data = response.json()
    assert data["intent"]["brief_summary"] == "2026.04 launch polish"
    assert data["timeline_events"][0]["brief_teaser"] == "2026.04 launch polish"


def test_public_timeline_brief_teaser_falls_back_to_list_content(tmp_path):
    database_path = tmp_path / "timeline-checklist-only.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(
            IntentVersion(
                intent_id="intent-checklist-only",
                intent_version=1,
                brief_text="- [ ] outline scope\n* draft copy\n+ ship note\n1. final review",
            )
        )
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/intent-checklist-only/timeline")

    assert response.status_code == 200
    data = response.json()
    assert data["intent"]["brief_summary"] == "outline scope"
    assert data["timeline_events"][0]["brief_teaser"] == "outline scope"


def test_public_timeline_brief_teaser_skips_multi_hash_headings(tmp_path):
    database_path = tmp_path / "timeline-hash-prefixed.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(
            IntentVersion(
                intent_id="intent-hash-prefixed",
                intent_version=1,
                brief_text="## Heading\n#launch-week polish\n#123 fix auth copy\nReadable teaser line",
            )
        )
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/intent-hash-prefixed/timeline")

    assert response.status_code == 200
    data = response.json()
    assert data["intent"]["brief_summary"] == "#launch-week polish"
    assert data["timeline_events"][0]["brief_teaser"] == "#launch-week polish"


def test_public_timeline_brief_teaser_preserves_literal_dash_prefix(tmp_path):
    database_path = tmp_path / "timeline-literal-dash.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(
            IntentVersion(
                intent_id="intent-literal-dash",
                intent_version=1,
                brief_text="-keep this literal\nReadable teaser line",
            )
        )
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/intent-literal-dash/timeline")

    assert response.status_code == 200
    data = response.json()
    assert data["intent"]["brief_summary"] == "-keep this literal"
    assert data["timeline_events"][0]["brief_teaser"] == "-keep this literal"


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


def test_unknown_public_routes_bypass_api_key_middleware(tmp_path):
    database_path = tmp_path / "timeline-api-key.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}", api_key="secret-key")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/not-a-route")

    assert response.status_code == 404


def test_public_timeline_returns_quiet_now_building_when_no_active_outcome(tmp_path):
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


def test_public_timeline_reports_explicit_outcome_result(tmp_path):
    database_path = tmp_path / "timeline-explicit.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music"))
        _seed_outcome(
            session,
            intent_id="intent-1",
            intent_version=1,
            title="Create /music",
            goal="Implement /music",
            phase=OutcomePhase.COMPLETED,
            result=OutcomeResult.SUCCEEDED,
            result_summary="Shipped /music endpoint",
            completed_at=now - timedelta(minutes=3),
            worker_id="worker-1",
            work_status=WorkItemStatus.SUCCEEDED,
            lease_window=(now - timedelta(minutes=8), now - timedelta(minutes=4), now - timedelta(minutes=4)),
            attempt_status=AttemptResultStatus.ACCEPTED,
            verdict_result=VerdictResult.PASS,
        )
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/intent-1/timeline")

    assert response.status_code == 200
    data = response.json()
    assert data["timeline_events"][-1]["outcome"] == "succeeded"
    assert data["timeline_events"][-1]["what_was_built"] == "Shipped /music endpoint"
    assert data["now_building"]["lease_status"] == "idle"


def test_public_timeline_omits_queued_follow_on_work_items(tmp_path):
    database_path = tmp_path / "timeline-queued.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        session.add(IntentVersion(intent_id="intent-queued-state", intent_version=1, brief_text="Build queued state"))
        outcome = Outcome(
            intent_id="intent-queued-state",
            intent_version=1,
            title="Build queued follow-on",
            goal="Queued work items should not appear as failures",
            phase=OutcomePhase.READY,
        )
        work_item = WorkItem(
            outcome=outcome,
            sequence_index=0,
            repo="studio-tactical-vector",
            lane="coder",
            work_type="execute",
            status=WorkItemStatus.READY,
            allowed_paths=["drops/queued/**"],
            forbidden_paths=[],
        )
        session.add_all([outcome, work_item])
        session.flush()
        outcome.current_work_item_id = work_item.id
        session.commit()

    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/public/intents/intent-queued-state/timeline")

    assert response.status_code == 200
    data = response.json()
    assert [event["outcome"] for event in data["timeline_events"]] == ["succeeded"]
