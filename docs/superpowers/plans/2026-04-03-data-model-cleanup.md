# Data Model Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clean up four data model inconsistencies: dead `_init_json_defaults` refs, inline enum normalization on Attempt/Verdict, typed `AmendmentProposalStatus` enum, and a `description` field on WorkItem for governance context.

**Architecture:** All changes are internal to the GWS models layer. The WorkItem `description` field is the only change that touches the API contract (`WorkerLeaseResponse`). An alembic migration adds the new column and enum. No breaking changes to existing data.

**Tech Stack:** SQLAlchemy ORM, Alembic, Pydantic, pytest

---

### Task 1: Clean dead references in `_init_json_defaults`

**Files:**
- Modify: `gws/models.py:561-589`
- Test: `tests/test_models.py`

The `_init_json_defaults` event listener checks for attributes that don't exist on any current model: `envelope`, `repo_access_set`, and `planning_result`. These are leftovers from the pre-cutover Case/Step model. Remove them.

- [ ] **Step 1: Remove dead attribute checks from `_init_json_defaults`**

In `gws/models.py`, replace the `_init_json_defaults` function (lines 561-589) with:

```python
def _init_json_defaults(target, args, kwargs):
    del args
    if getattr(target, "accepted_amendments", None) is None:
        target.accepted_amendments = DeepMutableList()
    if getattr(target, "available_repos", None) is None:
        target.available_repos = DeepMutableList()
    if getattr(target, "repo_heads", None) is None:
        target.repo_heads = DeepMutableDict()
    if getattr(target, "planning_context", None) is None:
        target.planning_context = DeepMutableDict()
    if getattr(target, "plan_payload", None) is None:
        target.plan_payload = DeepMutableDict()
    if getattr(target, "allowed_paths", None) is None:
        target.allowed_paths = DeepMutableList()
    if getattr(target, "forbidden_paths", None) is None:
        target.forbidden_paths = DeepMutableList()
    if getattr(target, "artifact_requirements", None) is None:
        target.artifact_requirements = DeepMutableList()
    if getattr(target, "artifact_refs", None) is None:
        target.artifact_refs = DeepMutableList()
    if getattr(target, "payload", None) is None:
        target.payload = DeepMutableDict()
```

- [ ] **Step 2: Run tests to verify nothing breaks**

Run: `python3 -m pytest tests/test_models.py tests/test_control_plane.py tests/test_amendments.py -q`
Expected: All 49 tests pass.

- [ ] **Step 3: Commit**

```bash
git add gws/models.py
git commit -m "cleanup: remove dead _init_json_defaults refs (envelope, repo_access_set, planning_result)"
```

---

### Task 2: Normalize Attempt and Verdict to use `_enum_column()`

**Files:**
- Modify: `gws/models.py:516-558`
- Test: `tests/test_control_plane.py`

`Attempt.result_status` (line 525-534) and `Verdict.result` (line 548-556) define their `Enum()` columns inline with duplicated `values_callable` lambdas. Every other enum column uses the `_enum_column()` helper. Normalize these two.

- [ ] **Step 1: Write a test that verifies enum column consistency**

Add to `tests/test_models.py`:

```python
def test_attempt_result_status_rejects_invalid_values(session):
    from gws.models import Attempt, AttemptResultStatus, Lease

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

    from sqlalchemy.exc import StatementError

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
```

- [ ] **Step 2: Run test to verify it passes (validation already works with inline Enum)**

Run: `python3 -m pytest tests/test_models.py::test_attempt_result_status_rejects_invalid_values -v`
Expected: PASS (the inline Enum already validates; this test locks the behavior before refactoring).

- [ ] **Step 3: Replace inline Enum definitions with `_enum_column()`**

In `gws/models.py`, replace the `Attempt.result_status` column (lines 525-534):

```python
    result_status: Mapped[AttemptResultStatus] = mapped_column(
        _enum_column(AttemptResultStatus),
        default=AttemptResultStatus.PENDING,
    )
```

Replace the `Verdict.result` column (lines 548-556):

```python
    result: Mapped[VerdictResult] = mapped_column(_enum_column(VerdictResult))
```

- [ ] **Step 4: Run all tests to verify nothing breaks**

Run: `python3 -m pytest tests/test_models.py tests/test_control_plane.py -q`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add gws/models.py tests/test_models.py
git commit -m "cleanup: normalize Attempt/Verdict enum columns to use _enum_column() helper"
```

---

### Task 3: Add `AmendmentProposalStatus` enum

**Files:**
- Modify: `gws/models.py:477-489` (AmendmentProposal model)
- Modify: `gws/amendments.py:31,72` (bare string comparisons)
- Test: `tests/test_amendments.py`
- Test: `tests/test_models.py`

`AmendmentProposal.status` is a bare `String(32)` with default `"pending"`. Every other status field uses a typed enum. Add `AmendmentProposalStatus` and update the two bare-string references in `amendments.py`.

- [ ] **Step 1: Write a test that verifies the enum rejects invalid values**

Add to `tests/test_models.py`:

```python
def test_amendment_proposal_status_rejects_invalid_values(session):
    from gws.models import AmendmentProposal
    from sqlalchemy.exc import StatementError

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
```

- [ ] **Step 2: Run test to verify it fails (currently accepts any string)**

Run: `python3 -m pytest tests/test_models.py::test_amendment_proposal_status_rejects_invalid_values -v`
Expected: FAIL — the bare `String(32)` column accepts `"bogus"` without error.

- [ ] **Step 3: Add the enum and update the model**

In `gws/models.py`, add the enum after `WorkItemStatus` (after line 85):

```python
class AmendmentProposalStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
```

Update the `AmendmentProposal` model — replace the `status` column:

```python
    status: Mapped[AmendmentProposalStatus] = mapped_column(
        _enum_column(AmendmentProposalStatus), default=AmendmentProposalStatus.PENDING
    )
