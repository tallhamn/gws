from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError, StatementError

from gws.control_plane import ControlPlaneService
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


def _outcome_with_work_item(session, *, allowed_paths=None, forbidden_paths=None):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    outcome = Outcome(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music", phase=OutcomePhase.READY)
    work_item = WorkItem(
        outcome=outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
        allowed_paths=allowed_paths or ["services/**"],
        forbidden_paths=forbidden_paths or [],
        base_commit="abc123",
    )
    session.add_all([intent, outcome, work_item])
    session.commit()
    return outcome, work_item


def test_control_plane_imports_without_legacy_step_models():
    from gws.control_plane import ControlPlaneService as ImportedService

    assert ImportedService is ControlPlaneService


def test_issue_lease_for_work_item_marks_outcome_running_and_creates_attempt(session):
    outcome, work_item = _outcome_with_work_item(session)

    service = ControlPlaneService(session)
    lease = service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)
    session.refresh(work_item)
    session.refresh(outcome)

    attempt = session.query(Attempt).filter_by(lease_id=lease.id).one()

    assert lease.work_item_id == work_item.id
    assert work_item.status is WorkItemStatus.LEASED
    assert outcome.phase is OutcomePhase.RUNNING
    assert outcome.current_work_item_id == work_item.id
    assert attempt.work_item_id == work_item.id
    assert attempt.result_status is AttemptResultStatus.PENDING


def test_apply_attempt_completion_marks_outcome_completed_on_success(session):
    outcome, work_item = _outcome_with_work_item(session)

    service = ControlPlaneService(session)
    service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)

    service.apply_attempt_completion(
        work_item_id=work_item.id,
        worker_id="worker-1",
        touched_paths=["services/music/player.py"],
        changed_hunks=["+return play(queue)"],
    )

    session.refresh(work_item)
    session.refresh(outcome)

    assert work_item.status is WorkItemStatus.SUCCEEDED
    assert outcome.phase is OutcomePhase.COMPLETED
    assert outcome.result is OutcomeResult.SUCCEEDED
    assert outcome.result_summary == "execute completed successfully"
    assert session.query(OutcomeEvent).filter_by(event_type="outcome_completed").count() == 1


def test_extend_lease_records_outcome_event(session):
    outcome, work_item = _outcome_with_work_item(session)

    service = ControlPlaneService(session)
    lease = service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)
    original_deadline = lease.heartbeat_deadline

    extended = service.extend_lease(
        lease_id=lease.id,
        worker_id="worker-1",
        ttl_seconds=120,
        reason="close to finishing validation",
    )

    session.refresh(outcome)
    events = session.query(OutcomeEvent).filter_by(outcome_id=outcome.id, event_type="lease_extended").all()

    assert extended.heartbeat_deadline == original_deadline + timedelta(seconds=120)
    assert extended.expires_at == extended.heartbeat_deadline
    assert len(events) == 1
    assert events[0].payload == {
        "lease_id": lease.id,
        "worker_id": "worker-1",
        "ttl_seconds": 120,
        "reason": "close to finishing validation",
    }


def test_issue_lease_creates_required_lease_attempt_and_verdict_foundation(session):
    outcome, work_item = _outcome_with_work_item(session)

    service = ControlPlaneService(session)
    lease = service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)
    session.refresh(work_item)
    session.refresh(outcome)

    attempt = session.query(Attempt).filter_by(lease_id=lease.id).one()

    assert lease.worker_id == "worker-1"
    assert lease.lane == "coder"
    assert lease.base_commit == "abc123"
    assert lease.heartbeat_deadline == lease.issued_at + timedelta(seconds=60)
    assert lease.expires_at == lease.heartbeat_deadline
    assert work_item.status is WorkItemStatus.LEASED
    assert outcome.phase is OutcomePhase.RUNNING

    assert attempt.lease_id == lease.id
    assert attempt.worker_id == "worker-1"
    assert attempt.repo == "repo-a"
    assert attempt.result_status is AttemptResultStatus.PENDING
    assert attempt.artifact_refs == []
    assert attempt.submitted_diff_ref is None

    verdict = Verdict(attempt=attempt, result=VerdictResult.PASS)
    session.add(verdict)
    session.commit()
    session.refresh(verdict)

    assert verdict.id is not None
    assert verdict.attempt_id == attempt.id
    assert verdict.result is VerdictResult.PASS


