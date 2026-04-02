from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError, StatementError

from gws.control_plane import ControlPlaneService
from gws.models import Attempt, AttemptResultStatus, Case, IntentVersion, Lease, Step, StepStatus, Verdict, VerdictResult


def test_issue_lease_creates_required_lease_attempt_and_verdict_foundation(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    step = Step(
        case=case,
        repo="repo-a",
        lane="coder",
        step_type="execute",
        status=StepStatus.READY,
        base_commit="abc123",
    )
    session.add_all([intent, case, step])
    session.commit()

    service = ControlPlaneService(session)
    lease = service.issue_lease(step_id=step.id, worker_id="worker-1", ttl_seconds=60)
    session.refresh(step)

    attempt = session.query(Attempt).filter_by(lease_id=lease.id).one()

    assert lease.step_id == step.id
    assert lease.worker_id == "worker-1"
    assert lease.lane == "coder"
    assert lease.base_commit == "abc123"
    assert lease.heartbeat_deadline == lease.issued_at + timedelta(seconds=60)
    assert lease.expires_at == lease.heartbeat_deadline
    assert step.status is StepStatus.LEASED

    assert attempt.step_id == step.id
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
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
    session.add_all([intent, case, step])
    session.commit()

    service = ControlPlaneService(session)
    lease = service.issue_lease(step_id=step.id, worker_id="worker-1", ttl_seconds=60)
    session.refresh(step)

    assert service.expire_leases() == 0
    session.refresh(step)
    session.refresh(lease)

    assert lease.expired_at is None
    assert step.status is StepStatus.LEASED

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
    session.refresh(step)
    session.refresh(refreshed_lease)

    assert expired_count == 1
    assert refreshed_lease.expired_at == refreshed_lease.heartbeat_deadline
    assert step.status is StepStatus.READY


def test_issue_lease_rejects_non_positive_ttl_and_second_active_lease(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
    session.add_all([intent, case, step])
    session.commit()

    service = ControlPlaneService(session)

    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        service.issue_lease(step_id=step.id, worker_id="worker-1", ttl_seconds=0)

    lease = service.issue_lease(step_id=step.id, worker_id="worker-1", ttl_seconds=60)

    step.status = StepStatus.READY
    session.commit()

    with pytest.raises(ValueError, match="active lease"):
        service.issue_lease(step_id=step.id, worker_id="worker-2", ttl_seconds=60)

    duplicate_lease = Lease(
        step_id=step.id,
        worker_id="worker-3",
        lane=step.lane,
        issued_at=lease.issued_at,
        heartbeat_deadline=lease.heartbeat_deadline,
        expires_at=lease.expires_at,
        base_commit=step.base_commit,
    )
    session.add(duplicate_lease)

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()


def test_heartbeat_rejects_non_positive_ttl_and_attempt_status_rejects_invalid_values(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
    other_step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
    session.add_all([intent, case, step])
    session.commit()

    service = ControlPlaneService(session)
    lease = service.issue_lease(step_id=step.id, worker_id="worker-1", ttl_seconds=60)
    other_lease = Lease(
        step_id=other_step.id,
        worker_id="worker-2",
        lane=other_step.lane,
        issued_at=lease.issued_at,
        heartbeat_deadline=lease.heartbeat_deadline,
        expires_at=lease.expires_at,
        base_commit=other_step.base_commit,
        expired_at=lease.expires_at,
    )
    session.add(other_lease)
    session.commit()

    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        service.heartbeat_lease(lease_id=lease.id, ttl_seconds=0)

    invalid_attempt = Attempt(
        step_id=other_step.id,
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
    active_lease_index = next(index for index in Lease.__table__.indexes if index.name == "uq_leases_active_step_id")

    sqlite_where = active_lease_index.dialect_options["sqlite"].get("where")
    postgresql_where = active_lease_index.dialect_options["postgresql"].get("where")

    assert str(sqlite_where) == "expired_at IS NULL"
    assert str(postgresql_where) == "expired_at IS NULL"


def test_apply_completed_diff_deduplicates_existing_review_step(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    execute_step = Step(
        case=case,
        repo="repo-a",
        lane="coder",
        step_type="execute",
        status=StepStatus.READY,
        allowed_paths=["auth/**"],
        forbidden_paths=[],
    )
    existing_review = Step(
        case=case,
        repo="repo-a",
        lane="security-review",
        step_type="review",
        status=StepStatus.READY,
        allowed_paths=["auth/**"],
        forbidden_paths=[],
    )
    session.add_all([intent, case, execute_step, existing_review])
    session.commit()

    service = ControlPlaneService(session)
    service.issue_lease(step_id=execute_step.id, worker_id="worker-1", ttl_seconds=60)

    service.apply_completed_diff(
        step_id=execute_step.id,
        worker_id="worker-1",
        touched_paths=["auth/session.py"],
        changed_hunks=["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
    )

    steps = session.query(Step).filter(Step.case_id == case.id).order_by(Step.id).all()
    verdicts = session.query(Verdict).all()

    assert [step.lane for step in steps] == ["coder", "security-review"]
    assert [step.step_type for step in steps] == ["execute", "review"]
    assert [step.status for step in steps] == [StepStatus.SUCCEEDED, StepStatus.READY]
    assert len(verdicts) == 1


def test_apply_completed_diff_rejects_non_owner_worker_without_mutating_lease_or_verdict(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    step = Step(
        case=case,
        repo="repo-a",
        lane="coder",
        step_type="execute",
        status=StepStatus.READY,
        allowed_paths=["auth/**"],
        forbidden_paths=[],
    )
    session.add_all([intent, case, step])
    session.commit()

    service = ControlPlaneService(session)
    service.issue_lease(step_id=step.id, worker_id="worker-1", ttl_seconds=60)

    with pytest.raises(PermissionError, match="step lease belongs to another worker"):
        service.apply_completed_diff(
            step_id=step.id,
            worker_id="worker-2",
            touched_paths=["auth/session.py"],
            changed_hunks=["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        )

    session.refresh(step)
    verdicts = session.query(Verdict).all()

    assert step.status is StepStatus.LEASED
    assert verdicts == []


def test_apply_completed_diff_rejects_non_owner_worker_after_owner_completion(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    execute_step = Step(
        case=case,
        repo="repo-a",
        lane="coder",
        step_type="execute",
        status=StepStatus.READY,
        allowed_paths=["auth/**"],
        forbidden_paths=[],
    )
    session.add_all([intent, case, execute_step])
    session.commit()

    service = ControlPlaneService(session)
    service.issue_lease(step_id=execute_step.id, worker_id="worker-1", ttl_seconds=60)

    service.apply_completed_diff(
        step_id=execute_step.id,
        worker_id="worker-1",
        touched_paths=["auth/session.py"],
        changed_hunks=["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
    )

    with pytest.raises(PermissionError, match="step lease belongs to another worker"):
        service.apply_completed_diff(
            step_id=execute_step.id,
            worker_id="worker-2",
            touched_paths=["auth/session.py"],
            changed_hunks=["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
        )

    steps = session.query(Step).filter(Step.case_id == case.id).order_by(Step.id).all()
    verdicts = session.query(Verdict).all()

    assert [step.lane for step in steps] == ["coder", "security-review"]
    assert [step.status for step in steps] == [StepStatus.SUCCEEDED, StepStatus.READY]
    assert len(verdicts) == 1
