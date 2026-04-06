"""Tests for the agent-mode evaluator layer."""
from __future__ import annotations

import os
import tempfile
from typing import Optional

import pytest

from gws.contracts import EvaluationResult, PlannerResult, SynthesizedPlan
from gws.evaluator import build_evaluator, resolve_repo_path
from gws.models import (
    IntentVersion,
    Outcome,
    OutcomePhase,
    OutcomeResult,
    PlanningSession,
    PlanningSessionStatus,
    WorkItem,
)
from gws.planner import PlannerService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEvaluator:
    def __init__(self, result: EvaluationResult):
        self.result = result
        self.calls: list[dict] = []

    def evaluate(
        self,
        *,
        brief: str,
        repo: str,
        repo_path: str,
        repo_trees: list[str],
        envelope: dict,
        intent_context: Optional[str] = None,
    ) -> EvaluationResult:
        self.calls.append(
            {"brief": brief, "repo": repo, "repo_path": repo_path, "repo_trees": repo_trees}
        )
        return self.result


class FakePlannerClient:
    def __init__(self, plan):
        self.plan = plan
        self.calls: list[dict] = []

    def synthesize(
        self,
        *,
        brief: str,
        lane: str,
        repo_heads: dict[str, str],
        envelope: dict,
        lane_capabilities: Optional[dict] = None,
        intent_context: Optional[str] = None,
        planner_guidance: Optional[str] = None,
        repo_trees: dict[str, list[str]] | None = None,
        evaluation_findings: Optional[str] = None,
    ):
        self.calls.append({"brief": brief, "evaluation_findings": evaluation_findings})
        return self.plan


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    from gws.db import Base, make_session_factory

    factory, engine = make_session_factory("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with factory() as s:
        yield s


def _planning_session(
    session,
    *,
    repo_trees: dict[str, list[str]] | None = None,
    planning_context: dict | None = None,
) -> PlanningSession:
    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="build a snake game"))
    outcome = Outcome(intent_id="intent-1", intent_version=1, title="", goal="", phase=OutcomePhase.PLANNING)
    ctx = planning_context or {
        "brief": "build a snake game",
        "envelope": {},
        "repo_trees": repo_trees or {},
    }
    planning = PlanningSession(
        outcome=outcome,
        worker_id="coder-1",
        lane="coder",
        planner_provider="codex",
        planner_model="test-model",
        available_repos=["studio-test"],
        repo_heads={"studio-test": "abc123"},
        planning_context=ctx,
    )
    session.add_all([outcome, planning])
    session.commit()
    return planning


# ---------------------------------------------------------------------------
# resolve_repo_path
# ---------------------------------------------------------------------------


def test_resolve_repo_path_returns_path_when_dir_exists():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = os.path.join(tmpdir, "studio-test")
        os.makedirs(repo_dir)
        assert resolve_repo_path(tmpdir, "studio-test") == repo_dir


def test_resolve_repo_path_returns_none_when_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        assert resolve_repo_path(tmpdir, "nonexistent") is None


# ---------------------------------------------------------------------------
# Fast-path guards in _evaluate_repo
# ---------------------------------------------------------------------------


def test_evaluator_skips_when_no_evaluator(session):
    """No evaluator configured → evaluation returns None, synthesis proceeds normally."""
    plan = SynthesizedPlan(
        title="Build game", goal="Create snake", repo="studio-test",
        allowed_paths=["**"], forbidden_paths=[], work_type="code",
    )
    planner_client = FakePlannerClient(plan)
    service = PlannerService(session, planner_client=planner_client, evaluator=None)

    ps = _planning_session(session, repo_trees={"studio-test": ["index.html"]})
    result = service.materialize_plan(ps.id)

    assert not isinstance(result, PlannerResult)
    assert planner_client.calls[0]["evaluation_findings"] is None


def test_evaluator_skips_when_repo_trees_empty(session):
    """Empty repo_trees → fast-path skip, evaluator never called."""
    evaluator = FakeEvaluator(EvaluationResult(satisfied=True, findings="looks good", files_examined=[]))
    plan = SynthesizedPlan(
        title="Build game", goal="Create snake", repo="studio-test",
        allowed_paths=["**"], forbidden_paths=[], work_type="code",
    )
    planner_client = FakePlannerClient(plan)

    with tempfile.TemporaryDirectory() as tmpdir:
        service = PlannerService(
            session, planner_client=planner_client,
            evaluator=evaluator, source_repos_root=tmpdir,
        )
        ps = _planning_session(session, repo_trees={})
        result = service.materialize_plan(ps.id)

    assert evaluator.calls == []  # evaluator was NOT called
    assert not isinstance(result, PlannerResult)


