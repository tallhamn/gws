# GWS Domain Remodel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the transitional `PullRequest`/`Step`-centered runtime with an `Outcome`-centered domain model that records explicit final results and canonical history.

**Architecture:** Introduce new durable models (`Outcome`, `PlanningSession`, `WorkItem`, `OutcomeEvent`) alongside typed enums, migrate the runtime to a planning coordinator plus a thinner API layer, and only then retire legacy `PullRequest` and `Step` usage. Keep the external surface small while allowing the wire contract to move from `step` to `work_item` and adding explicit lease extension handling.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy ORM, Pydantic, pytest, SQLite test DB

---

## File Map

### New files

- `gws/coordinator.py`
  - Planning coordinator for intent resolution, planner invocation, planning session persistence, outcome/work-item materialization, and event emission.
- `tests/test_coordinator.py`
  - Coordinator-focused tests for outcome creation, planning failures, and event emission.

### Modified files

- `gws/models.py`
  - Add new enums and ORM models; retire legacy runtime model usage.
- `gws/contracts.py`
  - Replace step-centric wire types with outcome/work-item-centric types and lease extension request/response models.
- `gws/api.py`
  - Convert to a translation layer over coordinator/control-plane services and update routes to `work_item` vocabulary.
- `gws/control_plane.py`
  - Issue leases against `WorkItem`, accept lease extension requests, submit attempts, create verdicts, update `Outcome`, and emit `OutcomeEvent`.
- `gws/planner.py`
  - Plan against `PlanningSession` and materialize `Outcome` + initial `WorkItem`.
- `gws/public_timeline.py`
  - Read from `Outcome`/`WorkItem`/`OutcomeEvent` instead of reconstructing from old runtime assumptions.
- `tests/test_models.py`
  - New ORM coverage for the remodeled schema, enums, and defaults.
- `tests/test_api.py`
  - Route coverage for `work_item` completion and lease extension.
- `tests/test_control_plane.py`
  - Outcome/result/event coverage for execution lifecycle.
- `tests/test_end_to_end.py`
  - End-to-end flow from planning to completed outcome.
- `tests/test_public_timeline_api.py`
  - Timeline assertions against remodeled history.
- `README.md`
  - Update domain vocabulary and route names after cutover.

### Legacy files to remove or fully retire from runtime

- `gws/api.py`
  - remove `PullRequest`/`step` route semantics
- `gws/planner.py`
  - remove `plan_pull_request(...)`
- `gws/models.py`
  - retire `PullRequest` from runtime and rename `Step` model to `WorkItem`

---

### Task 1: Introduce the New Domain Models and Enums

**Files:**
- Modify: `gws/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing model tests**

```python
def test_outcome_records_explicit_phase_and_result(session):
    from gws.models import IntentVersion, Outcome, OutcomePhase, OutcomeResult

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


def test_work_item_supports_sequence_and_dependency(session):
    from gws.models import IntentVersion, Outcome, OutcomePhase, WorkItem, WorkItemStatus

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
    second = WorkItem(
        outcome=outcome,
        sequence_index=1,
        blocked_by_work_item_id=1,
        repo="repo-a",
        lane="ci",
        work_type="review",
        status=WorkItemStatus.READY,
    )
    session.add_all([intent, outcome, first, second])
    session.commit()

    assert session.get(WorkItem, second.id).sequence_index == 1
```

- [ ] **Step 2: Run the model tests to verify they fail**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_models.py -q`

Expected: FAIL with import or ORM errors for missing `Outcome`, `WorkItem`, or enum types.

- [ ] **Step 3: Add the new ORM enums and models**

```python
class OutcomePhase(str, enum.Enum):
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"


class OutcomeResult(str, enum.Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"


class Outcome(Base):
    __tablename__ = "outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(128), index=True)
    intent_version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    goal: Mapped[str] = mapped_column(Text)
    phase: Mapped[OutcomePhase] = mapped_column(_enum_column(OutcomePhase), default=OutcomePhase.PLANNING)
    result: Mapped[Optional[OutcomeResult]] = mapped_column(_enum_column(OutcomeResult), nullable=True)
    selected_repo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    current_work_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("work_items.id"), nullable=True)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    result_commit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
```

