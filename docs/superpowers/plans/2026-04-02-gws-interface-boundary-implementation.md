# GWS Interface Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current mixed-auth, partially transitional `director`/`gws` seam with a small stable worker-scoped API and typed contracts.

**Architecture:** `gws` keeps its durable control-plane entities, but the public boundary is narrowed to `POST /intents` plus a worker-scoped execution namespace. Worker identity becomes fully server-derived from bearer tokens, planner output becomes typed, and `director` is updated to consume the new interface directly.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy, pytest, aiohttp

---

## File Structure

### `gws`

- Modify: `gws/config.py`
  - Add `policy_path` to explicit runtime settings.
- Create: `gws/contracts.py`
  - Hold typed planner and HTTP contract models.
- Modify: `gws/planner_client.py`
  - Return typed plan objects instead of raw mappings.
- Modify: `gws/planner.py`
  - Consume typed planner output and remove ad hoc dict validation.
- Modify: `gws/api.py`
  - Add worker-scoped endpoints and remove worker identity from request bodies.
- Modify: `tests/test_api.py`
  - Replace old worker execution route expectations with `/worker/*` route coverage.
- Modify: `tests/test_end_to_end.py`
  - Assert lease, heartbeat, and completion work through worker auth only.
- Modify: `tests/test_planner.py`
  - Validate typed planner contracts.
- Modify: `README.md`
  - Document the new stable API.

### `director`

- Modify: `/Users/marcus/Documents/Github/ystack/director/gws_client.py`
  - Rename worker-facing methods to the new seam and switch them to worker auth.
- Modify: `/Users/marcus/Documents/Github/ystack/director/agent_runner.py`
  - Stop sending `worker_id` in worker execution calls.
- Modify: `/Users/marcus/Documents/Github/ystack/director/tests/test_gws_client.py`
  - Assert the client targets `/worker/lease`, `/worker/leases/{lease_id}/heartbeat`, and `/worker/steps/{step_id}/complete`.

---

### Task 1: Add typed contracts and explicit runtime settings in `gws`

**Files:**
- Create: `gws/contracts.py`
- Modify: `gws/config.py`
- Modify: `gws/planner_client.py`
- Modify: `gws/planner.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing typed-planner tests**

Add these tests to `tests/test_planner.py`:

```python
from pydantic import ValidationError

from gws.contracts import SynthesizedPlan


def test_synthesized_plan_accepts_valid_payload():
    plan = SynthesizedPlan.model_validate(
        {
            "title": "Build player movement",
            "goal": "Implement movement controls",
            "repo": "repo-a",
            "allowed_paths": ["src/**"],
            "forbidden_paths": [],
            "step_type": "execute",
        }
    )

    assert plan.repo == "repo-a"
    assert plan.allowed_paths == ["src/**"]


def test_synthesized_plan_rejects_missing_required_keys():
    try:
        SynthesizedPlan.model_validate(
            {
                "title": "Build player movement",
                "repo": "repo-a",
            }
        )
    except ValidationError as exc:
        assert "goal" in str(exc)
        assert "allowed_paths" in str(exc)
        assert "forbidden_paths" in str(exc)
        assert "step_type" in str(exc)
    else:
        raise AssertionError("expected ValidationError")
```

- [ ] **Step 2: Run the planner test subset and verify it fails**

Run:

```bash
cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_planner.py -q
```

Expected:
- FAIL with `ModuleNotFoundError` or import failure for `gws.contracts`

- [ ] **Step 3: Add the typed contract models**

Create `gws/contracts.py`:

```python
from __future__ import annotations

from pydantic import BaseModel, Field


class SynthesizedPlan(BaseModel):
    title: str
    goal: str
    repo: str
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    step_type: str


class WorkerLeaseRequest(BaseModel):
    repo_heads: dict[str, str] = Field(default_factory=dict)
    intent_id: str | None = None
    ttl_seconds: int = 60