```

- [ ] **Step 4: Update amendments.py to use the enum**

In `gws/amendments.py`, add `AmendmentProposalStatus` to the import:

```python
from .models import AmendmentProposal, AmendmentProposalStatus, IntentVersion, Outcome, WorkItem, WorkItemStatus
```

Replace line 31:
```python
        if proposal.status != AmendmentProposalStatus.PENDING:
```

Replace line 72:
```python
        proposal.status = AmendmentProposalStatus.ACCEPTED
```

- [ ] **Step 5: Run tests to verify**

Run: `python3 -m pytest tests/test_models.py::test_amendment_proposal_status_rejects_invalid_values tests/test_amendments.py -v`
Expected: All pass. The new test now catches `"bogus"`, and existing amendment tests work with the enum.

- [ ] **Step 6: Commit**

```bash
git add gws/models.py gws/amendments.py tests/test_models.py
git commit -m "feat: add AmendmentProposalStatus enum replacing bare string status"
```

---

### Task 4: Add `description` field to WorkItem

**Files:**
- Modify: `gws/models.py:368-410` (WorkItem model)
- Modify: `gws/contracts.py:6-12,21-32` (SynthesizedPlan + WorkerLeaseResponse)
- Modify: `gws/planner.py:115-125` (materialize plan → work item)
- Modify: `gws/control_plane.py:58-93` (`_append_governance_work_items`)
- Modify: `gws/api.py:216-228` (lease response assembly)
- Test: `tests/test_models.py`
- Test: `tests/test_control_plane.py`

Workers currently receive outcome-level `title`+`goal` but no per-work-item context. This matters most for governance-appended review items, which carry no information about why they were created. Add a `description` field to WorkItem, populate it from the planner for initial items and from governance context for appended items, and expose it in the lease response.

- [ ] **Step 1: Write a test for WorkItem description persistence**

Add to `tests/test_models.py`:

```python
def test_work_item_description_persists(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.READY,
    )
    work_item = WorkItem(
        outcome=outcome,
        sequence_index=0,
        repo="repo-a",
        lane="coder",
        work_type="execute",
        status=WorkItemStatus.READY,
        description="Implement the /music endpoint with playlist support",
    )
    session.add_all([intent, outcome, work_item])
    session.commit()
    session.expunge_all()

    stored = session.get(WorkItem, work_item.id)
    assert stored.description == "Implement the /music endpoint with playlist support"


def test_work_item_description_defaults_empty(session):
    intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="Ship /music")
    outcome = Outcome(
        intent_id="intent-1",
        intent_version=1,
        title="Create /music",
        goal="Implement /music",
        phase=OutcomePhase.READY,
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
    session.expunge_all()

    stored = session.get(WorkItem, work_item.id)
    assert stored.description == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_models.py::test_work_item_description_persists tests/test_models.py::test_work_item_description_defaults_empty -v`
Expected: FAIL — `description` attribute does not exist on WorkItem.

- [ ] **Step 3: Add the `description` column to WorkItem**

In `gws/models.py`, add after the `work_type` column (after line 386):

```python
    description: Mapped[str] = mapped_column(Text, default="")
```

- [ ] **Step 4: Run model tests to verify they pass**

Run: `python3 -m pytest tests/test_models.py::test_work_item_description_persists tests/test_models.py::test_work_item_description_defaults_empty -v`
Expected: PASS.

- [ ] **Step 5: Update `SynthesizedPlan` contract to include description**

In `gws/contracts.py`, add `description` to `SynthesizedPlan`:

```python
class SynthesizedPlan(BaseModel):
    title: str
    goal: str
    description: str = ""
    repo: str
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    work_type: str
```

- [ ] **Step 6: Update `WorkerLeaseResponse` to include description**

In `gws/contracts.py`, add `description` to `WorkerLeaseResponse`:

```python
class WorkerLeaseResponse(BaseModel):
    lease_id: int
    work_item_id: int
    repo: str
    title: str
    goal: str
    description: str
    work_type: str
    allowed_paths: list[str]
    forbidden_paths: list[str]
    base_commit: str | None = None
    artifact_requirements: list[str] = Field(default_factory=list)
    heartbeat_deadline: str
```

- [ ] **Step 7: Update `planner.py` to populate description from plan**

In `gws/planner.py`, update the WorkItem creation (around line 115) to include `description`:

```python
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
        )
