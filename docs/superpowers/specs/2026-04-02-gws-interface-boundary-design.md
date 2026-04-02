# GWS Interface And Director Boundary Design

**Date:** 2026-04-02

## Goal

Make the `gws` public interface small, stable, typed, and operationally honest.

`gws` should read as a real control plane with a clean worker-facing API, not as a half-internal service that leaks older abstractions into the client boundary. `director` should interact with `gws` as a scheduler/executor consuming governed work, not as a co-owner of `gws` internals.

## Current Problems

### 1. The worker boundary has two trust models

Today, worker execution flows are split across two authentication patterns:

- some requests are authenticated with the global API key
- some requests are authenticated with worker bearer tokens
- some requests still include `worker_id` in the request body

That makes the interface feel transitional. The server is not consistently deriving worker identity from the authenticated caller.

### 2. The public API exposes an older abstraction

`PullRequest` is still part of the public API surface even though the active `director` integration is already centered on:

- creating intents
- pulling work
- heartbeating leases
- completing steps

This makes the API look broader and less intentional than the actual seam.

### 3. Planner output is stringly-typed

Planner synthesis currently returns a raw mapping that is validated by checking for required keys. This works, but it is not a strong or elegant contract. It weakens refactors, makes errors noisier, and leaks schema concerns into call sites.

### 4. Runtime configuration leaks through cwd assumptions

Files like `policy.yaml` and `workers.yaml` are part of runtime behavior, but not all of them are consistently accessed through explicit settings. That makes embedding, deployment, and testing less crisp than they should be.

## Desired System Shape

### Responsibility Split

`gws` owns:

- immutable intent versions
- planning
- governed step creation
- worker identity validation
- lease issuance and expiry
- diff-based governance
- artifact verification

`director` owns:

- worker lifecycle
- compute budget
- polling cadence
- repo execution
- result collection
- publishing and site operations

This means:

- `gws` does not know or care how much budget `director` has
- `gws` does not push work to workers
- `director` requests work only when it has real capacity to spend
- `director` does not provide spoofable worker identity in request bodies

This is the key design principle: `director` is the scheduler/executor, `gws` is the governor/planner.

## Public API Design

The stable public client contract should be reduced to one control-plane write endpoint plus a worker-scoped execution namespace.

### 1. `POST /intents`

Purpose:
- create a new immutable intent version

Authentication:
- control-plane authentication
- current API key mechanism is acceptable here

Request body:

```json
{
  "intent_id": "studio-ystackai-drop",
  "brief_text": "Build a browser demo crew homepage refresh.",
  "context": "Public homepage and crew pages only.",
  "planner_guidance": "Preserve the current studio visual language."
}
```

Response body:

```json
{
  "intent_id": "studio-ystackai-drop",
  "intent_version": 3
}
```

### 2. `POST /worker/lease`

Purpose:
- allow an authenticated worker to request one governed unit of work

Authentication:
- worker bearer token only

Server-derived fields:
- `worker_id`
- `lane`
- `repo_access_set`

Request body:

```json
{
  "repo_heads": {
    "studio-ystackai": "abc123"
  },
  "intent_id": "studio-ystackai-drop",
  "ttl_seconds": 900
}
```

All fields are optional. `repo_heads` is provided when the caller wants JIT planning against current repo state. `intent_id` is optional and allows the caller to scope planning to a specific intent; otherwise `gws` may use the most recent applicable intent. `ttl_seconds` overrides the default lease duration for that request.

Behavior:

- if a ready step already exists for the worker lane, lease it
- otherwise, if `repo_heads` are present, `gws` may JIT-plan the next eligible step
- if no eligible work exists, return `404`

Response body:

```json
{
  "lease_id": 41,
  "step_id": 88,
  "repo": "studio-ystackai",
  "title": "Implement homepage hero layout",
  "goal": "Restore the classic homepage structure and styling hooks.",
  "step_type": "execute",
  "allowed_paths": ["src/**", "public/**"],
  "forbidden_paths": ["infra/**"],
  "base_commit": "abc123",
  "artifact_requirements": [],
  "heartbeat_deadline": "2026-04-02T14:15:00Z"
}
```

### 3. `POST /worker/leases/{lease_id}/heartbeat`

Purpose:
- extend the active lease for the authenticated worker

Authentication:
- worker bearer token only

Request body:

```json
{
  "ttl_seconds": 60
}
```

Response body:

```json
{
  "lease_id": 41,
  "heartbeat_deadline": "2026-04-02T14:16:00Z"
}
```

Rules:

