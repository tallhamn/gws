from typing import Optional

import pytest

from gws.coordinator import PlanningCoordinator
from gws.models import (
    IntentVersion,
    Outcome,
    OutcomeEvent,
    OutcomePhase,
    OutcomeResult,
    PlanningSession,
    PlanningSessionStatus,
    WorkItem,
)


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
    ):
        self.calls.append(
            {
                "brief": brief,
                "lane": lane,
                "repo_heads": dict(repo_heads),
                "envelope": dict(envelope),
                "lane_capabilities": dict(lane_capabilities or {}),
                "intent_context": intent_context,
                "planner_guidance": planner_guidance,
            }
        )
        if isinstance(self.plan, dict):
            return dict(self.plan)
        return self.plan


def test_coordinator_creates_outcome_planning_session_and_work_item(session):
    session.add_all(
        [
            IntentVersion(
                intent_id="intent-1",
                intent_version=1,
                brief_text="old brief",
                context="old context",
                planner_guidance="old guidance",
            ),
            IntentVersion(
                intent_id="intent-1",
                intent_version=2,
                brief_text="ship /music",
                context="music domain context",
                planner_guidance="prefer endpoints over refactors",
            ),
        ]
    )
    session.commit()

    planner_client = FakePlannerClient(
        {
            "title": "Create /music endpoint",
            "goal": "Implement /music experience",
            "repo": "repo-a",
            "allowed_paths": ["services/**"],
            "forbidden_paths": ["infra/**"],
            "work_type": "execute",
        }
    )
    coordinator = PlanningCoordinator(
        session,
        planner_client=planner_client,
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
        lane_capabilities={"coder": "writes product code"},
    )

    outcome, work_item = coordinator.plan_outcome(
        intent_id="intent-1",
        worker_id="coder-1",
        lane="coder",
        available_repos=["repo-a"],
        repo_heads={"repo-a": "abc123"},
    )
    session.expunge_all()

    stored_outcome = session.get(Outcome, outcome.id)
    stored_work_item = session.get(WorkItem, work_item.id)
    stored_planning = session.query(PlanningSession).one()
    stored_events = session.query(OutcomeEvent).filter_by(outcome_id=outcome.id).order_by(OutcomeEvent.id.asc()).all()

    assert stored_outcome.intent_id == "intent-1"
    assert stored_outcome.intent_version == 2
    assert stored_outcome.phase is OutcomePhase.READY
    assert stored_outcome.selected_repo == "repo-a"

    assert stored_work_item.outcome_id == stored_outcome.id
    assert stored_work_item.repo == "repo-a"
    assert stored_work_item.base_commit == "abc123"

    assert stored_planning.status is PlanningSessionStatus.SUCCEEDED
    assert stored_planning.planner_provider == "claude_code"
    assert stored_planning.planner_model == "claude-sonnet-4-20250514"
    assert stored_planning.plan_payload == {**planner_client.plan, "description": ""}

    assert [event.event_type for event in stored_events] == ["planning_started", "planning_succeeded"]
    assert planner_client.calls == [
        {
            "brief": "ship /music",
            "lane": "coder",
            "repo_heads": {"repo-a": "abc123"},
            "envelope": {"existing_outcomes": []},
            "lane_capabilities": {"coder": "writes product code"},
            "intent_context": "music domain context",
            "planner_guidance": "prefer endpoints over refactors",
        }
    ]


def test_coordinator_includes_existing_outcomes_in_planning_envelope(session):
    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music"))
    existing = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Existing dashboard task",
        goal="Build the existing dashboard shell",
        phase=OutcomePhase.RUNNING,
        selected_repo="repo-a",
    )
    existing_work_item = WorkItem(
        outcome=existing,
        sequence_index=0,
        repo="repo-a",
        lane="artist",
        work_type="execute",
    )
    session.add_all([existing, existing_work_item])
    session.flush()
    existing.current_work_item_id = existing_work_item.id
    session.commit()

    planner_client = FakePlannerClient(
        {
            "title": "Create /music endpoint",
            "goal": "Implement /music experience",
            "repo": "repo-a",
            "allowed_paths": ["services/**"],
            "forbidden_paths": [],
            "work_type": "execute",
        }
    )
    coordinator = PlanningCoordinator(
        session,
        planner_client=planner_client,
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
    )

    coordinator.plan_outcome(
        intent_id="intent-1",
        worker_id="coder-1",
        lane="coder",
        available_repos=["repo-a"],
        repo_heads={"repo-a": "abc123"},
    )

    assert planner_client.calls[0]["envelope"] == {
        "existing_outcomes": [
            {
                "title": "Existing dashboard task",
                "goal": "Build the existing dashboard shell",
                "repo": "repo-a",
                "phase": "running",
                "result": None,
                "lane": "artist",
                "work_type": "execute",
            }
        ]
    }


