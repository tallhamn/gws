from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, selectinload

from .models import (
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _sort_timestamp(*values: datetime | None) -> str:
    candidates = [value for value in values if value is not None]
    if not candidates:
        return ""
    return _as_utc_iso(max(candidates))


def _active_lease(step: Step, now: datetime) -> Lease | None:
    for lease in sorted(step.leases, key=lambda item: item.id, reverse=True):
        if lease.expired_at is None and lease.heartbeat_deadline > now:
            return lease
    return None


def _latest_attempt(step: Step) -> Attempt | None:
    return max(step.attempts, key=lambda item: item.id, default=None)


def _latest_verdict(step: Step) -> Verdict | None:
    verdicts = [verdict for attempt in step.attempts for verdict in attempt.verdicts]
    return max(verdicts, key=lambda item: item.id, default=None)


def _outcome_for_step(
    step: Step,
    active_lease: Lease | None,
    latest_attempt: Attempt | None,
    latest_verdict: Verdict | None,
) -> str:
    if active_lease is not None:
        return "live"
    if latest_verdict is not None and latest_verdict.result == VerdictResult.PASS:
        return "succeeded"
    if latest_attempt is not None:
        if latest_attempt.result_status is AttemptResultStatus.ACCEPTED:
            return "succeeded"
        if latest_attempt.result_status in {
            AttemptResultStatus.PENDING,
            AttemptResultStatus.SUBMITTED,
            AttemptResultStatus.REJECTED,
        }:
            return "failed"
    if step.status is StepStatus.SUCCEEDED:
        return "succeeded"
    return "failed"


def build_public_timeline(session: Session, intent_id: str) -> dict[str, Any] | None:
    intent = (
        session.query(IntentVersion)
        .filter(IntentVersion.intent_id == intent_id)
        .order_by(IntentVersion.intent_version.desc())
        .first()
    )
    if intent is None:
        return None

    cases = (
        session.query(Case)
        .options(
            selectinload(Case.steps).selectinload(Step.leases),
            selectinload(Case.steps).selectinload(Step.attempts).selectinload(Attempt.verdicts),
        )
        .filter(Case.intent_id == intent.intent_id, Case.intent_version == intent.intent_version)
        .order_by(Case.id)
        .all()
    )

    now = _utc_now()
    events: list[dict[str, Any]] = [
        {
            "sequence_label": "1. Concept brief locked",
            "title": "Concept brief locked",
            "what_was_built": "Drop brief accepted and pushed into GWS.",
            "outcome": "succeeded",
            "worker_id": "",
            "occurred_at": _as_utc_iso(intent.created_at),
        }
    ]
    now_building = {
        "title": "",
        "summary": "",
        "worker_id": "",
        "lane": "",
        "lease_status": "idle",
        "lease_time_remaining_seconds": 0,
        "started_at": "",
    }
    latest_active_now_building: tuple[datetime, dict[str, Any]] | None = None

    sequence_number = 2
    for case in cases:
        for step in sorted(case.steps, key=lambda item: item.id):
            active_lease = _active_lease(step, now)
            latest_attempt = _latest_attempt(step)
            latest_verdict = _latest_verdict(step)
            outcome = _outcome_for_step(step, active_lease, latest_attempt, latest_verdict)
            occurred_at = _sort_timestamp(
                getattr(active_lease, "issued_at", None),
                getattr(latest_verdict, "created_at", None),
                getattr(latest_attempt, "created_at", None),
            )

            if outcome == "live" and active_lease is not None:
                candidate = {
                    "title": case.title,
                    "summary": case.goal,
                    "worker_id": active_lease.worker_id,
                    "lane": step.lane,
                    "lease_status": "live",
                    "lease_time_remaining_seconds": max(
                        0, int((active_lease.heartbeat_deadline - now).total_seconds())
                    ),
                    "started_at": _as_utc_iso(active_lease.issued_at),
                }
                if latest_active_now_building is None or active_lease.issued_at > latest_active_now_building[0]:
                    latest_active_now_building = (active_lease.issued_at, candidate)

            events.append(
                {
                    "sequence_label": str(sequence_number),
                    "step_id": step.id,
                    "title": case.title,
                    "what_was_built": case.goal,
                    "outcome": outcome,
                    "worker_id": active_lease.worker_id if active_lease is not None else getattr(latest_attempt, "worker_id", ""),
                    "occurred_at": occurred_at,
                }
            )
            sequence_number += 1

    if latest_active_now_building is not None:
        now_building = latest_active_now_building[1]

    return {
        "intent": {
            "intent_id": intent.intent_id,
            "intent_version": intent.intent_version,
            "title": cases[0].title if cases else intent.intent_id,
            "brief_summary": intent.brief_text.splitlines()[0] if intent.brief_text else "",
        },
        "case_progress": {
            "total_cases": len(cases),
            "completed_cases": sum(
                1
                for case in cases
                if case.steps and all(step.status is StepStatus.SUCCEEDED for step in case.steps)
            ),
            "active_cases": sum(
                1
                for case in cases
                if any(step.status in {StepStatus.READY, StepStatus.LEASED, StepStatus.RUNNING, StepStatus.VERIFYING} for step in case.steps)
            ),
        },
        "now_building": now_building,
        "timeline_events": events,
    }