- the lease must belong to the authenticated worker
- expired or unknown leases fail
- one worker must never be able to extend another worker’s lease

### 4. `POST /worker/steps/{step_id}/complete`

Purpose:
- submit the result of the active leased step for the authenticated worker

Authentication:
- worker bearer token only

Request body:

```json
{
  "touched_paths": ["src/homepage.tsx", "public/platform.css"],
  "changed_hunks": ["-old text", "+new text"]
}
```

Response body:

```json
{
  "status": "processed"
}
```

Rules:

- the step must have an active lease owned by the authenticated worker
- governance is derived from the submitted diff metadata
- artifact verification is part of completion, not a separate client-visible phase
- a worker must never be able to complete another worker’s step

## API Surface To Remove From The Public Contract

The following should no longer define the `director`/`gws` seam:

- `worker_id` in execution request bodies
- `lane` in execution request bodies where the lane is already part of worker identity
- public dependence on the `pull-requests` API for worker execution
- cwd-relative configuration assumptions
- dict-shaped planner output as the boundary contract

`PullRequest` may remain as an internal persistence artifact if it is useful to the planner implementation, but it should no longer be the conceptual center of the public interface.

## Typed Contracts

### Planner Output

Planner synthesis should return a typed plan model, not an unstructured `dict`.

Required fields:

- `title`
- `goal`
- `repo`
- `allowed_paths`
- `forbidden_paths`
- `step_type`

This model should be validated at the seam where the provider adapter returns, so downstream code only handles valid plans.

### Worker API Models

The HTTP layer should use explicit request and response models for:

- intent creation
- worker lease request
- worker lease response
- worker heartbeat request
- worker heartbeat response
- worker completion request
- worker completion response

That makes the boundary self-documenting and keeps server logic out of ad hoc mapping validation.

## Configuration Design

All runtime file dependencies should be resolved through settings, not hardcoded filenames.

`Settings` should explicitly include at least:

- `workers_path`
- `policy_path`
- `database_url`
- `api_key`
- planner provider/model/key/timeout
- `gateway_url`

JIT planning and artifact verification should read configuration through `Settings`, so embedding and deployment are deterministic and not dependent on the current working directory.

## Director Integration Design

`director` should talk to `gws` through a small client that mirrors the stable API:

- `push_intent(...)`
- `lease_work(...)`
- `heartbeat_lease(...)`
- `complete_step(...)`

Worker loops should instantiate a `GWSClient` bound to the worker token for that worker. The control-plane path that creates intents may continue to use API-key auth.

`director` should not send:

- `worker_id` in request bodies
- lane information that can be derived from worker auth
- planning metadata other than current repo heads and optional intent targeting

The worker loop remains responsible for deciding when to call `lease_work`, based on local budget and availability.

## Migration Plan

This is a deliberate breaking change.

### Server

- add the new worker-scoped endpoints
- move endpoint behavior to server-derived worker identity
- move policy file resolution behind `Settings`
- validate planner output through typed models
- remove old worker-facing route usage from tests and docs

### Director

- update `gws_client.py` to the new endpoint names and auth model
- update worker loops to use worker-token-bound clients
- stop sending `worker_id` in request bodies
- keep intent creation on the control-plane auth path

### Compatibility

No compatibility layer is required. The goal is a cleaner seam, not a prolonged transition window.

`gws` and `director` should be updated together and deployed together.

## Testing Strategy

### `gws`

Keep the full suite green and add explicit coverage for the new boundary:

- worker token can lease work without providing `worker_id`
- worker token cannot heartbeat another worker’s lease
- worker token cannot complete another worker’s step
- JIT planning respects authenticated worker lane and repo access
- typed planner output validation rejects malformed provider responses
- configured `policy_path` is used instead of cwd assumptions

### `director`

Add or update client and integration tests to prove:

- the client calls `/worker/lease`
- the client calls `/worker/leases/{lease_id}/heartbeat`
- the client calls `/worker/steps/{step_id}/complete`
- worker execution calls use worker auth, not the global API key
- intent creation remains on the control-plane auth path

## Non-Goals

This design does not:

- change the durable core entities in `gws`
- move worker scheduling or compute-budget logic into `gws`
- introduce push-based orchestration
- generalize into a plugin platform
- redesign the planner itself beyond tightening its contract

## Result

If implemented correctly, the `gws` boundary should feel like this:

- one control-plane write for new intents
- one worker-scoped namespace for execution
- server-owned worker identity
- typed contracts at every seam
- no old abstractions leaking into the client

That is the standard for “beautiful” here: small, legible, difficult to misuse, and honest about where responsibility lives.
