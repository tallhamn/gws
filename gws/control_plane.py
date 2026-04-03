from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    Attempt,
    AttemptResultStatus,
    Lease,
    OutcomeEvent,
    OutcomePhase,
    OutcomeResult,
    Verdict,
    VerdictResult,
    WorkItem,
    WorkItemStatus,
)
from .verifier import verify_attempt

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ControlPlaneService:
    def __init__(self, session: Session, *, policy_path: str = "policy.yaml"):
        self.session = session
        self.policy_path = policy_path

    def _complete_outcome_from_work_item(
        self,
        work_item: WorkItem,
        *,
        result: OutcomeResult,
        summary: str,
        commit_ref: str | None = None,
    ) -> None:
        outcome = work_item.outcome
        outcome.phase = OutcomePhase.COMPLETED
        outcome.result = result
        outcome.result_summary = summary
        outcome.result_commit = commit_ref
        outcome.current_work_item_id = work_item.id
        outcome.completed_at = _utc_now()
        self.session.add(
            OutcomeEvent(
                outcome=outcome,
                event_type="outcome_completed",
                payload={"result": result.value, "work_item_id": work_item.id},
            )
        )

    def _append_governance_work_items(self, work_item: WorkItem, lanes: list[str]) -> None:
        outcome = work_item.outcome
        existing_review_lanes = {
            existing_work_item.lane
            for existing_work_item in outcome.work_items
            if existing_work_item.id != work_item.id
            and existing_work_item.work_type == "review"
            and existing_work_item.status
            in {
                WorkItemStatus.READY,
                WorkItemStatus.LEASED,
                WorkItemStatus.RUNNING,
                WorkItemStatus.VERIFYING,
            }
        }
        appended_work_items: list[WorkItem] = []
        next_sequence_index = max((existing.sequence_index for existing in outcome.work_items), default=-1) + 1
        for lane in lanes:
            if lane in existing_review_lanes:
                continue
            review_work_item = WorkItem(
                outcome=outcome,
                sequence_index=next_sequence_index,
                blocked_by_work_item=work_item,
                repo=work_item.repo,
                lane=lane,
                work_type="review",
                status=WorkItemStatus.READY,
                allowed_paths=list(work_item.allowed_paths),
                forbidden_paths=list(work_item.forbidden_paths),
                base_commit=work_item.base_commit,
                artifact_requirements=list(work_item.artifact_requirements),
            )
            self.session.add(review_work_item)
            appended_work_items.append(review_work_item)
            next_sequence_index += 1

        self.session.flush()
        outcome.phase = OutcomePhase.READY
        outcome.result = None
        outcome.result_summary = ""
        outcome.result_commit = None
        outcome.completed_at = None
        if appended_work_items:
            outcome.current_work_item_id = appended_work_items[0].id
        self.session.add(
            OutcomeEvent(
                outcome=outcome,
                event_type="governance_work_items_appended",
                payload={
                    "source_work_item_id": work_item.id,
                    "appended_work_item_ids": [item.id for item in appended_work_items],
                    "lanes": [item.lane for item in appended_work_items],
                },
            )
        )

    def issue_lease(
        self,
        *,
        work_item_id: int,
        worker_id: str,
        ttl_seconds: int,
    ) -> Lease:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        now = _utc_now()
        deadline = now + timedelta(seconds=ttl_seconds)

        work_item = self.session.get(WorkItem, work_item_id)
        if work_item is None:
            raise ValueError(f"unknown work_item_id: {work_item_id}")
        active_lease = (
            self.session.query(Lease)
            .filter(Lease.work_item_id == work_item_id, Lease.expired_at.is_(None))
            .with_for_update()
            .first()
        )
        if active_lease is not None:
            logger.warning("Work item %d already has an active lease, cannot issue new one", work_item_id)
            raise ValueError(f"work item {work_item_id} already has an active lease")
        if work_item.status is not WorkItemStatus.READY:
            logger.warning(
                "Work item %d is not ready for lease issuance (status=%s)",
                work_item_id,
                work_item.status.value,
            )
            raise ValueError(f"work item {work_item_id} is not ready for lease issuance")

        lease = Lease(
            work_item=work_item,
            worker_id=worker_id,
            lane=work_item.lane,
            issued_at=now,
            heartbeat_deadline=deadline,
            expires_at=deadline,
            base_commit=work_item.base_commit,
        )
        attempt = Attempt(
            work_item=work_item,
            lease=lease,
            worker_id=worker_id,
            repo=work_item.repo,
            result_status=AttemptResultStatus.PENDING,
            artifact_refs=[],
            submitted_diff_ref=None,
            created_at=now,
        )
        self.session.add_all([lease, attempt])
        work_item.status = WorkItemStatus.LEASED
        work_item.outcome.phase = OutcomePhase.RUNNING
        work_item.outcome.current_work_item_id = work_item.id

        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            logger.warning("Work item %d lease issuance failed due to integrity error", work_item_id)
            raise ValueError(f"work item {work_item_id} already has an active lease") from exc
        logger.info(
            "Lease %d issued for work item %d to worker %s (ttl=%ds)", lease.id, work_item_id, worker_id, ttl_seconds
        )
        return lease

    def apply_attempt_completion(
        self,
        *,
        work_item_id: int,
        worker_id: str,
        touched_paths: list[str],
        changed_hunks: list[str],
    ) -> None:
        work_item = self.session.get(WorkItem, work_item_id)
        if work_item is None:
            raise ValueError(f"unknown work_item_id: {work_item_id}")

        active_lease = (
            self.session.query(Lease)
            .filter(Lease.work_item_id == work_item_id, Lease.expired_at.is_(None))
            .order_by(Lease.id.desc())
            .with_for_update()
            .first()
        )
        now = _utc_now()

        if work_item.status in {WorkItemStatus.SUCCEEDED, WorkItemStatus.FAILED, WorkItemStatus.REVOKED}:
            if active_lease is None or active_lease.heartbeat_deadline <= now:
                logger.warning(
                    "Work item %d already in terminal status %s, skipping", work_item_id, work_item.status.value
                )
                return
            if active_lease.worker_id != worker_id:
                raise PermissionError("work item lease belongs to another worker")
            logger.warning("Work item %d already in terminal status %s, skipping", work_item_id, work_item.status.value)
            return

        if active_lease is None or active_lease.heartbeat_deadline <= now:
            logger.warning("Work item %d has no active lease for diff application", work_item_id)
            raise ValueError("work item has no active lease")
        if active_lease.worker_id != worker_id:
            raise PermissionError("work item lease belongs to another worker")

        attempt = active_lease.attempt
        if attempt is None:
            logger.warning("Work item %d has no attempt associated with lease %d", work_item_id, active_lease.id)
            raise ValueError("work item has no attempt")

        verdict = verify_attempt(
            repo=work_item.repo,
            touched_paths=touched_paths,
            changed_hunks=changed_hunks,
            allowed_paths=list(work_item.allowed_paths),
            forbidden_paths=list(work_item.forbidden_paths),
            policy_path=self.policy_path,
        )
        attempt.submitted_diff_ref = "inline"
        if verdict.result in {VerdictResult.PASS.value, VerdictResult.APPEND_GOVERNANCE_STEP.value}:
            attempt.result_status = AttemptResultStatus.ACCEPTED
        else:
            attempt.result_status = AttemptResultStatus.REJECTED
        self.session.add(Verdict(attempt=attempt, result=VerdictResult(verdict.result)))
        active_lease.expired_at = now

        if verdict.result == VerdictResult.APPEND_GOVERNANCE_STEP.value:
            work_item.status = WorkItemStatus.SUCCEEDED
            self._append_governance_work_items(work_item, verdict.triggered_lanes)
        elif verdict.result == VerdictResult.PASS.value:
            work_item.status = WorkItemStatus.SUCCEEDED
            self._complete_outcome_from_work_item(
                work_item,
                result=OutcomeResult.SUCCEEDED,
                summary=f"{work_item.work_type} completed successfully",
            )
        else:
            work_item.status = WorkItemStatus.FAILED
            self._complete_outcome_from_work_item(
                work_item,
                result=OutcomeResult.FAILED,
                summary=f"{work_item.work_type} rejected by policy",
            )

        self.session.commit()

    def heartbeat_lease(self, lease_id: int, ttl_seconds: int = 60) -> Lease:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        now = _utc_now()
        lease = self.session.get(Lease, lease_id)
        if lease is None:
            raise ValueError(f"unknown lease_id: {lease_id}")
        if lease.expired_at is not None or lease.heartbeat_deadline <= now:
            logger.warning("Heartbeat rejected: lease %d is expired", lease_id)
            raise ValueError(f"lease {lease_id} is expired")

        deadline = now + timedelta(seconds=ttl_seconds)
        lease.heartbeat_deadline = deadline
        lease.expires_at = deadline
        self.session.commit()
        logger.debug("Lease %d heartbeat extended by %ds", lease_id, ttl_seconds)
        return lease

    def extend_lease(self, lease_id: int, worker_id: str, ttl_seconds: int, reason: str) -> Lease:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not reason.strip():
            raise ValueError("reason must be non-empty")

        now = _utc_now()
        lease = self.session.get(Lease, lease_id)
        if lease is None:
            raise ValueError(f"unknown lease_id: {lease_id}")
        if lease.expired_at is not None or lease.heartbeat_deadline <= now:
            raise ValueError(f"lease {lease_id} is expired")
        if lease.worker_id != worker_id:
            raise PermissionError("lease belongs to another worker")

        lease.heartbeat_deadline = lease.heartbeat_deadline + timedelta(seconds=ttl_seconds)
        lease.expires_at = lease.heartbeat_deadline
        self.session.add(
            OutcomeEvent(
                outcome=lease.work_item.outcome,
                event_type="lease_extended",
                payload={
                    "lease_id": lease.id,
                    "worker_id": worker_id,
                    "ttl_seconds": ttl_seconds,
                    "reason": reason,
                },
            )
        )
        self.session.commit()
        return lease

    def expire_leases(self, now_offset_seconds: int = 0) -> int:
        now = _utc_now() + timedelta(seconds=now_offset_seconds)
        leases = self.session.query(Lease).filter(Lease.expired_at.is_(None), Lease.heartbeat_deadline <= now).all()
        for lease in leases:
            lease.expired_at = lease.heartbeat_deadline
            if lease.work_item.status is WorkItemStatus.LEASED:
                lease.work_item.status = WorkItemStatus.READY
                lease.work_item.outcome.phase = OutcomePhase.READY
                lease.work_item.outcome.current_work_item_id = lease.work_item.id
        self.session.commit()
        logger.info("Expired %d leases", len(leases))
        return len(leases)