def test_evaluator_skips_when_repo_path_missing(session):
    """Repo dir doesn't exist → fast-path skip."""
    evaluator = FakeEvaluator(EvaluationResult(satisfied=True, findings="looks good", files_examined=[]))
    plan = SynthesizedPlan(
        title="Build game", goal="Create snake", repo="studio-test",
        allowed_paths=["**"], forbidden_paths=[], work_type="code",
    )
    planner_client = FakePlannerClient(plan)

    with tempfile.TemporaryDirectory() as tmpdir:
        # No studio-test directory created inside tmpdir
        service = PlannerService(
            session, planner_client=planner_client,
            evaluator=evaluator, source_repos_root=tmpdir,
        )
        ps = _planning_session(session, repo_trees={"studio-test": ["index.html"]})
        result = service.materialize_plan(ps.id)

    assert evaluator.calls == []
    assert not isinstance(result, PlannerResult)


# ---------------------------------------------------------------------------
# Evaluator integration
# ---------------------------------------------------------------------------


def test_evaluator_satisfied_returns_planner_result_satisfied(session):
    """Evaluator says satisfied + repo has files → SATISFIED returned."""
    evaluator = FakeEvaluator(EvaluationResult(
        satisfied=True, findings="Complete snake game found", files_examined=["index.html"],
    ))
    planner_client = FakePlannerClient(PlannerResult.SATISFIED)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "studio-test"))
        service = PlannerService(
            session, planner_client=planner_client,
            evaluator=evaluator, source_repos_root=tmpdir,
        )
        ps = _planning_session(session, repo_trees={"studio-test": ["index.html", "game.js"]})
        result = service.materialize_plan(ps.id)

    assert result is PlannerResult.SATISFIED
    assert planner_client.calls == []  # synthesis was skipped

    session.expunge_all()
    stored = session.get(PlanningSession, ps.id)
    assert stored.plan_payload["evaluation_findings"] == "Complete snake game found"
    assert stored.outcome.result_summary == "Intent already satisfied (evaluator)"


def test_evaluation_findings_passed_to_synthesize(session):
    """Evaluator says NOT satisfied → findings passed to synthesis."""
    evaluator = FakeEvaluator(EvaluationResult(
        satisfied=False, findings="Only has stub index.html, no game logic",
        files_examined=["index.html"],
    ))
    plan = SynthesizedPlan(
        title="Add game logic", goal="Implement snake movement", repo="studio-test",
        allowed_paths=["**"], forbidden_paths=[], work_type="code",
    )
    planner_client = FakePlannerClient(plan)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "studio-test"))
        service = PlannerService(
            session, planner_client=planner_client,
            evaluator=evaluator, source_repos_root=tmpdir,
        )
        ps = _planning_session(session, repo_trees={"studio-test": ["index.html"]})
        result = service.materialize_plan(ps.id)

    assert not isinstance(result, PlannerResult)
    assert planner_client.calls[0]["evaluation_findings"] == "Only has stub index.html, no game logic"


def test_evaluator_failure_falls_back_to_synthesis(session):
    """If evaluator raises, fall back to synthesis without findings."""

    class FailingEvaluator:
        def evaluate(self, **kwargs):
            raise RuntimeError("Codex crashed")

    plan = SynthesizedPlan(
        title="Build game", goal="Create snake", repo="studio-test",
        allowed_paths=["**"], forbidden_paths=[], work_type="code",
    )
    planner_client = FakePlannerClient(plan)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "studio-test"))
        service = PlannerService(
            session, planner_client=planner_client,
            evaluator=FailingEvaluator(), source_repos_root=tmpdir,
        )
        ps = _planning_session(session, repo_trees={"studio-test": ["index.html"]})
        result = service.materialize_plan(ps.id)

    assert not isinstance(result, PlannerResult)
    assert planner_client.calls[0]["evaluation_findings"] is None


# ---------------------------------------------------------------------------
# parse_evaluation_output
# ---------------------------------------------------------------------------


def test_parse_evaluation_output_valid_json():
    from gws.providers.common import parse_evaluation_output

    result = parse_evaluation_output('{"satisfied": true, "findings": "all good", "files_examined": ["a.js"]}')
    assert result.satisfied is True
    assert result.findings == "all good"
    assert result.files_examined == ["a.js"]


def test_parse_evaluation_output_empty_string():
    from gws.providers.common import parse_evaluation_output

    result = parse_evaluation_output("")
    assert result.satisfied is False
    assert "no output" in result.findings


def test_parse_evaluation_output_non_json_fallback():
    from gws.providers.common import parse_evaluation_output

    result = parse_evaluation_output("The repo looks incomplete, missing game logic")
    assert result.satisfied is False
    assert "missing game logic" in result.findings
