from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.orm import Session

from .models import Case, IntentVersion, PullRequest, Step, StepStatus
from .planner_client import PlannerClient


class PlannerService:
    REQUIRED_PLAN_KEYS = (
        "title",
        "goal",
        "repo",
        "allowed_paths",
        "forbidden_paths",
        "step_type",
    )

    def __init__(self, session: Session, planner_client: PlannerClient):
        self.session = session
        self.planner_client = planner_client

    def plan_pull_request(self, pull_request_id: int, repo_heads: dict[str, str]) -> tuple[Case, Step]:
        pull = self.session.get(PullRequest, pull_request_id)
        if pull is None:
            raise ValueError(f"unknown pull_request_id: {pull_request_id}")

        active_intent = (
            self.session.query(IntentVersion)
            .order_by(IntentVersion.created_at.desc(), IntentVersion.id.desc())
            .first()
        )
        if active_intent is None:
            raise ValueError("no active intent version")

        plan = self.planner_client.synthesize(
            brief=active_intent.brief_text,
            lane=pull.lane,
            repo_heads=repo_heads,
            envelope=pull.envelope,
        )
        plan = self._validate_plan(plan)
        selected_repo = plan["repo"]
        if selected_repo not in repo_heads:
            raise ValueError(f"missing repo head for repo: {selected_repo}")
        if selected_repo not in pull.repo_access_set:
            raise ValueError(f"repo {selected_repo} is not in pull request access set")

        case = Case(
            intent_id=active_intent.intent_id,
            intent_version=active_intent.intent_version,
            title=plan["title"],
            goal=plan["goal"],
        )
        step = Step(
            case=case,
            repo=selected_repo,
            lane=pull.lane,
            step_type=plan["step_type"],
            status=StepStatus.READY,
            allowed_paths=plan["allowed_paths"],
            forbidden_paths=plan["forbidden_paths"],
            base_commit=repo_heads[selected_repo],
        )

        pull.repo_heads = dict(repo_heads)
        pull.planning_result = dict(plan)
        pull.status = "ready"

        self.session.add_all([case, step])
        self.session.commit()
        return case, step

    def _validate_plan(self, plan: dict) -> dict:
        if not isinstance(plan, Mapping):
            raise ValueError("synthesized plan must be a mapping")
        missing_keys = [key for key in self.REQUIRED_PLAN_KEYS if key not in plan]
        if missing_keys:
            raise ValueError(f"synthesized plan missing required keys: {', '.join(missing_keys)}")
        normalized_plan = dict(plan)

        for key in ("title", "goal", "repo", "step_type"):
            if not isinstance(normalized_plan[key], str):
                raise ValueError(f"synthesized plan {key} must be a string")

        for key in ("allowed_paths", "forbidden_paths"):
            value = normalized_plan[key]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"synthesized plan {key} must be a list of strings")

        return normalized_plan
