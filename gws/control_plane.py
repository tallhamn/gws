from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Attempt, AttemptResultStatus, Lease, Step, StepStatus, Verdict, VerdictResult
from .verifier import verify_attempt


class ControlPlaneService:
    def __init__(self, session: Session):
        self.session = session

    def apply_completed_diff(
        self,
        *,
        step_id: int,
        worker_id: str,
        touched_paths: list[str],
        changed_hunks: list[str],
    ) -> None:
        step = self.session.get(Step, step_id)
        if step is None:
            raise ValueError(f"unknown step_id: {step_id}")
        if step.status in {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.REVOKED}:
            return

        active_lease = (
            self.session.query(Lease)
            .filter(Lease.step_id == step_id, Lease.expired_at.is_(None))
            .order_by(Lease.id.desc())
            .first()
        )
        if active_lease is None or active_lease.heartbeat_deadline <= datetime.utcnow():
            raise ValueError("step has no active lease")
        if active_lease.worker_id != worker_id:
            raise PermissionError("step lease belongs to another worker")

        attempt = active_lease.attempt
        if attempt is None:
            raise ValueError("step has no attempt")

        verdict = verify_attempt(
            repo=step.repo,
            touched_paths=touched_paths,
            changed_hunks=changed_hunks,
            allowed_paths=list(step.allowed_paths),
            forbidden_paths=list(step.forbidden_paths),
        )
        attempt.submitted_diff_ref = "inline"
        if verdict.result in {VerdictResult.PASS.value, VerdictResult.APPEND_GOVERNANCE_STEP.value}:
            attempt.result_status = AttemptResultStatus.ACCEPTED
        else:
            attempt.result_status = AttemptResultStatus.REJECTED
        self.session.add(
            Verdict(
                attempt=attempt,
                result=VerdictResult(verdict.result),
            )
        )

        if verdict.result == VerdictResult.APPEND_GOVERNANCE_STEP.value:
            existing_review_lanes = {
                existing_step.lane
                for existing_step in step.case.steps
                if existing_step.id != step.id
                and existing_step.step_type == "review"
                and existing_step.status
                in {
                    StepStatus.PLANNING,
                    StepStatus.READY,
                    StepStatus.LEASED,
                    StepStatus.RUNNING,
                    StepStatus.VERIFYING,
                }
            }
            for lane in verdict.triggered_lanes:
                if lane in existing_review_lanes:
                    continue
                self.session.add(
                    Step(
                        case_id=step.case_id,
                        repo=step.repo,
                        lane=lane,
                        step_type="review",
                        status=StepStatus.READY,
                        allowed_paths=list(step.allowed_paths),
                        forbidden_paths=list(step.forbidden_paths),
                        base_commit=step.base_commit,
                    )
                )
            step.status = StepStatus.SUCCEEDED
        elif verdict.result == VerdictResult.PASS.value:
            step.status = StepStatus.SUCCEEDED
        else:
            step.status = StepStatus.FAILED

        self.session.commit()

    def issue_lease(self, step_id: int, worker_id: str, ttl_seconds: int) -> Lease:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        now = datetime.utcnow()
        step = self.session.get(Step, step_id)
        if step is None:
            raise ValueError(f"unknown step_id: {step_id}")
        active_lease = (
            self.session.query(Lease)
            .filter(Lease.step_id == step_id, Lease.expired_at.is_(None))
            .first()
        )
        if active_lease is not None:
            raise ValueError(f"step {step_id} already has an active lease")
        if step.status is not StepStatus.READY:
            raise ValueError(f"step {step_id} is not ready for lease issuance")

        deadline = now + timedelta(seconds=ttl_seconds)

        lease = Lease(
            step=step,
            worker_id=worker_id,
            lane=step.lane,
            issued_at=now,
            heartbeat_deadline=deadline,
            expires_at=deadline,
            base_commit=step.base_commit,
        )
        attempt = Attempt(
            step=step,
            lease=lease,
            worker_id=worker_id,
            repo=step.repo,
            result_status=AttemptResultStatus.PENDING,
            artifact_refs=[],
            submitted_diff_ref=None,
            created_at=now,
        )
        step.status = StepStatus.LEASED
        self.session.add_all([lease, attempt])
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ValueError(f"step {step_id} already has an active lease") from exc
        return lease

    def heartbeat_lease(self, lease_id: int, ttl_seconds: int = 60) -> Lease:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        now = datetime.utcnow()
        lease = self.session.get(Lease, lease_id)
        if lease is None:
            raise ValueError(f"unknown lease_id: {lease_id}")
        if lease.expired_at is not None or lease.heartbeat_deadline <= now:
            raise ValueError(f"lease {lease_id} is expired")

        deadline = now + timedelta(seconds=ttl_seconds)
        lease.heartbeat_deadline = deadline
        lease.expires_at = deadline
        self.session.commit()
        return lease

    def expire_leases(self, now_offset_seconds: int = 0) -> int:
        now = datetime.utcnow() + timedelta(seconds=now_offset_seconds)
        leases = (
            self.session.query(Lease)
            .filter(Lease.expired_at.is_(None), Lease.heartbeat_deadline <= now)
            .all()
        )
        for lease in leases:
            lease.expired_at = lease.heartbeat_deadline
            if lease.step.status is StepStatus.LEASED:
                lease.step.status = StepStatus.READY
        self.session.commit()
        return len(leases)
