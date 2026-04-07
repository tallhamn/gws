"""End-to-end test: planner sees repo files and returns SATISFIED for a met intent.

Sets up a real (temp) repo directory, pushes an intent, leases work, and verifies
the evaluator fires and the planner correctly identifies satisfaction.
"""

from __future__ import annotations

import os
import tempfile
from typing import Optional

import pytest

from gws.contracts import EvaluationResult, SynthesizedPlan
from gws.coordinator import PlanningCoordinator
from gws.db import Base, make_session_factory
from gws.models import IntentVersion


class FakePlannerClient:
    """Returns a plan or SATISFIED based on evaluation_findings."""

    def __init__(self):
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
        self.calls.append({"brief": brief, "evaluation_findings": evaluation_findings, "repo_trees": repo_trees})
        # If evaluator found the intent is satisfied, the planner should agree
        # (In real usage, the evaluator short-circuits before synthesize is called)
        return SynthesizedPlan(
            title="Do the work",
            goal="Build it",
            repo=next(iter(repo_heads)),
            allowed_paths=["**"],
            forbidden_paths=[],
            work_type="code",
        )


class FakeEvaluator:
    """Evaluator that actually checks if a file exists in the repo."""

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
        # Check if the repo has an index.html — our "hello world" marker
        index_path = os.path.join(repo_path, "index.html")
        if os.path.isfile(index_path):
            content = open(index_path).read()
            if "hello world" in content.lower():
                return EvaluationResult(
                    satisfied=True,
                    findings="Found index.html containing hello world — intent is met.",
                    files_examined=["index.html"],
                )
        return EvaluationResult(
            satisfied=False,
            findings="No hello world implementation found.",
            files_examined=[],
        )


@pytest.fixture()
def db_session():
    factory, engine = make_session_factory("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with factory() as s:
        yield s


def test_evaluator_detects_satisfied_intent_from_repo_files(db_session):
    """End-to-end: real temp repo with hello world → evaluator says satisfied."""
    with tempfile.TemporaryDirectory() as source_repos_root:
        # Create a fake repo with hello world
        repo_dir = os.path.join(source_repos_root, "test-repo")
        os.makedirs(repo_dir)
        with open(os.path.join(repo_dir, "index.html"), "w") as f:
            f.write("<html><body><h1>Hello World</h1></body></html>")

        # Push intent: "make a hello world page"
        db_session.add(
            IntentVersion(
                intent_id="test-intent",
                intent_version=1,
                brief_text="Create an index.html page that says Hello World",
            )
        )
        db_session.commit()

        # Wire up coordinator with real evaluator and fake planner
        planner_client = FakePlannerClient()
        evaluator = FakeEvaluator()

        coordinator = PlanningCoordinator(
            db_session,
            planner_client=planner_client,
            planner_provider="fake",
            planner_model=None,
            evaluator=evaluator,
            source_repos_root=source_repos_root,
        )

        # Request work — evaluator should see the file and say satisfied
        result = coordinator.plan_outcome(
            intent_id="test-intent",
            worker_id="coder-1",
            lane="coder",
            available_repos=["test-repo"],
            repo_heads={"test-repo": "abc123"},
            repo_trees={},  # Empty! But evaluator checks mount directly
        )

        # Should return None (no work needed — intent satisfied)
        assert result is None

        # Planner should NOT have been called — evaluator short-circuited
        assert planner_client.calls == []


def test_evaluator_returns_findings_when_intent_not_met(db_session):
    """End-to-end: repo exists but doesn't satisfy intent → findings passed to planner."""
    with tempfile.TemporaryDirectory() as source_repos_root:
        # Create a repo with wrong content
        repo_dir = os.path.join(source_repos_root, "test-repo")
        os.makedirs(repo_dir)
        with open(os.path.join(repo_dir, "index.html"), "w") as f:
            f.write("<html><body><h1>Not what we wanted</h1></body></html>")

        db_session.add(
            IntentVersion(
                intent_id="test-intent",
                intent_version=1,
                brief_text="Create an index.html page that says Hello World",
            )
        )
        db_session.commit()

        planner_client = FakePlannerClient()
        evaluator = FakeEvaluator()

        coordinator = PlanningCoordinator(
            db_session,
            planner_client=planner_client,
            planner_provider="fake",
            planner_model=None,
            evaluator=evaluator,
            source_repos_root=source_repos_root,
        )

        result = coordinator.plan_outcome(
            intent_id="test-intent",
            worker_id="coder-1",
            lane="coder",
            available_repos=["test-repo"],
            repo_heads={"test-repo": "abc123"},
            repo_trees={},
        )

        # Should return a work item — intent not met
        assert result is not None
        outcome, work_item = result

        # Planner should have received the evaluator's findings
        assert len(planner_client.calls) == 1
        assert "No hello world" in planner_client.calls[0]["evaluation_findings"]


def test_evaluator_skips_when_repo_dir_missing(db_session):
    """End-to-end: source_repos_root exists but repo dir doesn't → evaluator skips."""
    with tempfile.TemporaryDirectory() as source_repos_root:
        # No repo dir created

        db_session.add(
            IntentVersion(
                intent_id="test-intent",
                intent_version=1,
                brief_text="Build something",
            )
        )
        db_session.commit()

        planner_client = FakePlannerClient()
        evaluator = FakeEvaluator()

        coordinator = PlanningCoordinator(
            db_session,
            planner_client=planner_client,
            planner_provider="fake",
            planner_model=None,
            evaluator=evaluator,
            source_repos_root=source_repos_root,
        )

        result = coordinator.plan_outcome(
            intent_id="test-intent",
            worker_id="coder-1",
            lane="coder",
            available_repos=["test-repo"],
            repo_heads={"test-repo": "abc123"},
            repo_trees={},
        )

        # Should still plan (evaluator skipped, planner ran)
        assert result is not None
        assert len(planner_client.calls) == 1
        assert planner_client.calls[0]["evaluation_findings"] is None
