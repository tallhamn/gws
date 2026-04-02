from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, func, insert, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.pool import StaticPool

from gws.db import make_engine
from gws.models import Case, IntentVersion, PullRequest, Step, StepStatus


def test_can_persist_intent_case_and_step(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    pull = PullRequest(worker_id="coder-1", lane="coder", intent_id="intent-1", repo_access_set=["repo-a"])
    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)

    session.add_all([intent, pull, case, step])
    session.commit()
    session.expunge_all()

    assert session.get(IntentVersion, intent.id).brief_text == "ship /music"
    assert session.get(Case, case.id).intent_version_ref.brief_text == "ship /music"
    assert session.get(Step, step.id).status is StepStatus.READY


def test_intent_versions_are_unique_per_intent_and_version(session):
    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="one"))
    session.commit()

    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="two"))

    with pytest.raises(IntegrityError):
        session.commit()


def test_case_references_existing_intent_version(session):
    session.execute(text("PRAGMA foreign_keys=ON"))

    session.add(Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music"))

    with pytest.raises(IntegrityError):
        session.commit()


def test_json_payload_mutations_persist_after_commit(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music", accepted_amendments=[{"path": "a"}])
    pull = PullRequest(worker_id="coder-1", lane="coder", intent_id="intent-1", repo_access_set=["repo-a"], envelope={"mode": "strict"})
    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    step = Step(
        case=case,
        repo="repo-a",
        lane="coder",
        step_type="execute",
        status=StepStatus.READY,
        allowed_paths=["services/**"],
        forbidden_paths=["tests/**"],
        artifact_requirements=["diff"],
    )

    session.add_all([intent, pull, case, step])
    session.commit()

    intent.accepted_amendments.append({"path": "b"})
    pull.repo_access_set.append("repo-b")
    pull.envelope["mode"] = "relaxed"
    step.allowed_paths.append("docs/**")
    step.forbidden_paths.append("infra/**")
    step.artifact_requirements.append("summary")
    session.commit()
    session.expunge_all()

    reloaded_intent = session.get(IntentVersion, intent.id)
    reloaded_pull = session.get(PullRequest, pull.id)
    reloaded_step = session.get(Step, step.id)

    assert reloaded_intent.accepted_amendments == [{"path": "a"}, {"path": "b"}]
    assert reloaded_pull.repo_access_set == ["repo-a", "repo-b"]
    assert reloaded_pull.envelope == {"mode": "relaxed"}
    assert reloaded_step.allowed_paths == ["services/**", "docs/**"]
    assert reloaded_step.forbidden_paths == ["tests/**", "infra/**"]
    assert reloaded_step.artifact_requirements == ["diff", "summary"]


def test_fresh_instances_support_json_defaults_before_flush(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    pull = PullRequest(worker_id="coder-1", lane="coder", intent_id="intent-1")
    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)

    intent.accepted_amendments.append({"path": "a", "meta": {"owner": "alice"}})
    pull.repo_access_set.append("repo-a")
    pull.envelope["limits"] = {"max_runtime": 10}
    step.allowed_paths.append("services/**")
    step.forbidden_paths.append("tests/**")
    step.artifact_requirements.append("diff")

    session.add_all([intent, pull, case, step])
    session.commit()
    session.expunge_all()

    reloaded_intent = session.get(IntentVersion, intent.id)
    reloaded_pull = session.get(PullRequest, pull.id)
    reloaded_step = session.get(Step, step.id)

    assert reloaded_intent.accepted_amendments == [{"path": "a", "meta": {"owner": "alice"}}]
    assert reloaded_pull.repo_access_set == ["repo-a"]
    assert reloaded_pull.envelope == {"limits": {"max_runtime": 10}}
    assert reloaded_step.allowed_paths == ["services/**"]
    assert reloaded_step.forbidden_paths == ["tests/**"]
    assert reloaded_step.artifact_requirements == ["diff"]


def test_nested_json_payload_mutations_persist_after_commit(session):
    intent = IntentVersion(
        intent_id="intent-1",
        intent_version=1,
        brief_text="ship /music",
        accepted_amendments=[{"path": "a", "meta": {"owner": "alice"}}],
    )
    pull = PullRequest(
        worker_id="coder-1",
        lane="coder",
        intent_id="intent-1",
        repo_access_set=["repo-a"],
        envelope={"limits": {"max_runtime": 10}},
    )
    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)

    session.add_all([intent, pull, case, step])
    session.commit()

    intent.accepted_amendments[0]["path"] = "b"
    pull.envelope["limits"]["max_runtime"] = 30
    session.commit()
    session.expunge_all()

    reloaded_intent = session.get(IntentVersion, intent.id)
    reloaded_pull = session.get(PullRequest, pull.id)

    assert reloaded_intent.accepted_amendments[0]["path"] == "b"
    assert reloaded_pull.envelope["limits"]["max_runtime"] == 30


def test_step_status_persists_lowercase_value(session):
    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music"))
    session.commit()

    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    session.add(case)

    step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
    session.add(step)
    session.commit()

    stored_status = session.execute(text("select status from steps where id = :id"), {"id": step.id}).scalar_one()

    assert stored_status == StepStatus.READY.value
    assert session.get(Step, step.id).status is StepStatus.READY


def test_step_status_rejects_invalid_raw_strings(session):
    session.add(IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music"))
    session.commit()

    case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status="not-a-valid-status")
    session.add(step)

    with pytest.raises((StatementError, IntegrityError, ValueError)):
        session.commit()


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
    pull = PullRequest(worker_id="coder-1", lane="coder", intent_id="intent-1", repo_access_set=["repo-a"], envelope={"limits": {"max_runtime": 10}})
    session.add(pull)
    session.commit()

    old_limits = pull.envelope["limits"]
    del pull.envelope["limits"]
    old_limits["max_runtime"] = 999
    session.commit()
    session.expunge_all()

    reloaded_pull = session.get(PullRequest, pull.id)

    assert reloaded_pull.envelope == {}


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
    pull = PullRequest(worker_id="coder-1", lane="coder", intent_id="intent-1")

    with pytest.raises(KeyError):
        pull.envelope.popitem()


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
