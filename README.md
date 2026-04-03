[![CI](https://github.com/tallhamn/gws/actions/workflows/ci.yml/badge.svg)](https://github.com/tallhamn/gws/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/tallhamn/gws/graph/badge.svg)](https://codecov.io/gh/tallhamn/gws)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)

# Governed Work Synthesis

GWS is a control plane for mostly unattended software delivery. It holds a desired end state, observes current repo state, and synthesizes the next bounded unit of work only when a worker is ready to take it.

At the macro level, GWS does pathfinding: choose the next outcome toward the goal. At the micro level, workers do their own local planning to execute the current work item. After each result is merged or rejected, GWS reevaluates from the new state and chooses the next work item.

## What GWS Is Not

- Not a static backlog or sprint planner for agents.
- Not just a coding agent loop with better prompting.
- Not a general-purpose workflow engine; execution stays constrained and governance stays explicit.

## Architecture

```
                          ┌──────────────────┐
                          │  IntentVersion   │
                          │                  │  Immutable, versioned desired end state.
                          └────────┬─────────┘  Amendments create new versions, never
                                   │ 1:N        rewrite existing ones.
                          ┌────────▼─────────┐
                          │     Outcome      │  Durable unit of progress. Tracks phase:
                          │                  │  planning → ready → running → completed.
                          └──┬─────┬──────┬──┘
                             │     │      │
          ┌──────────────────┘     │      └──────────────────┐
          │ 1:N                    │ 1:N                     │ 1:N
          │                        │                         │
  ┌───────▼────────┐      ┌───────▼────────┐      ┌────���────▼───────┐
  │    Planning    │      │   WorkItem     │      │  OutcomeEvent   │
  │    Session     │─────>│                │─────>│                 │
  │                │      │                │      │                 │
  └────────────────┘      └───────┬────────┘      └─────────────────┘
   Calls the planner,      Leaseable unit of       Append-only audit log.
   synthesizes the first   execution. Scoped to    Records milestones from
   WorkItem + Outcome      one repo, one lane.     planning, leasing, and
   fields.                 Mostly serial within     governance decisions.
                           an outcome.
                                  │
                                  │ worker pulls
                                  │
                  ┌ ─ ─ ─ ─ ─ ─ ─▼─ ─ ─ ─ ─ ─ ─ ─ ┐
                  │         EXECUTION CYCLE           │
                  │                                   │
                  │       ┌───────────────┐           │
                  │       │     Lease     │           │
                  │       └───────┬───────┘           │
                  │               │ worker submits    │
                  │       ┌───────▼───────┐           │
                  │       │    Attempt    │           │
                  │       └───────┬───────┘           │
                  │               │                   │
                  │ ╔═════════════▼═════════════════╗ │
                  │ ║        GOVERNANCE             ║ │
                  │ ║                               ║ │
                  │ ║ Evaluates the actual diff,    ║ │
                  │ ║ not the worker's claimed      ║ │
                  │ ║ intent.                       ║ │
                  │ ║                               ║ │
                  │ ║ • Path triggers (auth/**)     ║ │
                  │ ║ • Content triggers ("jwt")    ║ │
                  │ ╚═════════════╤═════════════════╝ │
                  │               │                   │
                  │       ┌───────▼───────┐           │
                  │       │    Verdict    │           │
                  │       └───────┬───────┘           │
                  │               │                   │
                  └ ─ ─ ─ ─ ─ ─ ─┼─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
           pass              fail_and_        append_governance_step
        outcome done          replan          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
                           outcome failed
                                              Appends a new review  │
                                              WorkItem (e.g. in the
                                              security-review lane) │
                                              to the same Outcome.
                                              That WorkItem goes    │
                                              through the same
                                              execution cycle ──────┘
```

**Execution cycle:** A worker requests work → GWS either finds a ready WorkItem or JIT-plans a new Outcome → issues a Lease → worker executes and submits an Attempt → governance evaluates the actual diff against policy → the Verdict determines whether the outcome completes, fails, or grows a review step. Review steps go through the same cycle, so governance is re-entrant — a chain of reviews can form if policy requires it.

## Core Concepts

### Intent

The desired end state. An intent is a brief describing what should be true when the work is done, plus optional context and planner guidance. Intents are **immutable and versioned** — changes create a new `IntentVersion` rather than editing in place. This lets open work be reconciled against the new version.

### Outcome

A durable unit of progress toward an intent. When a worker asks for work, GWS plans the next **outcome** — a scoped goal with a title, a target repo, and one or more work items. An outcome tracks its own lifecycle through phases: `planning` → `ready` → `running` → `completed`, with a final result of `succeeded`, `failed`, `superseded`, or `abandoned`.

### Work Item

The actual leaseable execution unit. Each work item belongs to one outcome, targets one repo, and runs in one **lane**. Work items carry scope constraints (`allowed_paths`, `forbidden_paths`) and a `work_type` that tells the worker what kind of task it is. Work items within an outcome are mostly serial — each can be `blocked_by` a predecessor.

**Status lifecycle:** `ready` → `leased` → `running` → `verifying` → `succeeded` or `failed` or `revoked`.

### Lane

A capability channel. Each lane represents a distinct type of worker (e.g. `coder`, `artist`, `security-review`, `ci`, `merge`). Workers authenticate into a specific lane and can only pull work items assigned to that lane. Lanes are defined in `policy.yaml` with optional TTL limits and capability descriptions used during planning.

### Lease

A time-bounded claim on a work item. When a worker pulls work, GWS issues a lease with a `heartbeat_deadline`. The worker must periodically heartbeat to keep the lease alive. If the deadline passes without a heartbeat, GWS expires the lease and returns the work item to `ready` so another worker can pick it up. This prevents orphaned work when workers crash or disconnect.

### Attempt and Verdict

When a worker completes a work item, it submits an **attempt** containing the paths it touched and the changed hunks. GWS runs **governance** against the attempt — checking touched paths against policy triggers. Each governance check produces a **verdict**: `pass`, `fail_and_replan`, `append_governance_step` (e.g. trigger a security review), `quarantine`, or `superseded`.

### Governance

Policy-driven verification of actual changes, not claimed intent. Governance uses two trigger types defined in `policy.yaml`:

- **Path triggers** — if the worker touched files matching certain glob patterns (e.g. `auth/**`, `migrations/**`), route the result through a review lane.
- **Content triggers** — if changed hunks contain specific strings (e.g. `jwt`, `oauth`, `kms:`), route through a review lane.

When a trigger fires, GWS appends a new work item in the triggered lane (e.g. `security-review`) to the outcome. This means governance is not a gate that blocks — it extends the workflow dynamically.

### Amendment

A proposal to change an intent while work is in flight. Amendments reference a base intent version and provide an updated brief. When accepted, a new `IntentVersion` is created with the amendment folded in.

## Running Locally

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/uvicorn gws.api:create_app --factory --reload
```

Install a concrete planner backend only if you want to use one:

```bash
.venv/bin/pip install -e ".[anthropic]"
```

Planner selection prefers local Claude Code when `claude` is installed. That path uses your existing Claude Code login/session and defaults to `--effort max` for coding work. If you want the direct Anthropic API instead, set `GWS_PLANNER_PROVIDER=anthropic` and provide `GWS_PLANNER_API_KEY`. You can also force Claude Code explicitly with `GWS_PLANNER_PROVIDER=claude_code`.

Worker identities for authenticated execution live in `workers.yaml`. Each entry maps a bearer token to a worker ID, lane, and repository access set. `policy.yaml` defines lane capabilities and governance triggers. Both are runtime-configurable through `GWS_POLICY_PATH` and `GWS_WORKERS_PATH`.

## API Reference

All endpoints return JSON. Authentication is via `Authorization: Bearer <token>` header. Control-plane endpoints use the API key (`GWS_API_KEY`). Worker endpoints use worker tokens from `workers.yaml`.

### Control Plane

#### `POST /intents`

Create a new immutable intent version. If the intent ID already exists, increments the version number.

| Field | Type | Required | Description |
|---|---|---|---|
| `intent_id` | string | yes | Stable identifier for the intent |
| `brief_text` | string | yes | What should be true when the work is done |
| `context` | string | no | Additional background for the planner |
| `planner_guidance` | string | no | Hints for how the planner should approach the work |

**Response** `201`: `{ "intent_id": "...", "intent_version": 1 }`

#### `GET /intents/{intent_id}`

Returns the latest version of the given intent.

**Response** `200`: `{ "intent_id", "intent_version", "brief_text", "context", "planner_guidance" }`

#### `POST /intents/{intent_id}/complete`

Manually mark an intent as satisfied. The planner will no longer synthesize new outcomes for this intent. Creating a new intent version (via `POST /intents` or amendment acceptance) resets the status to `active`.

**Response** `200`: `{ "intent_id": "...", "intent_version": 1, "status": "satisfied" }`
**Response** `404`: Intent not found.

#### `POST /leases/expire`

Expires all leases past their heartbeat deadline. Returns work items to `ready`. Intended to be called on a timer.

**Response** `200`: `{ "expired_count": 0 }`

#### `GET /public/intents/{intent_id}/timeline`

Public (unauthenticated) timeline of all outcomes and work items for an intent. Useful for dashboards.

### Worker

All worker endpoints require a valid worker token. The worker's lane and repo access are determined by the token, not the request body.

#### `POST /worker/lease`

Pull the next available work item for this worker's lane and repos. If no `ready` work item exists and `repo_heads` are provided, JIT-plans a new outcome.

| Field | Type | Required | Description |
|---|---|---|---|
| `repo_heads` | object | no | Map of repo name → current commit SHA. Enables JIT planning against current state. |
| `intent_id` | string | no | Scope to a specific intent. Defaults to the most recent intent. |
| `ttl_seconds` | int | no | Lease duration (default 60). Worker must heartbeat before this expires. |

**Response** `200`:
```json
{
  "lease_id": 1,
  "work_item_id": 1,
  "repo": "my-repo",
  "title": "Outcome title",
  "goal": "What the outcome should achieve",
  "description": "Per-item context for the worker",
  "work_type": "implement",
  "allowed_paths": ["src/**"],
  "forbidden_paths": [],
  "base_commit": "abc123",
  "artifact_requirements": [],
  "heartbeat_deadline": "2026-04-02T12:01:00"
}
```

**Response** `404`: No eligible work available.
**Response** `503`: JIT planning failed (planner unavailable).

#### `POST /worker/leases/{lease_id}/heartbeat`

Extend the heartbeat deadline on an active lease. Must be called before the current deadline expires.

| Field | Type | Required | Description |
|---|---|---|---|
| `ttl_seconds` | int | no | New TTL from now (default 60) |

**Response** `200`: `{ "lease_id": 1, "heartbeat_deadline": "..." }`

#### `POST /worker/leases/{lease_id}/extend`

Request a lease extension with a reason. Subject to policy TTL limits.

| Field | Type | Required | Description |
|---|---|---|---|
| `ttl_seconds` | int | no | Requested extension (default 60) |
| `reason` | string | yes | Why the extension is needed |

**Response** `200`: `{ "lease_id": 1, "heartbeat_deadline": "..." }`

#### `POST /worker/work-items/{work_item_id}/complete`

Submit completion results for a leased work item. Triggers governance verification.

| Field | Type | Required | Description |
|---|---|---|---|
| `touched_paths` | string[] | yes | All file paths the worker modified |
| `changed_hunks` | string[] | yes | Raw diff hunks of the changes |

**Response** `200`: `{ "status": "processed" }`
**Response** `400`: Lease expired or work item not in a completable state.
**Response** `403`: Work item is leased to a different worker.

#### `GET /healthz`

Unauthenticated health check. Returns `{ "status": "ok" }`.

## Current Status

The active runtime lives in `gws/`. Provider-specific planner adapters live under `gws/providers/`, while the control-plane surface stays generic. The durable architecture choices for the current system are captured in `DESIGN_DECISIONS.md`.

## Repository Layout

- `gws/` active control-plane runtime
- `gws/providers/` explicit planner adapters
- `tests/` runtime test suite
- `policy.yaml` example governance policy
- `workers.yaml` example worker registry
- `DESIGN_DECISIONS.md` architecture record