def test_coordinator_commits_success_state_and_event_together(session, monkeypatch):
    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music"))
    session.commit()

    planner_client = FakePlannerClient(
        {
            "title": "Create /music endpoint",
            "goal": "Implement /music experience",
            "repo": "repo-a",
            "allowed_paths": ["services/**"],
            "forbidden_paths": [],
            "work_type": "execute",
        }
    )
    coordinator = PlanningCoordinator(
        session,
        planner_client=planner_client,
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
    )

    commit_calls = {"count": 0}
    original_commit = session.commit

    def counting_commit():
        commit_calls["count"] += 1
        return original_commit()

    monkeypatch.setattr(session, "commit", counting_commit)

    coordinator.plan_outcome(
        intent_id="intent-1",
        worker_id="coder-1",
        lane="coder",
        available_repos=["repo-a"],
        repo_heads={"repo-a": "abc123"},
    )

    assert commit_calls["count"] == 2


def test_coordinator_records_failed_planning_session_and_event(session):
    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music"))
    session.commit()

    planner_client = FakePlannerClient(
        {
            "title": "Change repo-b",
            "goal": "Implement in unavailable repo",
            "repo": "repo-b",
            "allowed_paths": ["services/**"],
            "forbidden_paths": [],
            "work_type": "execute",
        }
    )
    coordinator = PlanningCoordinator(
        session,
        planner_client=planner_client,
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
    )

    with pytest.raises(ValueError, match="repo repo-b is not in planning session available repos"):
        coordinator.plan_outcome(
            intent_id="intent-1",
            worker_id="coder-1",
            lane="coder",
            available_repos=["repo-a"],
            repo_heads={"repo-a": "abc123", "repo-b": "def456"},
        )

    stored_outcome = session.query(Outcome).one()
    stored_planning = session.query(PlanningSession).one()
    stored_events = (
        session.query(OutcomeEvent).filter_by(outcome_id=stored_outcome.id).order_by(OutcomeEvent.id.asc()).all()
    )

    assert stored_outcome.phase is OutcomePhase.COMPLETED
    assert stored_outcome.result is OutcomeResult.FAILED
    assert "repo repo-b is not in planning session available repos" in stored_outcome.result_summary
    assert stored_outcome.completed_at is not None
    assert session.query(WorkItem).count() == 0

    assert stored_planning.status is PlanningSessionStatus.FAILED
    assert "repo repo-b is not in planning session available repos" in stored_planning.error_detail
    assert stored_planning.plan_payload == {**planner_client.plan, "description": ""}

    assert [event.event_type for event in stored_events] == ["planning_started", "planning_failed"]


def test_coordinator_rolls_back_failed_materialization_before_recording_failure(session, monkeypatch):
    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music"))
    session.commit()

    planner_client = FakePlannerClient(
        {
            "title": "Create /music endpoint",
            "goal": "Implement /music experience",
            "repo": "repo-a",
            "allowed_paths": ["services/**"],
            "forbidden_paths": [],
            "work_type": "execute",
        }
    )
    coordinator = PlanningCoordinator(
        session,
        planner_client=planner_client,
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
    )

    original_flush = session.flush
    flush_state = {"failed": False}

    def flaky_flush(*args, **kwargs):
        if not flush_state["failed"] and any(isinstance(obj, WorkItem) for obj in session.new):
            flush_state["failed"] = True
            raise RuntimeError("flush boom")
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", flaky_flush)

    with pytest.raises(RuntimeError, match="flush boom"):
        coordinator.plan_outcome(
            intent_id="intent-1",
            worker_id="coder-1",
            lane="coder",
            available_repos=["repo-a"],
            repo_heads={"repo-a": "abc123"},
        )

    stored_outcome = session.query(Outcome).one()
    stored_planning = session.query(PlanningSession).one()
    stored_events = (
        session.query(OutcomeEvent).filter_by(outcome_id=stored_outcome.id).order_by(OutcomeEvent.id.asc()).all()
    )

    assert stored_outcome.phase is OutcomePhase.COMPLETED
    assert stored_outcome.result is OutcomeResult.FAILED
    assert stored_outcome.result_summary == "flush boom"
    assert stored_outcome.completed_at is not None
    assert stored_outcome.current_work_item_id is None
    assert session.query(WorkItem).count() == 0
    assert stored_planning.status is PlanningSessionStatus.FAILED
    assert stored_planning.error_detail == "flush boom"
    assert [event.event_type for event in stored_events] == ["planning_started", "planning_failed"]
