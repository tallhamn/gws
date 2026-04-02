from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .models import (
    IntentVersion,
    Outcome,
    OutcomeEvent,
    OutcomePhase,
    PlanningSession,
    PlanningSessionStatus,
    WorkItem,
)
from .planner import PlannerService
from .planner_client import PlannerClient


class PlanningCoordinator:
    def __init__(
        self,
        session: Session,
        *,
        planner_client: PlannerClient,
        planner_provider: str,
        planner_model: Optional[str],
        lane_capabilities: Optional[dict[str, str]] = None,
    ):
        self.session = session
        self.planner_provider = planner_provider
        self.planner_model = planner_model
        self.planner_service = PlannerService(
            session,
            planner_client=planner_client,
            lane_capabilities=lane_capabilities,
        )

    def plan_outcome(
        self,
        *,
        intent_id: str,
        worker_id: str,
        lane: str,
        available_repos: list[str],
        repo_heads: dict[str, str],
    ) -> tuple[Outcome, WorkItem]:
        intent = (
            self.session.query(IntentVersion)
            .filter(IntentVersion.intent_id == intent_id)
            .order_by(IntentVersion.intent_version.desc())
            .first()
        )
        if intent is None:
            raise ValueError(f"no active intent version for intent_id: {intent_id}")

        outcome = Outcome(
            intent_id=intent.intent_id,
            intent_version=intent.intent_version,
            title="",
            goal="",
            phase=OutcomePhase.PLANNING,
        )
        planning_session = PlanningSession(
            outcome=outcome,
            worker_id=worker_id,
            lane=lane,
            status=PlanningSessionStatus.PENDING,
            planner_provider=self.planner_provider,
            planner_model=self.planner_model,
            available_repos=list(available_repos),
            repo_heads=dict(repo_heads),
            planning_context={
                "brief": intent.brief_text,
                "envelope": {},
                "intent_context": intent.context,
                "planner_guidance": intent.planner_guidance,
            },
        )
        self.session.add_all([outcome, planning_session])
        self.session.flush()
        self.session.add(
            OutcomeEvent(
                outcome=outcome,
                event_type="planning_started",
                payload={
                    "planning_session_id": planning_session.id,
                    "worker_id": worker_id,
                    "lane": lane,
                },
            )
        )
        self.session.commit()
        planning_session_id = planning_session.id

        try:
            outcome, work_item = self.planner_service.materialize_plan(planning_session_id)
            self.session.add(
                OutcomeEvent(
                    outcome=outcome,
                    event_type="planning_succeeded",
                    payload={
                        "planning_session_id": planning_session_id,
                        "work_item_id": work_item.id,
                        "selected_repo": work_item.repo,
                    },
                )
            )
            self.session.commit()
        except Exception as exc:
            planning_session = self.session.get(PlanningSession, planning_session_id)
            if (
                self.session.is_active
                and planning_session is not None
                and planning_session.status is PlanningSessionStatus.FAILED
            ):
                self.session.add(
                    OutcomeEvent(
                        outcome=planning_session.outcome,
                        event_type="planning_failed",
                        payload={
                            "planning_session_id": planning_session_id,
                            "error": str(exc),
                        },
                    )
                )
                self.session.commit()
            else:
                failure_plan_payload = dict(getattr(exc, "plan_payload", {}) or {})
                self.session.rollback()
                planning_session = self.session.get(PlanningSession, planning_session_id)
                if planning_session is not None:
                    planning_session.status = PlanningSessionStatus.FAILED
                    if failure_plan_payload:
                        planning_session.plan_payload = failure_plan_payload
                    planning_session.error_detail = str(exc)
                    planning_session.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    self.session.add(
                        OutcomeEvent(
                            outcome=planning_session.outcome,
                            event_type="planning_failed",
                            payload={
                                "planning_session_id": planning_session_id,
                                "error": str(exc),
                            },
                        )
                    )
                    self.session.commit()
            raise
        return outcome, work_item
