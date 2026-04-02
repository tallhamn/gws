from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Optional

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from .contracts import SynthesizedPlan
from .models import (
    Case,
    IntentVersion,
    Outcome,
    OutcomePhase,
    PlanningSession,
    PlanningSessionStatus,
    PullRequest,
    Step,
    StepStatus,
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

    def plan_pull_request(self, pull_request_id: int, repo_heads: dict[str, str]) -> tuple[Case, Step]:
        pull = self.session.get(PullRequest, pull_request_id)
        if pull is None:
            raise ValueError(f"unknown pull_request_id: {pull_request_id}")

        active_intent = (
            self.session.query(IntentVersion)
            .filter(IntentVersion.intent_id == pull.intent_id)
            .order_by(IntentVersion.intent_version.desc())
            .first()
        )
        if active_intent is None:
            raise ValueError(f"no active intent version for intent_id: {pull.intent_id}")

        logger.info(
            "Planning pull request %d against intent %s v%d",
            pull_request_id,
            active_intent.intent_id,
            active_intent.intent_version,
        )

        plan = self._validate_plan(
            self.planner_client.synthesize(
                brief=active_intent.brief_text,
                lane=pull.lane,
                repo_heads=repo_heads,
                envelope=pull.envelope,
                lane_capabilities=self.lane_capabilities,
                intent_context=active_intent.context or None,
                planner_guidance=active_intent.planner_guidance or None,
            )
        )

        selected_repo = plan.repo
        if selected_repo not in repo_heads:
            raise ValueError(f"missing repo head for repo: {selected_repo}")
        if selected_repo not in pull.repo_access_set:
            raise ValueError(f"repo {selected_repo} is not in pull request access set")

        case = Case(
            intent_id=active_intent.intent_id,
            intent_version=active_intent.intent_version,
            title=plan.title,
            goal=plan.goal,
        )
        step = Step(
            case=case,
            repo=selected_repo,
            lane=pull.lane,
            step_type=plan.step_type,
            status=StepStatus.READY,
            allowed_paths=plan.allowed_paths,
            forbidden_paths=plan.forbidden_paths,
            base_commit=repo_heads[selected_repo],
        )

        pull.repo_heads = dict(repo_heads)
        pull.planning_result = plan.model_dump()
        pull.status = "ready"

        self.session.add_all([case, step])
        self.session.commit()
        return case, step

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
            work_type=plan.step_type,
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
