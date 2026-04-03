from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, func, insert, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.pool import StaticPool

from gws.db import make_engine
from gws.models import (
    Attempt,
    AttemptResultStatus,
    IntentVersion,
    Lease,
    Outcome,
    OutcomeEvent,
    OutcomePhase,
    OutcomeResult,
    PlanningSession,
    PlanningSessionStatus,
    WorkItem,
    WorkItemStatus,
)


def test_legacy_pull_request_runtime_model_is_gone():
    import gws.models as models

    assert not hasattr(models, "PullRequest")


def test_can_persist_intent_outcome_planning_session_and_work_item(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    outcome = Outcome(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    planning = PlanningSession(
        outcome=outcome,
        worker_id="coder-1",
        lane="coder",
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
        available_repos=["repo-a"],
        repo_heads={"repo-a": "abc123"},
        planning_context={"brief": "ship /music"},
    )
    work_item = WorkItem(
        outcome=outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
    )

    session.add_all([intent, outcome, planning, work_item])
    session.commit()
    session.expunge_all()

    assert session.get(IntentVersion, intent.id).brief_text == "ship /music"
    assert session.get(Outcome, outcome.id).intent_id == "intent-1"
    assert session.get(PlanningSession, planning.id).status is PlanningSessionStatus.PENDING
    assert session.get(WorkItem, work_item.id).status is WorkItemStatus.READY


def test_intent_versions_are_unique_per_intent_and_version(session):
    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="one"))
    session.commit()

    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="two"))

    with pytest.raises(IntegrityError):
        session.commit()


def test_legacy_case_and_step_runtime_models_are_gone():
    import gws.models as models

    assert not hasattr(models, "Case")
    assert not hasattr(models, "Step")
    assert not hasattr(models, "StepStatus")


def test_lease_and_attempt_persist_with_required_work_item_target(session):
    from gws.models import (
        Attempt,
        AttemptResultStatus,
        IntentVersion,
        Lease,
        Outcome,
        OutcomePhase,
        WorkItem,
        WorkItemStatus,
    )

    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    outcome = Outcome(
        intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music", phase=OutcomePhase.READY
    )
    work_item = WorkItem(
        outcome=outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
    )
    session.add_all([intent, outcome, work_item])
    session.commit()

    lease = Lease(
        work_item_id=work_item.id,
        worker_id="worker-1",
        lane="coder",
        heartbeat_deadline=work_item.created_at,
        expires_at=work_item.created_at,
    )
    session.add(lease)
    session.flush()

    attempt = Attempt(
        work_item_id=work_item.id,
        lease_id=lease.id,
        worker_id="worker-1",
        repo="repo-a",
        result_status=AttemptResultStatus.PENDING,
    )
    session.add(attempt)
    session.commit()

    assert session.get(Lease, lease.id).work_item_id == work_item.id
    assert session.get(Attempt, attempt.id).work_item_id == work_item.id


