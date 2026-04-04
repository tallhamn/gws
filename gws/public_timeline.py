from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, selectinload

from .models import (
    Attempt,
    IntentVersion,
    Lease,
    Outcome,
    OutcomeEvent,
    OutcomePhase,
    OutcomeResult,
    WorkItem,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _brief_teaser(brief_text: str) -> str:
    heading_pattern = re.compile(r"^#{1,6}\s+")
    list_item_pattern = re.compile(r"^(?:[-+*]|\d+[.)])\s+(.*\S.*)$")
    checklist_pattern = re.compile(r"^\[(?: |x|X)\]\s*(.*\S.*)$")

    def _list_item_content(readable_line: str) -> str | None:
        list_item_match = list_item_pattern.match(readable_line)
        if list_item_match is None:
            return None
        content = list_item_match.group(1).strip()
        if not content:
            return None
        checklist_match = checklist_pattern.match(content)
        if checklist_match is not None:
            content = checklist_match.group(1).strip()
        return content or None

    fallback = ""
    for line in brief_text.splitlines():
        readable_line = line.strip()
        if not readable_line:
            continue
        if heading_pattern.match(readable_line):
            continue
        list_item_content = _list_item_content(readable_line)
        if list_item_content is not None:
            if not fallback:
                fallback = list_item_content
            continue
        return readable_line

    return fallback


def _active_work_item_and_lease(outcome: Outcome, now: datetime) -> tuple[WorkItem | None, Lease | None]:
    active_candidates: list[tuple[datetime, WorkItem, Lease]] = []
    for work_item in outcome.work_items:
        for lease in sorted(work_item.leases, key=lambda item: item.id, reverse=True):
            if lease.expired_at is None and lease.heartbeat_deadline > now:
                active_candidates.append((lease.issued_at, work_item, lease))
                break
    if not active_candidates:
        return None, None
    _, work_item, lease = max(active_candidates, key=lambda item: item[0])
    return work_item, lease


def _latest_attempt(outcome: Outcome) -> Attempt | None:
    attempts = [attempt for work_item in outcome.work_items for attempt in work_item.attempts]
    return max(attempts, key=lambda item: item.created_at, default=None)


def _latest_event(outcome: Outcome, *event_types: str) -> OutcomeEvent | None:
    matching_events = [event for event in outcome.events if event.event_type in event_types]
    return max(matching_events, key=lambda item: item.created_at, default=None)


def _event_worker_id(outcome: Outcome) -> str:
    latest_attempt = _latest_attempt(outcome)
    if latest_attempt is not None:
        return latest_attempt.worker_id
    planning_started = _latest_event(outcome, "planning_started")
    if planning_started is not None:
        return str(planning_started.payload.get("worker_id", ""))
    return ""


def _completed_outcome_label(result: OutcomeResult | None) -> str:
    if result is OutcomeResult.SUCCEEDED:
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

    outcomes = (
        session.query(Outcome)
        .options(
            selectinload(Outcome.events),
            selectinload(Outcome.work_items).selectinload(WorkItem.leases),
            selectinload(Outcome.work_items).selectinload(WorkItem.attempts),
        )
        .filter(Outcome.intent_id == intent.intent_id)
        .order_by(Outcome.created_at, Outcome.id)
        .all()
    )

    now = _utc_now()
    timeline_rows: list[tuple[datetime, dict[str, Any]]] = []
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

    for outcome in outcomes:
        active_work_item, active_lease = _active_work_item_and_lease(outcome, now)
        if active_work_item is not None and active_lease is not None:
            candidate = {
                "title": outcome.title,
                "summary": outcome.goal,
                "worker_id": active_lease.worker_id,
                "lane": active_work_item.lane,
                "lease_status": "live",
                "lease_time_remaining_seconds": max(0, int((active_lease.heartbeat_deadline - now).total_seconds())),
                "started_at": _as_utc_iso(active_lease.issued_at),
            }
            if latest_active_now_building is None or active_lease.issued_at > latest_active_now_building[0]:
                latest_active_now_building = (active_lease.issued_at, candidate)
            timeline_rows.append(
                (
                    active_lease.issued_at,
                    {
                        "title": outcome.title,
                        "what_was_built": outcome.goal,
                        "outcome": "live",
                        "worker_id": active_lease.worker_id,
                        "occurred_at": _as_utc_iso(active_lease.issued_at),
                        "outcome_id": outcome.id,
                        "work_item_id": active_work_item.id,
                    },
                )
            )
            continue

        if outcome.phase is OutcomePhase.COMPLETED:
            completed_event = _latest_event(outcome, "outcome_completed")
            occurred_at = (
                completed_event.created_at
                if completed_event is not None
                else outcome.completed_at or outcome.created_at
            )
            timeline_rows.append(
                (
                    occurred_at,
                    {
                        "title": outcome.title,
                        "what_was_built": outcome.result_summary or outcome.goal,
                        "outcome": _completed_outcome_label(outcome.result),
                        "worker_id": _event_worker_id(outcome),
                        "occurred_at": _as_utc_iso(occurred_at),
                        "outcome_id": outcome.id,
                        "work_item_id": outcome.current_work_item_id,
                    },
                )
            )
            continue

        planning_failed = _latest_event(outcome, "planning_failed")
        if planning_failed is not None:
            timeline_rows.append(
                (
                    planning_failed.created_at,
                    {
                        "title": outcome.title or intent.intent_id,
                        "what_was_built": outcome.goal or str(planning_failed.payload.get("error", "")),
                        "outcome": "failed",
                        "worker_id": str(planning_failed.payload.get("worker_id", "")) or _event_worker_id(outcome),
                        "occurred_at": _as_utc_iso(planning_failed.created_at),
                        "outcome_id": outcome.id,
                        "work_item_id": outcome.current_work_item_id,
                    },
                )
            )

    if latest_active_now_building is not None:
        now_building = latest_active_now_building[1]

    timeline_events = [
        {
            "sequence_label": "1. Concept brief locked",
            "title": "Concept brief locked",
            "what_was_built": "Drop brief accepted and pushed into GWS.",
            "brief_teaser": _brief_teaser(intent.brief_text or ""),
            "brief_text": intent.brief_text or "",
            "outcome": "succeeded",
            "worker_id": "",
            "occurred_at": _as_utc_iso(intent.created_at),
        }
    ]
    for index, (_, event) in enumerate(sorted(timeline_rows, key=lambda item: item[0]), start=2):
        timeline_events.append({"sequence_label": str(index), **event})

    return {
        "intent": {
            "intent_id": intent.intent_id,
            "intent_version": intent.intent_version,
            "title": outcomes[0].title if outcomes else intent.intent_id,
            "brief_summary": _brief_teaser(intent.brief_text or ""),
        },
        "outcome_progress": {
            "total_outcomes": len(outcomes),
            "completed_outcomes": sum(1 for outcome in outcomes if outcome.phase is OutcomePhase.COMPLETED),
            "active_outcomes": sum(
                1
                for outcome in outcomes
                if outcome.phase in {OutcomePhase.PLANNING, OutcomePhase.READY, OutcomePhase.RUNNING}
            ),
        },
        "now_building": now_building,
        "timeline_events": timeline_events,
    }
