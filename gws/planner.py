from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Optional

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from .contracts import SynthesizedPlan
from .models import (
    Outcome,
    OutcomePhase,
    PlanningSession,
    PlanningSessionStatus,
    WorkItem,
    WorkItemStatus,
)
from .planner_client import PlannerClient

logger = logging.getLogger(__name__)


class MaterializePlanError(ValueError):
    def __init__(self, message: str, *, plan_payload: Optional[dict] = None):
        super().__init__(message)
        self.plan_payload = dict(plan_payload or {})


class PlannerService:
    def __init__(
        self,
        session: Session,
        planner_client: PlannerClient,
        *,
        lane_capabilities: Optional[dict[str, str]] = None,
    ):
        self.session = session
        self.planner_client = planner_client
        self.lane_capabilities = lane_capabilities

    def _validate_plan(self, plan: SynthesizedPlan | dict) -> SynthesizedPlan:
        try:
            return plan if isinstance(plan, SynthesizedPlan) else SynthesizedPlan.model_validate(plan)
        except ValidationError as exc:
            logger.warning("Plan validation failed: %s", str(exc))
            raise ValueError(f"synthesized plan invalid: {exc}") from exc

    def materialize_plan(self, planning_session_id: int) -> tuple[Outcome, WorkItem]:
        claim_result = self.session.execute(
            update(PlanningSession)
            .where(
                PlanningSession.id == planning_session_id,
                PlanningSession.status == PlanningSessionStatus.PENDING,
            )
            .values(status=PlanningSessionStatus.MATERIALIZING)
        )
        if claim_result.rowcount != 1:
            planning_session = self.session.get(PlanningSession, planning_session_id)
            if planning_session is None:
                raise ValueError(f"unknown planning_session_id: {planning_session_id}")
            raise ValueError(
                "planning session already claimed: "
                f"{planning_session_id} is {planning_session.status.value}"
            )

        planning_session = self.session.get(PlanningSession, planning_session_id)
        if planning_session is None:
            raise ValueError(f"unknown planning_session_id: {planning_session_id}")

        try:
            context = planning_session.planning_context or {}
            plan = self._validate_plan(
                self.planner_client.synthesize(
                    brief=str(context.get("brief", "")),
                    lane=planning_session.lane,
                    repo_heads=dict(planning_session.repo_heads),
                    envelope=dict(context.get("envelope", {})),
                    lane_capabilities=self.lane_capabilities,
                    intent_context=context.get("intent_context") or None,
                    planner_guidance=context.get("planner_guidance") or None,
                )
            )

            planning_session.plan_payload = plan.model_dump()

            selected_repo = plan.repo
            if selected_repo not in planning_session.repo_heads:
                raise MaterializePlanError(
                    f"missing repo head for repo: {selected_repo}",
                    plan_payload=planning_session.plan_payload,
                )
            if selected_repo not in planning_session.available_repos:
                raise MaterializePlanError(
                    f"repo {selected_repo} is not in planning session available repos",
                    plan_payload=planning_session.plan_payload,
                )
        except Exception as exc:
            if self.session.is_active:
                planning_session.status = PlanningSessionStatus.FAILED
                if isinstance(exc, MaterializePlanError) and exc.plan_payload:
                    planning_session.plan_payload = dict(exc.plan_payload)
                planning_session.error_detail = str(exc)
                planning_session.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                self.session.flush()
            raise

        outcome = planning_session.outcome
        outcome.title = plan.title
        outcome.goal = plan.goal
        outcome.phase = OutcomePhase.READY
        outcome.selected_repo = selected_repo

        sequence_index = len(outcome.work_items)
        work_item = WorkItem(
            outcome=outcome,
            sequence_index=sequence_index,
            repo=selected_repo,
            lane=planning_session.lane,
            work_type=plan.work_type,
            status=WorkItemStatus.READY,
            allowed_paths=plan.allowed_paths,
            forbidden_paths=plan.forbidden_paths,
            base_commit=planning_session.repo_heads[selected_repo],
        )
        planning_session.status = PlanningSessionStatus.SUCCEEDED
        planning_session.error_detail = ""
        planning_session.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)

        self.session.add(work_item)
        self.session.flush()
        outcome.current_work_item_id = work_item.id
        return outcome, work_item