- [ ] **Step 4: Add `PlanningSession`, `WorkItem`, and `OutcomeEvent` plus JSON defaults**

```python
class PlanningSession(Base):
    __tablename__ = "planning_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outcome_id: Mapped[int] = mapped_column(ForeignKey("outcomes.id"), index=True)
    worker_id: Mapped[str] = mapped_column(String(128), index=True)
    lane: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[PlanningSessionStatus] = mapped_column(_enum_column(PlanningSessionStatus), default=PlanningSessionStatus.PENDING)
    planner_provider: Mapped[str] = mapped_column(String(64))
    planner_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    available_repos: Mapped[list[str]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    repo_heads: Mapped[dict] = mapped_column(DeepMutableDict.as_mutable(JSON), default=dict)
    planning_context: Mapped[dict] = mapped_column(DeepMutableDict.as_mutable(JSON), default=dict)
    plan_payload: Mapped[dict] = mapped_column(DeepMutableDict.as_mutable(JSON), default=dict)
    error_detail: Mapped[str] = mapped_column(Text, default="")
```

- [ ] **Step 5: Run the model tests to verify they pass**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_models.py -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git -C /Users/marcus/Documents/Github/gws add gws/models.py tests/test_models.py
git -C /Users/marcus/Documents/Github/gws commit -m "refactor: add outcome-centered domain models"
```

### Task 2: Replace Planner Persistence with a Planning Coordinator

**Files:**
- Create: `gws/coordinator.py`
- Modify: `gws/planner.py`
- Modify: `gws/planner_client.py`
- Test: `tests/test_coordinator.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing coordinator tests**

```python
def test_coordinator_creates_outcome_planning_session_and_work_item(session):
    coordinator = PlanningCoordinator(
        session,
        planner_client=FakePlannerClient(
            {
                "title": "Create /music endpoint",
                "goal": "Implement /music",
                "repo": "repo-a",
                "allowed_paths": ["services/**"],
                "forbidden_paths": ["infra/**"],
                "step_type": "execute",
            }
        ),
        planner_provider="claude_code",
        planner_model="claude-sonnet-4-20250514",
    )

    outcome, work_item = coordinator.plan_outcome(
        intent_id="intent-1",
        worker_id="coder-1",
        lane="coder",
        available_repos=["repo-a"],
        repo_heads={"repo-a": "abc123"},
    )

    assert outcome.selected_repo == "repo-a"
    assert work_item.repo == "repo-a"
    assert session.query(PlanningSession).one().status is PlanningSessionStatus.SUCCEEDED
    assert session.query(OutcomeEvent).filter_by(event_type="planning_succeeded").count() == 1
```

- [ ] **Step 2: Run the coordinator tests to verify they fail**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_coordinator.py tests/test_planner.py -q`

Expected: FAIL because `PlanningCoordinator` and new persistence flow do not exist.

- [ ] **Step 3: Implement `PlanningCoordinator` and move orchestration out of `api.py`**

```python
class PlanningCoordinator:
    def __init__(self, session: Session, *, planner_client: PlannerClient, planner_provider: str, planner_model: str | None, lane_capabilities: dict[str, str] | None = None):
        self.session = session
        self.planner_client = planner_client
        self.planner_provider = planner_provider
        self.planner_model = planner_model
        self.lane_capabilities = lane_capabilities or {}

    def plan_outcome(self, *, intent_id: str, worker_id: str, lane: str, available_repos: list[str], repo_heads: dict[str, str]) -> tuple[Outcome, WorkItem]:
        outcome = Outcome(intent_id=intent.intent_id, intent_version=intent.intent_version, title="", goal="", phase=OutcomePhase.PLANNING)
        planning_session = PlanningSession(
            outcome=outcome,
            worker_id=worker_id,
            lane=lane,
            planner_provider=self.planner_provider,
            planner_model=self.planner_model,
            available_repos=available_repos,
            repo_heads=repo_heads,
        )