```

- [ ] **Step 8: Update `_append_governance_work_items` to populate description**

In `gws/control_plane.py`, update the WorkItem creation in `_append_governance_work_items` (around line 78) to include a governance-specific description:

```python
            review_work_item = WorkItem(
                outcome=outcome,
                sequence_index=next_sequence_index,
                blocked_by_work_item=work_item,
                repo=work_item.repo,
                lane=lane,
                work_type="review",
                description=f"Review changes from work item {work_item.id} ({work_item.work_type}). Policy triggered {lane} review.",
                status=WorkItemStatus.READY,
                allowed_paths=list(work_item.allowed_paths),
                forbidden_paths=list(work_item.forbidden_paths),
                base_commit=work_item.base_commit,
                artifact_requirements=list(work_item.artifact_requirements),
            )
```

- [ ] **Step 9: Update lease response assembly in api.py**

In `gws/api.py`, add `description` to the `WorkerLeaseResponse` construction (around line 216):

```python
            return WorkerLeaseResponse(
                lease_id=lease.id,
                work_item_id=work_item.id,
                title=work_item.outcome.title,
                goal=work_item.outcome.goal,
                description=work_item.description,
                repo=work_item.repo,
                work_type=work_item.work_type,
                allowed_paths=list(work_item.allowed_paths),
                forbidden_paths=list(work_item.forbidden_paths),
                base_commit=work_item.base_commit,
                artifact_requirements=list(work_item.artifact_requirements),
                heartbeat_deadline=lease.heartbeat_deadline.isoformat(),
            )
```

- [ ] **Step 10: Write a test verifying governance work items get descriptions**

Add to `tests/test_control_plane.py`:

```python
def test_governance_work_items_get_descriptive_context(session):
    outcome, work_item = _outcome_with_work_item(session, allowed_paths=["auth/**"])

    service = ControlPlaneService(session)
    service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)

    service.apply_attempt_completion(
        work_item_id=work_item.id,
        worker_id="worker-1",
        touched_paths=["auth/session.py"],
        changed_hunks=["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
    )

    review_items = (
        session.query(WorkItem)
        .filter(WorkItem.outcome_id == outcome.id, WorkItem.work_type == "review")
        .all()
    )

    assert len(review_items) == 1
    assert "security-review" in review_items[0].description
    assert str(work_item.id) in review_items[0].description
```

- [ ] **Step 11: Run all tests**

Run: `python3 -m pytest tests/test_models.py tests/test_control_plane.py tests/test_amendments.py -q`
Expected: All tests pass (49 existing + 4 new).

- [ ] **Step 12: Commit**

```bash
git add gws/models.py gws/contracts.py gws/planner.py gws/control_plane.py gws/api.py tests/test_models.py tests/test_control_plane.py
git commit -m "feat: add description field to WorkItem for per-item execution context"
```

---

### Task 5: Alembic migration

**Files:**
- Create: `alembic/versions/a4c2e8f91b3d_data_model_cleanup.py`

This migration covers all schema changes: the new `AmendmentProposalStatus` enum column on `amendment_proposals`, and the new `description` column on `work_items`. The enum normalization on Attempt/Verdict doesn't require a migration because the underlying stored values are unchanged — `_enum_column()` produces the same `native_enum=False` column with `create_constraint=True` as the inline definition.

- [ ] **Step 1: Create the migration file**

Create `alembic/versions/a4c2e8f91b3d_data_model_cleanup.py`:

```python
"""data model cleanup

Revision ID: a4c2e8f91b3d
Revises: 7c3b1d5c8f1a
Create Date: 2026-04-03 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c2e8f91b3d"
down_revision: Union[str, None] = "7c3b1d5c8f1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add description column to work_items (defaults empty string)
    op.add_column("work_items", sa.Column("description", sa.Text(), nullable=False, server_default=""))

    # Replace bare varchar status on amendment_proposals with constrained enum
    # The existing values ("pending", "accepted") are valid members of the new enum,
    # so no data migration is needed — just add the check constraint.
    op.create_check_constraint(
        "ck_amendment_proposals_status_amendmentproposalstatus",
        "amendment_proposals",
        sa.column("status").in_(["pending", "accepted"]),
    )


def downgrade() -> None:
    op.drop_constraint("ck_amendment_proposals_status_amendmentproposalstatus", "amendment_proposals", type_="check")
    op.drop_column("work_items", "description")
```

- [ ] **Step 2: Run tests to verify the migration is compatible with the test suite**

The test suite uses `Base.metadata.create_all(engine)` which creates tables from the ORM models directly, so it already includes the new column and enum. Run the full suite:

Run: `python3 -m pytest tests/test_models.py tests/test_control_plane.py tests/test_amendments.py -q`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add alembic/versions/a4c2e8f91b3d_data_model_cleanup.py
git commit -m "migration: add work_item.description and amendment_proposal status constraint"
```