class WorkerLeaseResponse(BaseModel):
    lease_id: int
    step_id: int
    repo: str
    title: str
    goal: str
    step_type: str
    allowed_paths: list[str]
    forbidden_paths: list[str]
    base_commit: str | None = None
    artifact_requirements: list[str] = Field(default_factory=list)
    heartbeat_deadline: str


class WorkerHeartbeatRequest(BaseModel):
    ttl_seconds: int = 60


class WorkerHeartbeatResponse(BaseModel):
    lease_id: int
    heartbeat_deadline: str


class WorkerCompletionRequest(BaseModel):
    touched_paths: list[str]
    changed_hunks: list[str]


class WorkerCompletionResponse(BaseModel):
    status: str
```

- [ ] **Step 4: Route settings and planner interfaces through the typed contracts**

Update `gws/config.py`:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GWS_", extra="ignore")
    database_url: str = "sqlite+pysqlite:///:memory:"
    db_pool_size: int = 10
    db_pool_timeout: int = 30
    db_pool_pre_ping: bool = True
    api_key: Optional[str] = None
    workers_path: str = "workers.yaml"
    policy_path: str = "policy.yaml"
    planner_provider: Optional[str] = None
    planner_model: Optional[str] = None
    planner_api_key: Optional[str] = None
    planner_timeout: float = 60.0
    gateway_url: Optional[str] = None
```

Update `gws/planner_client.py`:

```python
from .contracts import SynthesizedPlan


class PlannerClient(Protocol):
    def synthesize(
        self,
        *,
        brief: str,
        lane: str,
        repo_heads: dict[str, str],
        envelope: dict,
        lane_capabilities: Optional[dict[str, str]] = None,
        intent_context: Optional[str] = None,
        planner_guidance: Optional[str] = None,
    ) -> SynthesizedPlan:
        ...
```

Update `gws/planner.py` to use `SynthesizedPlan` directly:

```python
from .contracts import SynthesizedPlan


plan = self.planner_client.synthesize(
    brief=active_intent.brief_text,
    lane=pull.lane,
    repo_heads=repo_heads,
    envelope=pull.envelope,
    lane_capabilities=self.lane_capabilities,
    intent_context=active_intent.context or None,
    planner_guidance=active_intent.planner_guidance or None,
)
selected_repo = plan.repo
if selected_repo not in repo_heads:
    raise ValueError(f"missing repo head for repo: {selected_repo}")
if selected_repo not in pull.repo_access_set:
    raise ValueError(f"repo {selected_repo} is not in pull request access set")

case = Case(
    intent_id=active_intent.intent_id,
    intent_version=active_intent.intent_version,
    title=plan.title,
    goal=plan.goal,
)
step = Step(
    case=case,
    repo=selected_repo,
    lane=pull.lane,
    step_type=plan.step_type,
    status=StepStatus.READY,
    allowed_paths=plan.allowed_paths,
    forbidden_paths=plan.forbidden_paths,
    base_commit=repo_heads[selected_repo],
)

pull.planning_result = plan.model_dump()
```

- [ ] **Step 5: Run the planner tests and verify they pass**

Run:

```bash
cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_planner.py -q
```

Expected:
- PASS

- [ ] **Step 6: Commit the typed-contract slice**

Run:

```bash
cd /Users/marcus/Documents/Github/gws && git add gws/contracts.py gws/config.py gws/planner_client.py gws/planner.py tests/test_planner.py && git commit -m "refactor: add typed GWS interface contracts"
```

---

### Task 2: Replace the worker execution API with a worker-scoped namespace

**Files:**
- Modify: `gws/api.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_end_to_end.py`
- Modify: `README.md`

- [ ] **Step 1: Write the failing worker-namespace API tests**

Add these tests to `tests/test_api.py`:

```python
def test_worker_lease_derives_identity_from_token(tmp_path, worker_registry_path):
    from gws.models import Case, IntentVersion, Step, StepStatus

    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief")
        case = Case(intent_id="intent-1", intent_version=1, title="Case", goal="Goal")
        step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
        session.add_all([intent, case, step])
        session.commit()

    client = TestClient(create_app(settings))
    response = client.post(
        "/worker/lease",
        json={"ttl_seconds": 60},
        headers=auth_headers("token-coder-1"),
    )

    assert response.status_code == 200
    assert response.json()["step_id"] == 1


def test_worker_heartbeat_rejects_non_owner(tmp_path, worker_registry_path):
    from gws.models import Case, IntentVersion, Step, StepStatus

    database_path = tmp_path / "api.db"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path}",
        workers_path=str(worker_registry_path),
    )
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        intent = IntentVersion(intent_id="intent-1", intent_version=1, brief_text="brief")
        case = Case(intent_id="intent-1", intent_version=1, title="Case", goal="Goal")
        step = Step(case=case, repo="repo-a", lane="coder", step_type="execute", status=StepStatus.READY)
        session.add_all([intent, case, step])
        session.commit()
        lease = ControlPlaneService(session).issue_lease(step.id, worker_id="coder-1", ttl_seconds=60)

    client = TestClient(create_app(settings))
    response = client.post(
        f"/worker/leases/{lease.id}/heartbeat",
        json={"ttl_seconds": 60},
        headers=auth_headers("token-security-1"),
    )

    assert response.status_code == 403
```

- [ ] **Step 2: Run the API test subset and verify it fails**

Run:

```bash
cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_api.py tests/test_end_to_end.py -q
```

Expected:
- FAIL because `/worker/lease` and `/worker/leases/...` do not exist yet

- [ ] **Step 3: Implement the worker-scoped API**

Update `gws/api.py` so the worker execution surface looks like this:

```python
from .contracts import (
    SynthesizedPlan,
    WorkerCompletionRequest,
    WorkerCompletionResponse,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerLeaseRequest,
    WorkerLeaseResponse,
)
```

Add these endpoint shapes:

```python
@app.post("/worker/lease", response_model=WorkerLeaseResponse)
def lease_work(
    payload: WorkerLeaseRequest,
    worker: WorkerIdentity = Depends(require_worker),
) -> WorkerLeaseResponse:
    with session_factory() as session:
        from .models import IntentVersion, PullRequest, Step, StepStatus

        step = (
            session.query(Step)
            .filter(Step.lane == worker.lane, Step.status == StepStatus.READY)
            .order_by(Step.id)
            .first()
        )

        if step is None and payload.repo_heads:
            intent_query = session.query(IntentVersion)
            if payload.intent_id:
                intent_query = intent_query.filter(IntentVersion.intent_id == payload.intent_id)
            intent = intent_query.order_by(IntentVersion.intent_version.desc(), IntentVersion.created_at.desc()).first()
            if intent is not None:
                planner_client = build_planner_client(settings)
                policy = PolicyEngine.from_file(settings.policy_path)
                planner_service = PlannerService(
                    session,
                    planner_client,
                    lane_capabilities=policy.lane_capabilities(),
                )
                pr = PullRequest(
                    worker_id=worker.worker_id,
                    lane=worker.lane,
                    intent_id=intent.intent_id,
                    repo_access_set=list(worker.repo_access_set),
                )
                session.add(pr)
                session.commit()
                session.refresh(pr)
                _case, step = planner_service.plan_pull_request(pr.id, payload.repo_heads)

        if step is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No eligible work")

        lease = ControlPlaneService(session).issue_lease(
            step_id=step.id,
            worker_id=worker.worker_id,
            ttl_seconds=payload.ttl_seconds,
        )
        session.refresh(step)
        return WorkerLeaseResponse(
            lease_id=lease.id,
            step_id=step.id,
            repo=step.repo,
            title=step.case.title,
            goal=step.case.goal,
            step_type=step.step_type,
            allowed_paths=list(step.allowed_paths),
            forbidden_paths=list(step.forbidden_paths),
            base_commit=step.base_commit,
            artifact_requirements=list(step.artifact_requirements),
            heartbeat_deadline=lease.heartbeat_deadline.isoformat() + "Z",
        )
```

