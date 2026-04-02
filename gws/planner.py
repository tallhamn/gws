from __future__ import annotations

import logging
from typing import Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from .contracts import SynthesizedPlan
from .models import Case, IntentVersion, PullRequest, Step, StepStatus
from .planner_client import PlannerClient

logger = logging.getLogger(__name__)


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

        logger.info("Planning pull request %d against intent %s v%d", pull_request_id, active_intent.intent_id, active_intent.intent_version)

        plan = self.planner_client.synthesize(
            brief=active_intent.brief_text,
            lane=pull.lane,
            repo_heads=repo_heads,
            envelope=pull.envelope,
            lane_capabilities=self.lane_capabilities,
            intent_context=active_intent.context or None,
            planner_guidance=active_intent.planner_guidance or None,
        )
        try:
            plan = plan if isinstance(plan, SynthesizedPlan) else SynthesizedPlan.model_validate(plan)
        except ValidationError as exc:
            logger.warning("Plan validation failed: %s", str(exc))
            raise ValueError(f"synthesized plan invalid: {exc}") from exc
        selected_repo = plan.repo
        if selected_repo not in repo_heads:
            raise ValueError(f"missing repo head for repo: {selected_repo}")
        if selected_repo not in pull.repo_access_set:
            raise ValueError(f"repo {selected_repo} is not in pull request access set")

        logger.info("Plan: repo=%s, step_type=%s, title=%s", plan.repo, plan.step_type, plan.title)

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
