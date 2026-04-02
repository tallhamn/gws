# Design Decisions

This file captures the durable architectural choices behind the current GWS runtime.

## 1. End-State Driven, Just-In-Time Work Synthesis

GWS does not maintain a pre-expanded backlog of tickets. It holds a desired end state, observes the current repo state, and synthesizes the next bounded unit of work only when a worker is ready to take it.

## 2. Central Planning, Lease-Based Worker Pull

The control plane owns the world model and planning logic. Workers do not self-assign arbitrary work; they pull work for a lane and receive a time-bounded lease on a ready step.

## 3. Immutable Intent Versions

Goals are versioned. Accepted amendments create a new `IntentVersion` instead of rewriting the brief in place. Open work is reconciled against the new version as `continue`, `revalidate`, `revoke`, or `superseded`.

## 4. Cases And Steps

An intent decomposes into durable `Case` objects. Each case contains repo-scoped `Step` objects that are the actual leaseable execution units. This keeps planning, execution, and governance legible.

## 5. Repo-Scoped Execution

Each step executes against exactly one repo and one base commit. Cross-repo initiatives are handled by sequencing multiple repo-scoped steps rather than attempting atomic multi-repo execution.

## 6. Mostly Serial Workflows Within A Case

A case may grow dynamically, but its internal flow stays constrained and mostly serial. Parallelism comes primarily from multiple independent cases, not from turning each case into a general-purpose workflow DAG.

## 7. Deterministic Governance From Actual Diffs

Governance is derived from the actual change, not from model-claimed intent. Runtime routing decisions use touched paths and changed hunk text. Structural contract violations fail hard; review triggers append new steps in the appropriate lane.

## 8. Push Is Limited

The default model is worker pull. Push is reserved for orchestration and urgency, and only targets idle workers. General preemption is intentionally out of scope.

## 9. Provider-Agnostic Runtime Boundary

The control plane depends on a generic planner client boundary. Concrete backends live under `gws/providers/`. GWS is not built around any single model provider.

## 10. No Provider Plugin Framework Yet

The repo keeps one clean seam for planner adapters, but deliberately avoids registries, dynamic plugin loading, or provider capability frameworks. The current goal is a clean replacement boundary, not a platform for plugins.
