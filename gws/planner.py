from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from .contracts import PlannerResult, SynthesizedPlan
from .models import (
    IntentStatus,
    IntentVersion,
    Outcome,
    OutcomePhase,
    OutcomeResult,
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

    @staticmethod
    def _mark_outcome_failed(outcome: Outcome, message: str, *, completed_at: datetime) -> None:
        outcome.phase = OutcomePhase.COMPLETED
        outcome.result = OutcomeResult.FAILED
        outcome.result_summary = str(message)
        outcome.completed_at = completed_at
        outcome.current_work_item_id = None

    @staticmethod
    def _normalize_selected_repo(selected_repo: str, repo_heads: dict[str, str]) -> str:
        repo = str(selected_repo or "").strip()
        if repo in repo_heads or not repo or len(repo_heads) != 1:
            return repo
        # Local planner models sometimes return a file path or repo+hash instead of the only repo id we exposed.
        return next(iter(repo_heads))

    @staticmethod
    def _normalize_task_text(text: str) -> str:
        lowered = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())
        return re.sub(r"\s+", " ", lowered).strip()

    @classmethod
    def _task_texts_equivalent(cls, left: str, right: str) -> bool:
        normalized_left = cls._normalize_task_text(left)
        normalized_right = cls._normalize_task_text(right)
        if not normalized_left or not normalized_right:
            return False
        if normalized_left == normalized_right:
            return True
        left_tokens = set(normalized_left.split())
        right_tokens = set(normalized_right.split())
        if min(len(left_tokens), len(right_tokens)) < 4:
            return False
        overlap = len(left_tokens & right_tokens)
        return overlap / max(1, min(len(left_tokens), len(right_tokens))) >= 0.8

    def _find_duplicate_outcome(
        self,
        *,
        planning_session: PlanningSession,
        selected_repo: str,
        plan: SynthesizedPlan,
    ) -> Outcome | None:
        duplicate_candidates = (
            self.session.query(Outcome)
            .filter(
                Outcome.intent_id == planning_session.outcome.intent_id,
                Outcome.intent_version == planning_session.outcome.intent_version,
                Outcome.id != planning_session.outcome.id,
                Outcome.selected_repo == selected_repo,
            )
            .order_by(Outcome.created_at.desc(), Outcome.id.desc())
            .all()
        )
        for existing in duplicate_candidates:
            if existing.phase in {OutcomePhase.READY, OutcomePhase.RUNNING}:
                pass
            elif existing.phase is OutcomePhase.COMPLETED and existing.result is OutcomeResult.SUCCEEDED:
                pass
            else:
                continue
            if self._task_texts_equivalent(existing.title, plan.title):
                return existing
            if self._task_texts_equivalent(existing.goal, plan.goal):
                return existing
        return None

    def materialize_plan(self, planning_session_id: int) -> tuple[Outcome, WorkItem] | PlannerResult:
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
                f"planning session already claimed: {planning_session_id} is {planning_session.status.value}"
            )

        planning_session = self.session.get(PlanningSession, planning_session_id)
        if planning_session is None:
            raise ValueError(f"unknown planning_session_id: {planning_session_id}")

        try:
            context = planning_session.planning_context or {}
            raw_result = self.planner_client.synthesize(
                brief=str(context.get("brief", "")),
                lane=planning_session.lane,
                repo_heads=dict(planning_session.repo_heads),
                envelope=dict(context.get("envelope", {})),
                lane_capabilities=self.lane_capabilities,
                intent_context=context.get("intent_context") or None,
                planner_guidance=context.get("planner_guidance") or None,
            )

            if isinstance(raw_result, PlannerResult):
                planning_session.status = PlanningSessionStatus.SUCCEEDED
                planning_session.plan_payload = {"result": raw_result.value}
                planning_session.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                if raw_result is PlannerResult.SATISFIED:
                    intent = (
                        self.session.query(IntentVersion)
                        .filter(
                            IntentVersion.intent_id == planning_session.outcome.intent_id,
                            IntentVersion.intent_version == planning_session.outcome.intent_version,
                        )
                        .one()
                    )
                    intent.status = IntentStatus.SATISFIED

                    planning_session.outcome.phase = OutcomePhase.COMPLETED
                    planning_session.outcome.result = OutcomeResult.ABANDONED
                    planning_session.outcome.result_summary = "Intent already satisfied"
                    planning_session.outcome.completed_at = planning_session.completed_at
                self.session.flush()
                return raw_result

            plan = self._validate_plan(raw_result)

            planning_session.plan_payload = plan.model_dump()

            selected_repo = self._normalize_selected_repo(plan.repo, planning_session.repo_heads)
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
            duplicate_outcome = self._find_duplicate_outcome(
                planning_session=planning_session,
                selected_repo=selected_repo,
                plan=plan,
            )
            if duplicate_outcome is not None:
                duplicate_completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                planning_session.status = PlanningSessionStatus.SUCCEEDED
                planning_session.error_detail = ""
                planning_session.completed_at = duplicate_completed_at
                planning_session.plan_payload = {
                    **planning_session.plan_payload,
                    "result": PlannerResult.DUPLICATE.value,
                    "duplicate_outcome_id": duplicate_outcome.id,
                }
                planning_session.outcome.phase = OutcomePhase.COMPLETED
                planning_session.outcome.result = OutcomeResult.ABANDONED
                planning_session.outcome.result_summary = f"Duplicate of outcome {duplicate_outcome.id}"
                planning_session.outcome.completed_at = duplicate_completed_at
                self.session.flush()
                return PlannerResult.DUPLICATE
        except Exception as exc:
            if self.session.is_active:
                failed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                planning_session.status = PlanningSessionStatus.FAILED
                if isinstance(exc, MaterializePlanError) and exc.plan_payload:
                    planning_session.plan_payload = dict(exc.plan_payload)
                planning_session.error_detail = str(exc)
                planning_session.completed_at = failed_at
                self._mark_outcome_failed(planning_session.outcome, str(exc), completed_at=failed_at)
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
            description=plan.description,
            status=WorkItemStatus.READY,
            allowed_paths=plan.allowed_paths,
            forbidden_paths=plan.forbidden_paths,
            base_commit=planning_session.repo_heads[selected_repo],
            target_branch=context.get("target_branch"),
        )
        planning_session.status = PlanningSessionStatus.SUCCEEDED
        planning_session.error_detail = ""
        planning_session.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)

        self.session.add(work_item)
        self.session.flush()
        outcome.current_work_item_id = work_item.id
        return outcome, work_item
