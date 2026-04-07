"""End-to-end test: governance routing blocks merge for sensitive paths.

Verifies the full lifecycle:
1. Push intent → lease work item → submit completion
2. If touched paths trigger policy (auth/**, payments/**) → verdict is append_governance_step
3. Governance work item created in security-review lane
4. Original work item should NOT be marked as final until governance completes
"""

from __future__ import annotations

import pytest

from gws.contracts import SynthesizedPlan
from gws.control_plane import ControlPlaneService
from gws.coordinator import PlanningCoordinator
from gws.db import Base, make_session_factory
from gws.models import (
    IntentVersion,
    OutcomePhase,
    Verdict,
    VerdictResult,
    WorkItem,
    WorkItemStatus,
)


class FakePlannerClient:
    def synthesize(self, *, brief, lane, repo_heads, envelope, **kwargs):
        return SynthesizedPlan(
            title="Update auth flow",
            goal="Add OAuth2 support",
            repo=next(iter(repo_heads)),
            allowed_paths=["src/**", "auth/**"],
            forbidden_paths=[],
            work_type="code",
        )


@pytest.fixture()
def db():
    factory, engine = make_session_factory("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with factory() as session:
        yield session, factory


def test_governance_triggered_for_auth_paths(db):
    """Touching auth/** triggers security-review governance lane."""
    session, factory = db

    # 1. Push intent
    session.add(IntentVersion(intent_id="test-gov", intent_version=1, brief_text="Add OAuth2 login"))
    session.commit()

    # 2. Plan and lease a work item
    coordinator = PlanningCoordinator(
        session,
        planner_client=FakePlannerClient(),
        planner_provider="fake",
        planner_model=None,
    )
    result = coordinator.plan_outcome(
        intent_id="test-gov",
        worker_id="coder-1",
        lane="coder",
        available_repos=["test-repo"],
        repo_heads={"test-repo": "abc123"},
    )
    assert result is not None
    outcome, work_item = result

    # Lease it
    service = ControlPlaneService(session, policy_path="policy.yaml")
    service.issue_lease(work_item_id=work_item.id, worker_id="coder-1", ttl_seconds=60)

    # 3. Complete with paths that touch auth/**
    service.apply_attempt_completion(
        work_item_id=work_item.id,
        worker_id="coder-1",
        touched_paths=["auth/oauth2.py", "auth/tokens.py", "src/login.py"],
        changed_hunks=["+import oauth2"],
    )
    session.flush()

    # 4. Verify: governance triggered
    verdict = session.query(Verdict).order_by(Verdict.id.desc()).first()
    assert verdict is not None
    assert verdict.result == VerdictResult.APPEND_GOVERNANCE_STEP

    # 5. Verify: work item succeeded but outcome NOT completed (governance pending)
    session.refresh(work_item)
    assert work_item.status == WorkItemStatus.SUCCEEDED

    # Check governance work item was created in security-review lane
    governance_items = (
        session.query(WorkItem).filter(WorkItem.outcome_id == outcome.id, WorkItem.lane == "security-review").all()
    )
    assert len(governance_items) >= 1, "Expected security-review governance work item"

    # Outcome should still be running (not completed) — governance hasn't finished
    session.refresh(outcome)
    assert outcome.phase != OutcomePhase.COMPLETED, (
        "Outcome should not be completed until governance work item finishes"
    )


def test_clean_paths_pass_without_governance(db):
    """Touching only allowed non-sensitive paths → pass, no governance."""
    session, factory = db

    session.add(IntentVersion(intent_id="test-clean", intent_version=1, brief_text="Update game UI"))
    session.commit()

    coordinator = PlanningCoordinator(
        session,
        planner_client=FakePlannerClient(),
        planner_provider="fake",
        planner_model=None,
    )
    result = coordinator.plan_outcome(
        intent_id="test-clean",
        worker_id="coder-1",
        lane="coder",
        available_repos=["test-repo"],
        repo_heads={"test-repo": "abc123"},
    )
    outcome, work_item = result

    service = ControlPlaneService(session, policy_path="policy.yaml")
    service.issue_lease(work_item_id=work_item.id, worker_id="coder-1", ttl_seconds=60)

    service.apply_attempt_completion(
        work_item_id=work_item.id,
        worker_id="coder-1",
        touched_paths=["src/ui/menu.js", "src/ui/hud.js"],
        changed_hunks=["+const menu = {}"],
    )
    session.flush()

    verdict = session.query(Verdict).order_by(Verdict.id.desc()).first()
    assert verdict is not None
    assert verdict.result == VerdictResult.PASS

    session.refresh(work_item)
    assert work_item.status == WorkItemStatus.SUCCEEDED

    session.refresh(outcome)
    assert outcome.phase == OutcomePhase.COMPLETED
    assert outcome.result.value == "succeeded"


def test_forbidden_paths_rejected(db):
    """Touching forbidden paths → fail_and_replan, work item failed."""
    session, factory = db

    session.add(IntentVersion(intent_id="test-forbid", intent_version=1, brief_text="Fix a bug"))
    session.commit()

    # Plan with specific forbidden paths
    class RestrictedPlanner:
        def synthesize(self, *, brief, lane, repo_heads, envelope, **kwargs):
            return SynthesizedPlan(
                title="Fix bug",
                goal="Patch the issue",
                repo=next(iter(repo_heads)),
                allowed_paths=["src/**"],
                forbidden_paths=["config/**"],
                work_type="code",
            )

    coordinator = PlanningCoordinator(
        session,
        planner_client=RestrictedPlanner(),
        planner_provider="fake",
        planner_model=None,
    )
    result = coordinator.plan_outcome(
        intent_id="test-forbid",
        worker_id="coder-1",
        lane="coder",
        available_repos=["test-repo"],
        repo_heads={"test-repo": "abc123"},
    )
    outcome, work_item = result

    service = ControlPlaneService(session, policy_path="policy.yaml")
    service.issue_lease(work_item_id=work_item.id, worker_id="coder-1", ttl_seconds=60)

    service.apply_attempt_completion(
        work_item_id=work_item.id,
        worker_id="coder-1",
        touched_paths=["src/fix.py", "config/secrets.yaml"],
        changed_hunks=["+secret=abc"],
    )
    session.flush()

    verdict = session.query(Verdict).order_by(Verdict.id.desc()).first()
    assert verdict.result == VerdictResult.FAIL_AND_REPLAN

    session.refresh(work_item)
    assert work_item.status == WorkItemStatus.FAILED

    session.refresh(outcome)
    assert outcome.phase == OutcomePhase.COMPLETED
    assert outcome.result.value == "failed"
