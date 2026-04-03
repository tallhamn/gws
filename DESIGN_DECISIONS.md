# Design Decisions

This file captures the durable architectural choices behind the current GWS runtime.

## 1. End-State Driven, Just-In-Time Work Synthesis

GWS does not maintain a pre-expanded backlog of tickets. It holds a desired end state, observes the current repo state, and synthesizes the next bounded unit of work only when a worker is ready to take it.

## 2. Central Planning, Lease-Based Worker Pull

The control plane owns the world model and planning logic. Workers do not self-assign arbitrary work; they pull work for a lane and receive a time-bounded lease on a ready work item.

## 3. Immutable Intent Versions

Goals are versioned. Accepted amendments create a new `IntentVersion` instead of rewriting the brief in place. Open work is reconciled against the new version as `continue`, `revalidate`, `revoke`, or `superseded`.

## 4. Outcomes And Work Items

An intent version decomposes into durable `Outcome` records. Each outcome owns repo-scoped `WorkItem` records that are the actual leaseable execution units. This keeps planning, execution, and governance legible while preserving a clean separation between current state and event history.

## 5. Repo-Scoped Execution

Each work item executes against exactly one repo and one base commit. Cross-repo initiatives are handled by sequencing multiple repo-scoped work items rather than attempting atomic multi-repo execution.

## 6. Mostly Serial Workflows Within An Outcome

An outcome may grow dynamically, but its internal flow stays constrained and mostly serial. Parallelism comes primarily from multiple independent outcomes, not from turning each outcome into a general-purpose workflow DAG.

## 7. Deterministic Governance From Actual Diffs

Governance is derived from the actual change, not from model-claimed intent. Runtime routing decisions use touched paths and changed hunk text. Structural contract violations fail hard; review triggers append new work items in the appropriate lane.

## 8. Push Is Limited

The default model is worker pull. Push is reserved for orchestration and urgency, and only targets idle workers. General preemption is intentionally out of scope.

## 9. Provider-Agnostic Runtime Boundary

The control plane depends on a generic planner client boundary. Concrete backends live under `gws/providers/`. GWS is not built around any single model provider.

## 10. No Provider Plugin Framework Yet

The repo keeps one clean seam for planner adapters, but deliberately avoids registries, dynamic plugin loading, or provider capability frameworks. The current goal is a clean replacement boundary, not a platform for plugins.

## 11. Bounded And Unbounded Intents

Not all work converges to a discrete end state. A convergent intent ("add user authentication") has a natural completion point — the planner sees the finished code and returns `SATISFIED`. An unbounded intent ("build and polish a platformer until it's amazing") never satisfies — the planner always finds something to improve.

GWS handles both without special-casing. The planner evaluates the current repo state against the intent brief on every planning cycle. Convergent intents terminate automatically via `SATISFIED`. Unbounded intents keep producing work items until an external signal stops them — a human calls `POST /intents/{id}/complete`, an amendment redirects the goal, or workers simply stop polling (budget or time exhausted).

The intent controls whether work is bounded or unbounded. The control plane doesn't need to know the difference.
