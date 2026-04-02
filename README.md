# Governed Work Synthesis

GWS is a control plane for mostly unattended software delivery. It holds a desired end state, observes current repo state, and synthesizes the next bounded unit of work only when a worker is ready to take it.

At the macro level, GWS does pathfinding: choose the next waypoint toward the goal. At the micro level, workers do their own local planning to execute that waypoint. After each result is merged or rejected, GWS reevaluates from the new state and chooses the next step.

## What GWS Is Not

- Not a static backlog or sprint planner for agents.
- Not just a coding agent loop with better prompting.
- Not a general-purpose workflow engine; execution stays constrained and governance stays explicit.

## Current Status

The active runtime lives in `gws/`. Provider-specific planner adapters live under `gws/providers/`, while the control-plane surface stays generic. The durable architecture choices for the current system are captured in `DESIGN_DECISIONS.md`.

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

Worker identities for authenticated execution live in `workers.yaml`. Each entry maps a bearer token to a worker ID, lane, and repository access set.

The public execution contract is:

- `POST /intents`
  - control-plane authenticated
  - creates a new immutable intent version
- `POST /worker/lease`
  - worker-token authenticated
  - leases ready work or triggers JIT planning when repo heads are available
- `POST /worker/leases/{lease_id}/heartbeat`
  - worker-token authenticated
  - extends a lease owned by the authenticated worker
- `POST /worker/steps/{step_id}/complete`
  - worker-token authenticated
  - submits result metadata for the step leased to the authenticated worker

`policy.yaml` defines lane capabilities and governance triggers. `workers.yaml` defines worker identities. Both are runtime-configurable through `GWS_POLICY_PATH` and `GWS_WORKERS_PATH`.

## Repository Layout

- `gws/` active control-plane runtime
- `gws/providers/` explicit planner adapters
- `tests/` runtime test suite
- `policy.yaml` example governance policy
- `DESIGN_DECISIONS.md` architecture record