```

- [ ] **Step 4: Rewrite `PlannerService` to plan a `PlanningSession`, not a `PullRequest`**

```python
def materialize_plan(self, planning_session_id: int) -> tuple[Outcome, WorkItem]:
    planning_session = self.session.get(PlanningSession, planning_session_id)
    plan = self.planner_client.synthesize(...)
    outcome = planning_session.outcome
    outcome.title = plan.title
    outcome.goal = plan.goal
    outcome.phase = OutcomePhase.READY
    outcome.selected_repo = plan.repo
    work_item = WorkItem(
        outcome=outcome,
        sequence_index=0,
        repo=plan.repo,
        lane=planning_session.lane,
        work_type=plan.step_type,
        status=WorkItemStatus.READY,
        allowed_paths=plan.allowed_paths,
        forbidden_paths=plan.forbidden_paths,
        base_commit=repo_heads[plan.repo],
    )
```

- [ ] **Step 5: Run the coordinator and planner tests to verify they pass**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_coordinator.py tests/test_planner.py -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git -C /Users/marcus/Documents/Github/gws add gws/coordinator.py gws/planner.py gws/planner_client.py tests/test_coordinator.py tests/test_planner.py
git -C /Users/marcus/Documents/Github/gws commit -m "refactor: add planning coordinator"
```

### Task 3: Remodel the Control Plane Around Outcome and WorkItem

**Files:**
- Modify: `gws/control_plane.py`
- Modify: `gws/models.py`
- Test: `tests/test_control_plane.py`
- Test: `tests/test_end_to_end.py`

- [ ] **Step 1: Write the failing control-plane tests**

```python
def test_apply_attempt_completion_marks_outcome_completed_on_success(session):
    service = ControlPlaneService(session, policy_path="policy.yaml")
    lease = service.issue_lease(work_item_id=work_item.id, worker_id="worker-1", ttl_seconds=60)

    service.apply_attempt_completion(
        work_item_id=work_item.id,
        worker_id="worker-1",
        touched_paths=["services/music/player.py"],
        changed_hunks=["+return play(queue)"],
    )

    session.refresh(outcome)
    assert outcome.phase is OutcomePhase.COMPLETED
    assert outcome.result is OutcomeResult.SUCCEEDED
    assert session.query(OutcomeEvent).filter_by(event_type="outcome_completed").count() == 1
```

- [ ] **Step 2: Run the control-plane tests to verify they fail**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_control_plane.py tests/test_end_to_end.py -q`

Expected: FAIL because `issue_lease(work_item_id=...)`, `apply_attempt_completion(...)`, and outcome completion behavior do not exist yet.

- [ ] **Step 3: Switch lease and attempt handling from `Step` to `WorkItem`**

```python
def issue_lease(self, work_item_id: int, worker_id: str, ttl_seconds: int) -> Lease:
    work_item = self.session.get(WorkItem, work_item_id)
    ...
    work_item.status = WorkItemStatus.LEASED
    work_item.outcome.phase = OutcomePhase.RUNNING
```

- [ ] **Step 4: Add explicit outcome completion and event emission**

```python
def _complete_outcome_from_work_item(self, work_item: WorkItem, *, result: OutcomeResult, summary: str, commit_ref: str | None = None) -> None:
    outcome = work_item.outcome
    outcome.phase = OutcomePhase.COMPLETED
    outcome.result = result
    outcome.result_summary = summary
    outcome.result_commit = commit_ref
    outcome.completed_at = _utc_now()
    self.session.add(OutcomeEvent(outcome=outcome, event_type="outcome_completed", payload={"result": result.value, "work_item_id": work_item.id}))
