from sqlalchemy.exc import IntegrityError

from gws.amendments import AmendmentService
from gws.models import AmendmentProposal, Case, IntentVersion, Step, StepStatus


def test_breaking_amendment_creates_intent_version_plus_one(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    proposal = AmendmentProposal(
        intent_id="intent-1",
        base_intent_version=1,
        summary="Add podcast support",
        amended_brief_text="ship /music and /podcasts",
        is_breaking=True,
    )
    session.add_all([intent, proposal])
    session.commit()

    accepted_intent = AmendmentService(session).accept_proposal(proposal.id)

    assert accepted_intent.intent_version == 2
    assert accepted_intent.brief_text == "ship /music and /podcasts"


def test_breaking_amendment_revokes_open_steps_for_prior_intent_version(session):
    prior_intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    current_case = Case(intent_id="intent-1", intent_version=1, title="Create /music", goal="Implement /music")
    open_step = Step(case=current_case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
    succeeded_step = Step(case=current_case, repo="repo-a", lane="coder", step_type="verify", status=StepStatus.SUCCEEDED)

    other_intent = IntentVersion(intent_id="intent-2", intent_version=1, brief_text="ship /video")
    other_case = Case(intent_id="intent-2", intent_version=1, title="Create /video", goal="Implement /video")
    other_step = Step(case=other_case, repo="repo-b", lane="coder", step_type="execute", status=StepStatus.READY)

    proposal = AmendmentProposal(
        intent_id="intent-1",
        base_intent_version=1,
        summary="Replace /music with /podcasts",
        amended_brief_text="ship /podcasts",
        is_breaking=True,
    )
    session.add_all([prior_intent, other_intent, current_case, other_case, open_step, succeeded_step, other_step, proposal])
    session.commit()

    AmendmentService(session).accept_proposal(proposal.id)
    session.refresh(open_step)
    session.refresh(succeeded_step)
    session.refresh(other_step)

    assert open_step.status is StepStatus.REVOKED
    assert succeeded_step.status is StepStatus.SUCCEEDED
    assert other_step.status is StepStatus.READY


def test_accepted_amendments_records_summary_on_new_intent(session):
    intent = IntentVersion(
        intent_id="intent-1",
        intent_version=1,
        brief_text="ship /music",
        accepted_amendments=[{"summary": "Initial scope", "is_breaking": False}],
    )
    proposal = AmendmentProposal(
        intent_id="intent-1",
        base_intent_version=1,
        summary="Add podcast support",
        amended_brief_text="ship /music and /podcasts",
        is_breaking=True,
    )
    session.add_all([intent, proposal])
    session.commit()

    accepted_intent = AmendmentService(session).accept_proposal(proposal.id)

    assert accepted_intent.accepted_amendments == [
        {"summary": "Initial scope", "is_breaking": False},
        {"summary": "Add podcast support", "is_breaking": True},
    ]


def test_accept_proposal_rejects_stale_base_intent_version(session):
    older_intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    latest_intent = IntentVersion(intent_id="intent-1", intent_version=2, brief_text="ship /music and /video")
    proposal = AmendmentProposal(
        intent_id="intent-1",
        base_intent_version=1,
        summary="Add podcasts from stale base",
        amended_brief_text="ship /music and /podcasts",
        is_breaking=True,
    )
    session.add_all([older_intent, latest_intent, proposal])
    session.commit()

    try:
        AmendmentService(session).accept_proposal(proposal.id)
    except ValueError as exc:
        assert str(exc) == "proposal base intent version is stale"
    else:
        raise AssertionError("expected ValueError")


def test_accepting_proposal_does_not_mutate_prior_intent_amendment_history(session):
    intent = IntentVersion(
        intent_id="intent-1",
        intent_version=1,
        brief_text="ship /music",
        accepted_amendments=[{"summary": "Initial scope", "is_breaking": False, "meta": {"owner": "alice"}}],
    )
    proposal = AmendmentProposal(
        intent_id="intent-1",
        base_intent_version=1,
        summary="Add podcast support",
        amended_brief_text="ship /music and /podcasts",
        is_breaking=True,
    )
    session.add_all([intent, proposal])
    session.commit()

    accepted_intent = AmendmentService(session).accept_proposal(proposal.id)
    accepted_intent.accepted_amendments[0]["meta"]["owner"] = "bob"
    session.commit()
    session.refresh(intent)

    assert intent.accepted_amendments == [
        {"summary": "Initial scope", "is_breaking": False, "meta": {"owner": "alice"}}
    ]


def test_accept_proposal_translates_duplicate_version_conflict_to_domain_error(session, monkeypatch):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="ship /music")
    proposal = AmendmentProposal(
        intent_id="intent-1",
        base_intent_version=1,
        summary="Add podcast support",
        amended_brief_text="ship /music and /podcasts",
        is_breaking=True,
    )
    session.add_all([intent, proposal])
    session.commit()

    service = AmendmentService(session)
    real_commit = session.commit
    call_count = 0

    def flaky_commit():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise IntegrityError("insert", {}, Exception("duplicate version"))
        return real_commit()

    monkeypatch.setattr(session, "commit", flaky_commit)

    try:
        service.accept_proposal(proposal.id)
    except ValueError as exc:
        assert str(exc) == "proposal base intent version is stale"
    else:
        raise AssertionError("expected ValueError")