def test_json_payload_mutations_persist_after_commit(session):
    intent = IntentVersion(
        intent_id="intent-1", intent_version=1, brief_text="ship /music", accepted_amendments=[{"path": "a"}]
    )
    outcome = Outcome(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    planning = PlanningSession(
        outcome=outcome,
        worker_id="coder-1",
        lane="coder",
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
        available_repos=["repo-a"],
        repo_heads={"repo-a": "abc123"},
        planning_context={"mode": "strict"},
    )
    work_item = WorkItem(
        outcome=outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
        allowed_paths=["services/**"],
        forbidden_paths=["tests/**"],
        artifact_requirements=["diff"],
    )

    session.add_all([intent, outcome, planning, work_item])
    session.commit()

    intent.accepted_amendments.append({"path": "b"})
    planning.available_repos.append("repo-b")
    planning.planning_context["mode"] = "relaxed"
    work_item.allowed_paths.append("docs/**")
    work_item.forbidden_paths.append("infra/**")
    work_item.artifact_requirements.append("summary")
    session.commit()
    session.expunge_all()

    reloaded_intent = session.get(IntentVersion, intent.id)
    reloaded_planning = session.get(PlanningSession, planning.id)
    reloaded_work_item = session.get(WorkItem, work_item.id)

    assert reloaded_intent.accepted_amendments == [{"path": "a"}, {"path": "b"}]
    assert reloaded_planning.available_repos == ["repo-a", "repo-b"]
    assert reloaded_planning.planning_context == {"mode": "relaxed"}
    assert reloaded_work_item.allowed_paths == ["services/**", "docs/**"]
    assert reloaded_work_item.forbidden_paths == ["tests/**", "infra/**"]
    assert reloaded_work_item.artifact_requirements == ["diff", "summary"]


def test_fresh_instances_support_json_defaults_before_flush(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    outcome = Outcome(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    planning = PlanningSession(
        outcome=outcome,
        worker_id="coder-1",
        lane="coder",
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
    )
    work_item = WorkItem(
        outcome=outcome, sequence_index=0, repo="repo-a", lane="coder", work_type="execute", status=WorkItemStatus.READY
    )

    intent.accepted_amendments.append({"path": "a", "meta": {"owner": "alice"}})
    planning.available_repos.append("repo-a")
    planning.planning_context["limits"] = {"max_runtime": 10}
    work_item.allowed_paths.append("services/**")
    work_item.forbidden_paths.append("tests/**")
    work_item.artifact_requirements.append("diff")

    session.add_all([intent, outcome, planning, work_item])
    session.commit()
    session.expunge_all()

    reloaded_intent = session.get(IntentVersion, intent.id)
    reloaded_planning = session.get(PlanningSession, planning.id)
    reloaded_work_item = session.get(WorkItem, work_item.id)

    assert reloaded_intent.accepted_amendments == [{"path": "a", "meta": {"owner": "alice"}}]
    assert reloaded_planning.available_repos == ["repo-a"]
    assert reloaded_planning.planning_context == {"limits": {"max_runtime": 10}}
    assert reloaded_work_item.allowed_paths == ["services/**"]
    assert reloaded_work_item.forbidden_paths == ["tests/**"]
    assert reloaded_work_item.artifact_requirements == ["diff"]


def test_nested_json_payload_mutations_persist_after_commit(session):
    intent = IntentVersion(
        intent_id="intent-1",
        intent_version=1,
        brief_text="ship /music",
        accepted_amendments=[{"path": "a", "meta": {"owner": "alice"}}],
    )
    outcome = Outcome(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    planning = PlanningSession(
        outcome=outcome,
        worker_id="coder-1",
        lane="coder",
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
        available_repos=["repo-a"],
        planning_context={"limits": {"max_runtime": 10}},
    )
    session.add_all([intent, outcome, planning])
    session.commit()

    intent.accepted_amendments[0]["path"] = "b"
    planning.planning_context["limits"]["max_runtime"] = 30
    session.commit()
    session.expunge_all()

    reloaded_intent = session.get(IntentVersion, intent.id)
    reloaded_planning = session.get(PlanningSession, planning.id)

    assert reloaded_intent.accepted_amendments[0]["path"] == "b"
    assert reloaded_planning.planning_context["limits"]["max_runtime"] == 30


def test_replaced_nested_list_items_stop_dirtying_old_parent(session):
    intent = IntentVersion(
        intent_id="intent-1",
        intent_version=1,
        brief_text="ship /music",
        accepted_amendments=[{"path": "old", "meta": {"owner": "alice"}}],
    )
    session.add(intent)
    session.commit()

    old_amendment = intent.accepted_amendments[0]
    intent.accepted_amendments[0] = {"path": "new", "meta": {"owner": "bob"}}
    old_amendment["path"] = "stale"
    session.commit()
    session.expunge_all()

    reloaded_intent = session.get(IntentVersion, intent.id)

    assert reloaded_intent.accepted_amendments == [{"path": "new", "meta": {"owner": "bob"}}]


def test_removed_nested_dict_entries_stop_dirtying_old_parent(session):
    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music"))
    outcome = Outcome(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    planning = PlanningSession(
        outcome=outcome,
        worker_id="coder-1",
        lane="coder",
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
        planning_context={"limits": {"max_runtime": 10}},
    )
    session.add_all([outcome, planning])
    session.commit()

    old_limits = planning.planning_context["limits"]
    del planning.planning_context["limits"]
    old_limits["max_runtime"] = 999
    session.commit()
    session.expunge_all()

    reloaded_planning = session.get(PlanningSession, planning.id)

    assert reloaded_planning.planning_context == {}


def test_removed_list_item_stops_dirtying_old_parent(session):
    intent = IntentVersion(
        intent_id="intent-1",
        intent_version=1,
        brief_text="ship /music",
        accepted_amendments=[{"path": "first"}, {"path": "second"}],
    )
    session.add(intent)
    session.commit()

    removed_amendment = intent.accepted_amendments[0]
    intent.accepted_amendments.remove({"path": "first"})
    session.commit()
    removed_amendment["path"] = "stale"

    assert not session.is_modified(intent, include_collections=True)


def test_deleted_list_item_stops_dirtying_old_parent(session):
    intent = IntentVersion(
        intent_id="intent-1",
        intent_version=1,
        brief_text="ship /music",
        accepted_amendments=[{"path": "first"}, {"path": "second"}],
    )
    session.add(intent)
    session.commit()

    removed_amendment = intent.accepted_amendments[0]
    del intent.accepted_amendments[0]
    session.commit()
    removed_amendment["path"] = "stale"

    assert not session.is_modified(intent, include_collections=True)


def test_popitem_on_empty_dict_raises_key_error(session):
    planning = PlanningSession(
        outcome=Outcome(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music"),
        worker_id="coder-1",
        lane="coder",
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
    )

    with pytest.raises(KeyError):
        planning.planning_context.popitem()


def test_intent_version_has_context_and_planner_guidance(session):
    iv = IntentVersion(
        intent_id="i-1",
        intent_version=1,
        brief_text="Build a platformer",
        context="Browser game. HTML/CSS/JS output.",
        planner_guidance="Prioritize core loop before polish.",
    )
    session.add(iv)
    session.commit()
    session.refresh(iv)
    assert iv.context == "Browser game. HTML/CSS/JS output."
    assert iv.planner_guidance == "Prioritize core loop before polish."


def test_intent_version_context_defaults_empty(session):
    iv = IntentVersion(intent_id="i-2", intent_version=1, brief_text="Build something")
    session.add(iv)
    session.commit()
    session.refresh(iv)
    assert iv.context == ""
    assert iv.planner_guidance == ""


def test_outcome_records_explicit_phase_and_result(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.COMPLETED,
        result=OutcomeResult.SUCCEEDED,
        selected_repo="repo-a",
        result_summary="Shipped /music endpoint",
        result_commit="abc123",
    )
    session.add_all([intent, outcome])
    session.commit()

    stored = session.get(Outcome, outcome.id)
    assert stored.phase is OutcomePhase.COMPLETED
    assert stored.result is OutcomeResult.SUCCEEDED
    assert stored.result_commit == "abc123"


def test_planning_session_defaults_and_json_mutations_persist(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.PLANNING,
    )
    planning = PlanningSession(
        outcome=outcome,
        worker_id="planner-1",
        lane="planner",
        planner_provider="claude_code",
    )
    planning.available_repos.append("repo-a")
    planning.repo_heads["repo-a"] = "abc123"
    planning.planning_context["intent"] = {"id": "intent-1"}
    planning.plan_payload["work_items"] = [{"repo": "repo-a"}]

    session.add_all([intent, outcome, planning])
    session.commit()
    session.expunge_all()

    stored = session.get(PlanningSession, planning.id)
    assert stored.status is PlanningSessionStatus.PENDING
    assert stored.available_repos == ["repo-a"]
    assert stored.repo_heads == {"repo-a": "abc123"}
    assert stored.planning_context == {"intent": {"id": "intent-1"}}
    assert stored.plan_payload == {"work_items": [{"repo": "repo-a"}]}


def test_work_item_supports_sequence_and_dependency(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.READY,
    )
    first = WorkItem(
        outcome=outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
    )
    session.add_all([intent, outcome, first])
    session.flush()

    second = WorkItem(
        outcome=outcome,
        sequence_index=1,
        blocked_by_work_item_id=first.id,
        repo="repo-a",
        lane="ci",
        work_type="review",
        status=WorkItemStatus.READY,
    )
    session.add(second)
    session.commit()

    stored = session.get(WorkItem, second.id)
    assert stored.sequence_index == 1
    assert stored.blocked_by_work_item_id == first.id


def test_work_item_dependency_relationship_round_trips(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.READY,
    )
    upstream = WorkItem(
        outcome=outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
    )
    downstream = WorkItem(
        outcome=outcome,
        sequence_index=1,
        blocked_by_work_item=upstream,
        repo="repo-a",
        lane="ci",
        work_type="review",
        status=WorkItemStatus.READY,
    )

    session.add_all([intent, outcome, upstream, downstream])
    session.commit()
    session.expunge_all()

    stored_downstream = session.get(WorkItem, downstream.id)
    stored_upstream = session.get(WorkItem, upstream.id)

    assert stored_downstream.blocked_by_work_item_id == upstream.id
    assert stored_downstream.blocked_by_work_item.id == upstream.id
    assert [item.id for item in stored_upstream.dependent_work_items] == [downstream.id]


def test_work_items_are_ordered_by_sequence_index(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.READY,
    )
    late = WorkItem(
        outcome=outcome,
        sequence_index=10,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
    )
    early = WorkItem(
        outcome=outcome,
        sequence_index=5,
        repo="repo-a",
        lane="ci",
        work_type="review",
        status=WorkItemStatus.READY,
    )

    session.add_all([intent, outcome, late, early])
    session.commit()
    session.expunge_all()

    stored = session.get(Outcome, outcome.id)
    assert [item.sequence_index for item in stored.work_items] == [5, 10]


def test_work_item_sequence_index_must_be_unique_per_outcome(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.READY,
    )

    session.add_all(
        [
            intent,
            outcome,
            WorkItem(
                outcome=outcome,
                sequence_index=0,
                repo="repo-a",
                lane="coder",
                work_type="execute",
                status=WorkItemStatus.READY,
            ),
            WorkItem(
                outcome=outcome,
                sequence_index=0,
                repo="repo-a",
                lane="ci",
                work_type="review",
                status=WorkItemStatus.READY,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_work_item_dependency_cannot_cross_outcomes(session):
    session.execute(text("PRAGMA foreign_keys=ON"))
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    first_outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.READY,
    )
    second_outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Harden /music",
        goal="Add tests",
        phase=OutcomePhase.READY,
    )
    upstream = WorkItem(
        outcome=first_outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
    )
    session.add_all([intent, first_outcome, second_outcome, upstream])
    session.flush()

    downstream = WorkItem(
        outcome=second_outcome,
        sequence_index=0,
        blocked_by_work_item_id=upstream.id,
        repo="repo-a",
        lane="ci",
        work_type="review",
        status=WorkItemStatus.READY,
    )

    session.add(downstream)

    with pytest.raises(IntegrityError):
        session.commit()


def test_outcome_current_work_item_must_belong_to_same_outcome(session):
    session.execute(text("PRAGMA foreign_keys=ON"))
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    first_outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.RUNNING,
    )
    second_outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Harden /music",
        goal="Add tests",
        phase=OutcomePhase.RUNNING,
    )
    first_work_item = WorkItem(
        outcome=first_outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.RUNNING,
    )
    second_work_item = WorkItem(
        outcome=second_outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.RUNNING,
    )

    session.add_all([intent, first_outcome, second_outcome, first_work_item, second_work_item])
    session.commit()

    first_outcome.current_work_item_id = second_work_item.id

    with pytest.raises(ValueError):
        session.commit()


def test_outcome_event_payload_is_append_only(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.RUNNING,
    )
    event = OutcomeEvent(outcome=outcome, event_type="lease_extended", payload={"lease": {"id": 1, "seconds": 30}})

    session.add_all([intent, outcome, event])
    session.commit()
    event_id = event.id

    event.payload["lease"]["seconds"] = 45
    with pytest.raises((StatementError, ValueError)):
        session.commit()
    session.rollback()
    session.expunge_all()

    stored = session.get(OutcomeEvent, event_id)
    assert stored.payload == {"lease": {"id": 1, "seconds": 30}}


def test_outcome_event_delete_is_rejected(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.RUNNING,
    )
    event = OutcomeEvent(outcome=outcome, event_type="lease_extended", payload={"lease": {"id": 1, "seconds": 30}})

    session.add_all([intent, outcome, event])
    session.commit()
    event_id = event.id

    session.delete(event)
    with pytest.raises((StatementError, ValueError)):
        session.commit()
    session.rollback()

    assert session.get(OutcomeEvent, event_id) is not None


def test_outcome_with_null_current_work_item_persists(session):
    session.execute(text("PRAGMA foreign_keys=ON"))
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.PLANNING,
    )

    session.add_all([intent, outcome])
    session.commit()
    session.expunge_all()

    stored = session.get(Outcome, outcome.id)
    assert stored.current_work_item_id is None


def test_outcome_current_work_item_has_db_foreign_key(session):
    rows = session.execute(text("PRAGMA foreign_key_list(outcomes)")).all()

    assert any(row[2] == "work_items" and row[3] == "current_work_item_id" and row[4] == "id" for row in rows)


def test_cannot_move_referenced_current_work_item_to_different_outcome(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    first_outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.RUNNING,
    )
    second_outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Harden /music",
        goal="Add tests",
        phase=OutcomePhase.READY,
    )
    current_item = WorkItem(
        outcome=first_outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.RUNNING,
    )

    session.add_all([intent, first_outcome, second_outcome, current_item])
    session.commit()
    current_item_id = current_item.id
    first_outcome_id = first_outcome.id

    first_outcome.current_work_item_id = current_item.id
    session.commit()

    current_item.outcome_id = second_outcome.id
    with pytest.raises(ValueError):
        session.commit()
    session.rollback()
    session.expunge_all()

    stored_item = session.get(WorkItem, current_item_id)
    stored_outcome = session.get(Outcome, first_outcome_id)

    assert stored_item.outcome_id == first_outcome_id
    assert stored_outcome.current_work_item_id == current_item_id


def test_make_engine_uses_static_pool_for_in_memory_sqlite():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()
    sample = Table("sample", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(insert(sample).values(id=1))

    def read_count() -> int:
        with engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(sample)).scalar_one()

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(read_count).result() == 1

    assert isinstance(engine.pool, StaticPool)


def test_attempt_result_status_rejects_invalid_values(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    outcome = Outcome(
        intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music", phase=OutcomePhase.READY
    )
    work_item = WorkItem(
        outcome=outcome, sequence_index=0, repo="repo-a", lane="coder", work_type="execute", status=WorkItemStatus.READY
    )
    session.add_all([intent, outcome, work_item])
    session.commit()

    lease = Lease(
        work_item_id=work_item.id,
        worker_id="worker-1",
        lane="coder",
        heartbeat_deadline=work_item.created_at,
        expires_at=work_item.created_at,
    )
    session.add(lease)
    session.flush()

    attempt = Attempt(
        work_item_id=work_item.id,
        lease_id=lease.id,
        worker_id="worker-1",
        repo="repo-a",
        result_status="bogus",
    )
    session.add(attempt)

    with pytest.raises(StatementError, match="not among the defined enum values"):
        session.commit()


def test_amendment_proposal_status_rejects_invalid_values(session):
    from gws.models import AmendmentProposal

    proposal = AmendmentProposal(
        intent_id="intent-1",
        base_intent_version=1,
        summary="Add podcast support",
        amended_brief_text="ship /music and /podcasts",
        status="bogus",
    )
    session.add(proposal)

    with pytest.raises(StatementError, match="not among the defined enum values"):
        session.commit()