```

- [ ] **Step 5: Add lease extension support with bounded event history**

```python
def extend_lease(self, lease_id: int, worker_id: str, ttl_seconds: int, reason: str) -> Lease:
    lease = self.session.get(Lease, lease_id)
    ...
    lease.heartbeat_deadline = lease.heartbeat_deadline + timedelta(seconds=ttl_seconds)
    lease.expires_at = lease.heartbeat_deadline
    self.session.add(OutcomeEvent(outcome=lease.work_item.outcome, event_type="lease_extended", payload={"lease_id": lease.id, "worker_id": worker_id, "ttl_seconds": ttl_seconds, "reason": reason}))
```

- [ ] **Step 6: Run the control-plane and end-to-end tests to verify they pass**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_control_plane.py tests/test_end_to_end.py -q`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git -C /Users/marcus/Documents/Github/gws add gws/control_plane.py gws/models.py tests/test_control_plane.py tests/test_end_to_end.py
git -C /Users/marcus/Documents/Github/gws commit -m "refactor: complete outcomes explicitly"
```

### Task 4: Cut the API Over to WorkItem and Extension Routes

**Files:**
- Modify: `gws/contracts.py`
- Modify: `gws/api.py`
- Modify: `tests/test_api.py`
- Modify: `README.md`

- [ ] **Step 1: Write the failing API tests**

```python
def test_worker_can_complete_work_item(tmp_path, worker_registry_path):
    response = client.post(
        f"/worker/work-items/{work_item_id}/complete",
        json={"touched_paths": ["services/music/player.py"], "changed_hunks": ["+return play(queue)"]},
        headers=auth_headers("token-coder-1"),
    )
    assert response.status_code == 200
    assert response.json() == {"status": "processed"}


def test_worker_can_request_lease_extension(tmp_path, worker_registry_path):
    response = client.post(
        f"/worker/leases/{lease_id}/extend",
        json={"ttl_seconds": 120, "reason": "close to finishing validation"},
        headers=auth_headers("token-coder-1"),
    )
    assert response.status_code == 200
    assert "heartbeat_deadline" in response.json()
```

- [ ] **Step 2: Run the API tests to verify they fail**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_api.py -q`

Expected: FAIL because the new routes and request/response models do not exist.

- [ ] **Step 3: Replace step-centric contracts with work-item and lease-extension contracts**

```python
class WorkerLeaseExtensionRequest(BaseModel):
    ttl_seconds: int = 60
    reason: str


class WorkerLeaseResponse(BaseModel):
    lease_id: int
    work_item_id: int
    repo: str
    title: str
    goal: str
    work_type: str
```

- [ ] **Step 4: Rewrite `api.py` as a translation layer over coordinator/control-plane services**

```python
@app.post("/worker/lease", response_model=WorkerLeaseResponse)
def lease_work(...):
    work_item = _find_ready_work_item(...)
    if work_item is None and eligible_repo_heads:
        outcome, work_item = coordinator.plan_outcome(...)
    lease = service.issue_lease(work_item_id=work_item.id, worker_id=worker.worker_id, ttl_seconds=payload.ttl_seconds)
    return WorkerLeaseResponse(...)


@app.post("/worker/work-items/{work_item_id}/complete", response_model=WorkerCompletionResponse)
async def complete_work_item(...):
    return await _complete_work_item_for_worker(...)
```

- [ ] **Step 5: Update README and API tests to the final vocabulary**

```markdown
- `POST /worker/lease`
- `POST /worker/leases/{lease_id}/heartbeat`
- `POST /worker/leases/{lease_id}/extend`
- `POST /worker/work-items/{work_item_id}/complete`
```

- [ ] **Step 6: Run the API tests to verify they pass**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_api.py -q`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git -C /Users/marcus/Documents/Github/gws add gws/contracts.py gws/api.py tests/test_api.py README.md
git -C /Users/marcus/Documents/Github/gws commit -m "refactor: move worker API to work items"
```