def test_lease_heartbeat_and_expiration_follow_actual_deadlines(session):
    outcome, work_item = _outcome_with_work_item(session)

    service = ControlPlaneService(session)
    lease = service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)
    session.refresh(work_item)

    assert service.expire_leases() == 0
    session.refresh(work_item)
    session.refresh(lease)

    assert lease.expired_at is None
    assert work_item.status is WorkItemStatus.LEASED

    previous_deadline = lease.heartbeat_deadline
    refreshed_lease = service.heartbeat_lease(lease_id=lease.id, ttl_seconds=90)

    assert refreshed_lease.heartbeat_deadline == refreshed_lease.expires_at
    assert refreshed_lease.heartbeat_deadline > previous_deadline

    refreshed_lease.heartbeat_deadline = refreshed_lease.issued_at - timedelta(seconds=1)
    refreshed_lease.expires_at = refreshed_lease.heartbeat_deadline
    session.commit()

    with pytest.raises(ValueError, match="expired"):
        service.heartbeat_lease(lease_id=lease.id, ttl_seconds=90)

    expired_count = service.expire_leases()
    session.refresh(work_item)
    session.refresh(outcome)
    session.refresh(refreshed_lease)

    assert expired_count == 1
    assert refreshed_lease.expired_at == refreshed_lease.heartbeat_deadline
    assert work_item.status is WorkItemStatus.READY
    assert outcome.phase is OutcomePhase.READY
    assert outcome.current_work_item_id == work_item.id


def test_issue_lease_rejects_non_positive_ttl_and_second_active_lease(session):
    _, work_item = _outcome_with_work_item(session)

    service = ControlPlaneService(session)

    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=0)

    lease = service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)

    work_item.status = WorkItemStatus.READY
    session.commit()

    with pytest.raises(ValueError, match="active lease"):
        service.issue_lease(work_item_id=work_item.id, worker_id="worker-2", ttl_seconds=60)

    duplicate_lease = Lease(
        work_item_id=work_item.id,
        worker_id="worker-3",
        lane=work_item.lane,
        issued_at=lease.issued_at,
        heartbeat_deadline=lease.heartbeat_deadline,
        expires_at=lease.expires_at,
        base_commit=work_item.base_commit,
    )
    session.add(duplicate_lease)

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()