```python
@app.post("/worker/leases/{lease_id}/heartbeat", response_model=WorkerHeartbeatResponse)
def heartbeat_worker_lease(
    lease_id: int,
    payload: WorkerHeartbeatRequest,
    worker: WorkerIdentity = Depends(require_worker),
) -> WorkerHeartbeatResponse:
    with session_factory() as session:
        lease = session.get(Lease, lease_id)
        if lease is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found")
        if lease.worker_id != worker.worker_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="lease belongs to another worker")
        lease = ControlPlaneService(session).heartbeat_lease(lease_id=lease_id, ttl_seconds=payload.ttl_seconds)
        return WorkerHeartbeatResponse(
            lease_id=lease.id,
            heartbeat_deadline=lease.heartbeat_deadline.isoformat() + "Z",
        )
```

```python
@app.post("/worker/steps/{step_id}/complete", response_model=WorkerCompletionResponse)
async def complete_worker_step(
    step_id: int,
    payload: WorkerCompletionRequest,
    worker: WorkerIdentity = Depends(require_worker),
) -> WorkerCompletionResponse:
    with session_factory() as session:
        service = ControlPlaneService(session)
        try:
            service.apply_completed_diff(
                step_id=step_id,
                worker_id=worker.worker_id,
                touched_paths=payload.touched_paths,
                changed_hunks=payload.changed_hunks,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValueError as exc:
            if str(exc) == f"unknown step_id: {step_id}":
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found") from exc
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        return WorkerCompletionResponse(status="processed")
```

Update `README.md` examples and endpoint descriptions to document the new worker namespace.

- [ ] **Step 4: Replace the old worker execution tests with the new seam assertions**

Update `tests/test_end_to_end.py` to call:

```python
response = client.post(
    "/worker/lease",
    json={"ttl_seconds": 60},
    headers=auth_headers("token-coder-1"),
)
```

and:

```python
response = client.post(
    f"/worker/steps/{step_id}/complete",
    json={
        "touched_paths": ["auth/session.py"],
        "changed_hunks": ["-issuer = 'internal'", "+issuer = 'https://sso.example.com'"],
    },
    headers=auth_headers("token-coder-1"),
)
```

Ensure at least one test asserts a different worker gets `403` on heartbeat and completion.

- [ ] **Step 5: Run the `gws` API and end-to-end suites and verify they pass**

Run:

```bash
cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest tests/test_api.py tests/test_end_to_end.py tests/test_planner.py -q
```

Expected:
- PASS

- [ ] **Step 6: Commit the API-boundary slice**

Run:

```bash
cd /Users/marcus/Documents/Github/gws && git add gws/api.py README.md tests/test_api.py tests/test_end_to_end.py tests/test_planner.py && git commit -m "refactor: add worker-scoped GWS API"
```

---

### Task 3: Update `director` to use the new stable seam

**Files:**
- Modify: `/Users/marcus/Documents/Github/ystack/director/gws_client.py`
- Modify: `/Users/marcus/Documents/Github/ystack/director/agent_runner.py`
- Modify: `/Users/marcus/Documents/Github/ystack/director/tests/test_gws_client.py`

- [ ] **Step 1: Write the failing `director` client tests**

Replace the worker-route assertions in `/Users/marcus/Documents/Github/ystack/director/tests/test_gws_client.py` with:

```python
@pytest.mark.asyncio
async def test_lease_work_calls_worker_namespace():
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "lease_id": 1,
            "step_id": 10,
            "title": "Build player movement",
            "goal": "WASD controls",
            "repo": "studio-ystackai",
            "step_type": "execute",
            "allowed_paths": ["src/**"],
            "forbidden_paths": [],
            "base_commit": "abc123",
            "artifact_requirements": [],
            "heartbeat_deadline": "2026-04-02T14:15:00Z",
        }
    )
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        client = GWSClient(base_url="http://gws:8000", worker_token="token-coder-1")
        await client.lease_work(repo_heads={"studio-ystackai": "abc123"})

    mock_session.post.assert_called_once()
    assert mock_session.post.call_args.args[0] == "http://gws:8000/worker/lease"
```

- [ ] **Step 2: Run the `director` client tests and verify they fail**

Run:

```bash
cd /Users/marcus/Documents/Github/ystack/director && python3 -m pytest tests/test_gws_client.py -q
```