### Task 5: Move Timeline and Read Models to Outcome-Centered History

**Files:**
- Modify: `gws/public_timeline.py`
- Modify: `tests/test_public_timeline_api.py`

- [ ] **Step 1: Write the failing timeline tests**

```python
def test_public_timeline_reports_explicit_outcome_result(session):
    payload = build_public_timeline(session, "intent-1")
    assert payload["timeline_events"][-1]["outcome"] == "succeeded"
    assert payload["now_building"]["lease_status"] == "idle"
```

- [ ] **Step 2: Run the timeline tests to verify they fail**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_public_timeline_api.py -q`

Expected: FAIL because the timeline still reads legacy step/verdict reconstruction paths.

- [ ] **Step 3: Rewrite the timeline read model around `Outcome`, `WorkItem`, and `OutcomeEvent`**

```python
def build_public_timeline(session: Session, intent_id: str) -> dict | None:
    outcome = (
        session.query(Outcome)
        .filter(Outcome.intent_id == intent_id)
        .order_by(Outcome.created_at.desc())
        .first()
    )
    if outcome is None:
        return None
    events = session.query(OutcomeEvent).filter_by(outcome_id=outcome.id).order_by(OutcomeEvent.created_at.asc()).all()
```

- [ ] **Step 4: Run the timeline tests to verify they pass**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_public_timeline_api.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C /Users/marcus/Documents/Github/gws add gws/public_timeline.py tests/test_public_timeline_api.py
git -C /Users/marcus/Documents/Github/gws commit -m "refactor: build public timeline from outcomes"
```

### Task 6: Retire Legacy Runtime Names and Finish the Cutover

**Files:**
- Modify: `gws/models.py`
- Modify: `gws/api.py`
- Modify: `gws/planner.py`
- Modify: `tests/test_models.py`
- Modify: `tests/test_planner.py`
- Modify: `tests/test_end_to_end.py`

- [ ] **Step 1: Write the failing cleanup tests**

```python
def test_legacy_pull_request_runtime_model_is_gone():
    from gws import models
    assert not hasattr(models, "PullRequest")
```

- [ ] **Step 2: Run the cleanup tests to verify they fail**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_models.py tests/test_planner.py tests/test_end_to_end.py -q`

Expected: FAIL because the old model and vocabulary are still present.

- [ ] **Step 3: Remove or alias legacy names only where migration requires it**

```python
# final target
class WorkItem(Base):
    __tablename__ = "work_items"
    ...

# no runtime use of PullRequest remains
```

- [ ] **Step 4: Update all remaining tests and imports to the final names**

```python
from gws.models import Outcome, PlanningSession, WorkItem, Lease, Attempt, Verdict
```

- [ ] **Step 5: Run the full suite**

Run: `cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git -C /Users/marcus/Documents/Github/gws add gws/models.py gws/api.py gws/planner.py tests/test_models.py tests/test_planner.py tests/test_end_to_end.py
git -C /Users/marcus/Documents/Github/gws commit -m "refactor: retire legacy planning runtime"
```

## Self-Review

### Spec coverage

- Explicit top-level durable result: covered by Task 1 and Task 3.
- Planning history separated from execution: covered by Task 2.
- Work item and lease model: covered by Task 3 and Task 4.
- Lease extension policy: covered by Task 3 and Task 4.
- Canonical event stream: covered by Task 2, Task 3, and Task 5.
- API cutover from `step` to `work_item`: covered by Task 4.
- Legacy runtime retirement: covered by Task 6.

### Placeholder scan

- No `TODO`, `TBD`, or deferred “write tests later” placeholders remain.
- Every task includes exact files, concrete tests, commands, and commit points.

### Type consistency

- Top-level durable record is consistently `Outcome`.
- Planning record is consistently `PlanningSession`.
- Executable unit is consistently `WorkItem`.
- History stream is consistently `OutcomeEvent`.
- Public execution route vocabulary consistently uses `work_item`.