def test_heartbeat_rejects_non_positive_ttl_and_attempt_status_rejects_invalid_values(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    outcome = Outcome(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music", phase=OutcomePhase.READY)
    work_item = WorkItem(
        outcome=outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
    )
    other_work_item = WorkItem(
        outcome=outcome,
        sequence_index=1,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
    )
    session.add_all([intent, outcome, work_item, other_work_item])
    session.commit()

    service = ControlPlaneService(session)
    lease = service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)
    other_lease = Lease(
        work_item_id=other_work_item.id,
        worker_id="worker-2",
        lane=other_work_item.lane,
        issued_at=lease.issued_at,
        heartbeat_deadline=lease.heartbeat_deadline,
        expires_at=lease.expires_at,
        base_commit=other_work_item.base_commit,
        expired_at=lease.expires_at,
    )
    session.add(other_lease)
    session.commit()

    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        service.heartbeat_lease(lease_id=lease.id, ttl_seconds=0)

    invalid_attempt = Attempt(
        work_item_id=other_work_item.id,
        lease_id=other_lease.id,
        worker_id="worker-2",
        repo="repo-a",
        result_status="not-a-valid-status",
        artifact_refs=[],
    )
    session.add(invalid_attempt)

    with pytest.raises(StatementError, match="not among the defined enum values") as exc_info:
        session.commit()

    assert "uq_attempts_lease_id" not in str(exc_info.value)


def test_active_lease_index_declares_sqlite_and_postgres_partial_predicates():
    active_work_item_index = next(index for index in Lease.__table__.indexes if index.name == "uq_leases_active_work_item_id")

    sqlite_where = active_work_item_index.dialect_options["sqlite"].get("where")
    postgresql_where = active_work_item_index.dialect_options["postgresql"].get("where")

    assert str(sqlite_where) == "expired_at IS NULL"
    assert str(postgresql_where) == "expired_at IS NULL"


def test_apply_attempt_completion_appends_governance_work_items(session):
    outcome, work_item = _outcome_with_work_item(session, allowed_paths=["auth/**"])

    service = ControlPlaneService(session)
    service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)

    service.apply_attempt_completion(
        work_item_id=work_item.id,
        worker_id="worker-1",
        touched_paths=["auth/session.py"],
        changed_hunks=["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
    )

    session.refresh(outcome)
    work_items = session.query(WorkItem).filter(WorkItem.outcome_id == outcome.id).order_by(WorkItem.id).all()
    verdicts = session.query(Verdict).all()
    events = session.query(OutcomeEvent).filter_by(event_type="governance_work_items_appended").all()

    assert [item.lane for item in work_items] == ["coder", "security-review"]
    assert [item.work_type for item in work_items] == ["execute", "review"]
    assert [item.status for item in work_items] == [WorkItemStatus.SUCCEEDED, WorkItemStatus.READY]
    assert work_items[1].blocked_by_work_item_id == work_items[0].id
    assert outcome.phase is OutcomePhase.READY
    assert outcome.current_work_item_id == work_items[1].id
    assert len(verdicts) == 1
    assert len(events) == 1


def test_apply_attempt_completion_deduplicates_existing_review_work_item(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    outcome = Outcome(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music", phase=OutcomePhase.READY)
    execute_work_item = WorkItem(
        outcome=outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
        allowed_paths=["auth/**"],
        forbidden_paths=[],
    )
    existing_review = WorkItem(
        outcome=outcome,
        sequence_index=1,
        repo="repo-a",
        lane="security-review",
        work_type="review",
        status=WorkItemStatus.READY,
        allowed_paths=["auth/**"],
        forbidden_paths=[],
    )
    session.add_all([intent, outcome, execute_work_item, existing_review])
    session.commit()

    service = ControlPlaneService(session)
    service.issue_lease(work_item_id=execute_work_item.id, worker_id="worker-1", ttl_seconds=60)

    service.apply_attempt_completion(
        work_item_id=execute_work_item.id,
        worker_id="worker-1",
        touched_paths=["auth/session.py"],
        changed_hunks=["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
    )

    work_items = session.query(WorkItem).filter(WorkItem.outcome_id == outcome.id).order_by(WorkItem.id).all()
    verdicts = session.query(Verdict).all()

    assert [item.lane for item in work_items] == ["coder", "security-review"]
    assert [item.work_type for item in work_items] == ["execute", "review"]
    assert [item.status for item in work_items] == [WorkItemStatus.SUCCEEDED, WorkItemStatus.READY]
    assert len(verdicts) == 1


def test_apply_attempt_completion_uses_configured_policy_path(session, tmp_path):
    custom_policy_path = tmp_path / "custom-policy.yaml"
    custom_policy_path.write_text(
        """lanes:
  coder:
    lease_ttl_seconds: 900
path_triggers: []
content_triggers: []
merge_requirements:
  default: []
""",
        encoding="utf-8",
    )

    outcome, work_item = _outcome_with_work_item(session, allowed_paths=["auth/**"])

    service = ControlPlaneService(session, policy_path=str(custom_policy_path))
    service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)

    service.apply_attempt_completion(
        work_item_id=work_item.id,
        worker_id="worker-1",
        touched_paths=["auth/session.py"],
        changed_hunks=["+issuer = 'https://sso.example.com'"],
    )

    session.refresh(work_item)
    verdicts = session.query(Verdict).all()
    review_work_items = session.query(WorkItem).filter(WorkItem.outcome_id == outcome.id, WorkItem.work_type == "review").all()

    assert work_item.status is WorkItemStatus.SUCCEEDED
    assert verdicts[0].result is VerdictResult.PASS
    assert review_work_items == []


def test_apply_attempt_completion_rejects_non_owner_worker_without_mutating_lease_or_verdict(session):
    _, work_item = _outcome_with_work_item(session, allowed_paths=["auth/**"])

    service = ControlPlaneService(session)
    service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)

    with pytest.raises(PermissionError, match="work item lease belongs to another worker"):
        service.apply_attempt_completion(
            work_item_id=work_item.id,
            worker_id="worker-2",
            touched_paths=["auth/session.py"],
            changed_hunks=["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        )

    session.refresh(work_item)
    verdicts = session.query(Verdict).all()

    assert work_item.status is WorkItemStatus.LEASED
    assert verdicts == []
