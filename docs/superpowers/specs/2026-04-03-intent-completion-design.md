# Intent Completion Design

## Problem

When a worker polls for work and no ready WorkItem exists, JIT planning always synthesizes a new Outcome. The planner receives the intent brief and repo heads but has no mechanism to signal "the intent is already satisfied." This means GWS will keep creating outcomes indefinitely.

## Design

### Data Model

Add `IntentStatus` enum and a `status` column to `IntentVersion`:

```python
class IntentStatus(str, enum.Enum):
    ACTIVE = "active"
    SATISFIED = "satisfied"
```

The column defaults to `active`. New intent versions (via amendment or `POST /intents`) always start as `active`, which implicitly reopens the intent. The latest version's status is the authoritative answer for whether the intent is satisfied.

### Planner Boundary

Add `PlannerResult` enum to `contracts.py`:

```python
class PlannerResult(str, enum.Enum):
    SATISFIED = "satisfied"
```

`PlannerClient.synthesize()` return type changes from `SynthesizedPlan` to `SynthesizedPlan | PlannerResult`.

The LLM planner prompt is updated to include the `SATISFIED` option: the planner should return the exact string `SATISFIED` instead of a JSON plan when the current repo state already fulfills the intent brief.

Parsing lives in `parse_synthesized_plan_text` in `providers/common.py`: if the stripped response is `SATISFIED`, return `PlannerResult.SATISFIED`; otherwise parse as JSON. No changes needed to individual provider adapters since they all route through this function.

### Coordinator

`PlannerService.materialize_plan()` handles the new return type. If the planner returns `SATISFIED`:

- Sets `intent.status = IntentStatus.SATISFIED` on the current intent version
- Marks the planning session as succeeded with a note in `plan_payload`
- Deletes the empty speculative Outcome (it never left `planning` phase and has no work items — keeping it would be confusing noise; the PlanningSession already records that planning was attempted)

If the planner returns a `SynthesizedPlan`: existing behavior unchanged.

### JIT Planning Short-Circuit

`_jit_plan_work_item` in `api.py` adds an early check before acquiring the planning lock:

```python
if intent.status == IntentStatus.SATISFIED:
    return None
```

Satisfied intents are cheap — a single column check on the already-fetched intent row.

### Manual Completion Endpoint

`POST /intents/{intent_id}/complete` sets `status = IntentStatus.SATISFIED` on the latest intent version. Returns the updated intent version number. No planner involved.

### Migration

Add `status` column to `intent_versions` table with `server_default="active"`. Existing rows are all active, which is correct.

## Files Affected

- `gws/models.py` — `IntentStatus` enum, `IntentVersion.status` column
- `gws/contracts.py` — `PlannerResult` enum
- `gws/planner_client.py` — updated `synthesize` return type
- `gws/providers/common.py` — `parse_synthesized_plan_text` handles `SATISFIED`
- `gws/planner.py` — `materialize_plan` handles `PlannerResult.SATISFIED`
- `gws/coordinator.py` — cleanup of empty outcome on satisfaction
- `gws/api.py` — JIT short-circuit + `POST /intents/{intent_id}/complete`
- `alembic/versions/` — migration for new column
- `tests/` — test coverage for all paths