Expected:
- FAIL because `lease_work`, `heartbeat_lease`, and `complete_step` do not exist yet

- [ ] **Step 3: Update the client to mirror the new seam**

Update `/Users/marcus/Documents/Github/ystack/director/gws_client.py`:

```python
class GWSClient:
    ...
    async def lease_work(
        self,
        *,
        ttl_seconds: int | None = None,
        intent_id: str | None = None,
        repo_heads: dict[str, str] | None = None,
    ) -> dict | None:
        session = await self._get_worker_session()
        payload: dict = {"ttl_seconds": ttl_seconds or GWS_PULL_TTL}
        if intent_id:
            payload["intent_id"] = intent_id
        if repo_heads:
            payload["repo_heads"] = repo_heads
        async with session.post(f"{self.base_url}/worker/lease", json=payload) as resp:
            if resp.status == 404:
                return None
            if resp.status >= 400:
                body = await resp.text()
                raise GWSError(resp.status, body)
            return await resp.json()

    async def heartbeat_lease(self, *, lease_id: int, ttl_seconds: int = 60) -> dict:
        session = await self._get_worker_session()
        async with session.post(
            f"{self.base_url}/worker/leases/{lease_id}/heartbeat",
            json={"ttl_seconds": ttl_seconds},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise GWSError(resp.status, body)
            return await resp.json()

    async def complete_step(
        self,
        *,
        step_id: int,
        touched_paths: list[str],
        changed_hunks: list[str],
    ) -> dict:
        session = await self._get_worker_session()
        async with session.post(
            f"{self.base_url}/worker/steps/{step_id}/complete",
            json={"touched_paths": touched_paths, "changed_hunks": changed_hunks},
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise GWSError(resp.status, body)
            return await resp.json()
```

Preserve `push_intent(...)` on the API-key session.

- [ ] **Step 4: Update the worker loop to use the new method names**

Update `/Users/marcus/Documents/Github/ystack/director/agent_runner.py`:

```python
step = await gws_client.lease_work(
    repo_heads=repo_heads or None,
)
...
await gws_client.heartbeat_lease(lease_id=lease_id)
...
await gws_client.complete_step(
    step_id=step_id,
    touched_paths=result["touched_paths"],
    changed_hunks=result["changed_hunks"],
)
```

Remove `worker_id=worker_id` from the client call site.

- [ ] **Step 5: Run the `director` seam tests and verify they pass**

Run:

```bash
cd /Users/marcus/Documents/Github/ystack/director && python3 -m pytest tests/test_gws_client.py tests/test_agent_runner.py -q
```

Expected:
- PASS

- [ ] **Step 6: Commit the `director` client slice**

Run:

```bash
cd /Users/marcus/Documents/Github/ystack/director && git add gws_client.py agent_runner.py tests/test_gws_client.py && git commit -m "refactor: align director with worker-scoped GWS API"
```

---

### Task 4: Full verification and integration cleanup

**Files:**
- Modify: `README.md` if any final wording is still stale
- Verify: `gws` full suite
- Verify: `director` relevant seam suite

- [ ] **Step 1: Run the full `gws` suite**

Run:

```bash
cd /Users/marcus/Documents/Github/gws && .venv/bin/pytest -q
```

Expected:
- PASS

- [ ] **Step 2: Run the relevant `director` seam suite**

Run:

```bash
cd /Users/marcus/Documents/Github/ystack/director && python3 -m pytest tests/test_gws_client.py tests/test_agent_runner.py -q
```

Expected:
- PASS

- [ ] **Step 3: Inspect both git worktrees**

Run:

```bash
cd /Users/marcus/Documents/Github/gws && git status --short
cd /Users/marcus/Documents/Github/ystack/director && git status --short
```

Expected:
- only intended tracked changes remain

- [ ] **Step 4: Push both repos**

Run:

```bash
cd /Users/marcus/Documents/Github/gws && git push origin main
cd /Users/marcus/Documents/Github/ystack/director && git push origin main
```

Expected:
- both pushes succeed

