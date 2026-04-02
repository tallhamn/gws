# GWS Work Item Cutover Design

## Summary

Retire the legacy `Case` / `Step` execution model and complete the cutover to the `Outcome` / `WorkItem` runtime.

This pass is intentionally breaking. It removes transitional dual-model support, renames remaining planner vocabulary from `step_type` to `work_type`, and leaves GWS with one coherent execution model across schema, services, API surfaces, tests, and live documentation.

`Outcome` remains the core mutable aggregate. `OutcomeEvent` remains the append-only history for that aggregate. `PlanningSession` remains the durable record of a single planning attempt.

## Problem

GWS currently carries two competing execution vocabularies:

- the new model: `Outcome`, `WorkItem`, `PlanningSession`, `OutcomeEvent`
- the legacy model: `Case`, `Step`, `step_type`, `step_id`, and step-scoped service branches

That split causes several problems:

- the schema still permits two kinds of lease and attempt targets
- runtime services still have legacy branches and error paths
- amendments still revoke `Step` records instead of canonical `WorkItem`s
- planner contracts still ask for `step_type` even though they materialize `WorkItem`s
- docs describe multiple models as if they are equally current

This is not a safe long-term transition state. It makes the code harder to reason about and forces every new feature to decide which model is real.

## Goals

- Make `Outcome` / `WorkItem` the only execution model in GWS.
- Remove `Case`, `Step`, `step_type`, and `step_id` from live code and schema.
- Keep `Outcome` as the mutable current-state aggregate.
- Keep `OutcomeEvent` as the append-only audit and timeline history.
- Keep `PlanningSession` as the durable planning-attempt record.
- Make worker-facing and planner-facing vocabulary consistently use `work_item` and `work_type`.
- Update public timeline derivation to depend only on the canonical model.
- Leave the repository documentation aligned with the resulting runtime.

## Non-Goals

- No compatibility bridge for old `step`-based clients.
- No dual-read or dual-write period.
- No rename of `Outcome` to another noun in this pass.
- No redesign of unrelated product surfaces outside the documentation updates needed to describe the new model correctly.

## Domain Model

### IntentVersion

`IntentVersion` continues to define what the system is trying to build at a given version of an intent.

It remains the anchor for:

- `brief_text`
- `context`
- `planner_guidance`
- accepted amendment history

### Outcome

`Outcome` is the mutable aggregate that represents one concrete track of work for an `IntentVersion`.

It owns:

- current lifecycle phase
- final result, when known
- selected repo
- the currently active `WorkItem`
- result summary and terminal timestamps

`Outcome` answers the question: "where are we now?"

### OutcomeEvent

`OutcomeEvent` is append-only history attached to an `Outcome`.

It records milestones such as:

- planning started
- planning succeeded
- planning failed
- governance work items appended
- outcome completed

`OutcomeEvent` answers the question: "how did we get here?"

### PlanningSession

`PlanningSession` remains a durable audit record for one planning attempt against an `Outcome`.

It stores:

- planner identity (`planner_provider`, `planner_model`)
- invoking worker and lane
- eligible repos and repo heads
- planning context
- returned plan payload
- terminal planning status and any error detail

This preserves planning reproducibility and failure diagnostics without polluting the core `Outcome` state.

### WorkItem

`WorkItem` is the only leaseable execution unit.

It owns:

- repo
- lane
- `work_type`
- filesystem guardrails
- base commit and artifact requirements
- dependency on another work item within the same outcome
- execution status

Any service that issues leases, records attempts, or applies governance decisions works only through `WorkItem`.

## Schema Changes

This pass includes a breaking migration that removes the old model instead of adapting around it.

### Remove Legacy Tables

Drop:

- `cases`
- `steps`

These tables are no longer part of the live domain model.

### Simplify Lease Targeting

`leases` currently supports either `step_id` or `work_item_id`.

After the cutover:

- remove `step_id`
- remove the exactly-one-target check that references `step_id`
- remove step-specific active-lease indexes
- keep a single `work_item_id` foreign key and the work-item active lease uniqueness rule

### Simplify Attempt Targeting

`attempts` currently supports either `step_id` or `work_item_id`.

After the cutover:

- remove `step_id`
- remove the exactly-one-target check that references `step_id`
- keep `work_item_id` as the single execution target

### Rename Planner Vocabulary

Replace any remaining `step_type` field with `work_type`.

This includes:

- planner contracts
- planner validation
- planner prompt text
- stored plan payloads produced after the migration

The historical migration trail may still mention `step_type`, but live schema and live code must not.

## Runtime Changes

### Planner And Materialization

The planner returns a plan with:

- `title`
- `goal`
- `repo`
- `allowed_paths`
- `forbidden_paths`
- `work_type`

`PlannerService.materialize_plan()` validates that shape and writes:

- `Outcome.title`
- `Outcome.goal`
- `Outcome.selected_repo`
- one `WorkItem` with `work_type = plan.work_type`

No planner or materialization path should mention `step_type`.

### PlanningCoordinator

`PlanningCoordinator` remains responsible for:

1. locating the active `IntentVersion`
2. creating an `Outcome`
3. creating a `PlanningSession`
4. appending `planning_started`
5. materializing the first `WorkItem`
6. appending `planning_succeeded` or `planning_failed`

Its outputs remain `Outcome` plus `WorkItem`.

### ControlPlaneService

`ControlPlaneService` becomes work-item only.

It should:

- issue leases only for `work_item_id`
- extend and heartbeat leases only for work-item-backed leases
- apply attempt completion only to `WorkItem`s
- append governance `WorkItem`s when policy requests review lanes
- mark outcomes complete through `Outcome` and `OutcomeEvent`

Legacy `step` branches, `StepStatus`, and step-specific error messages disappear.

### AmendmentService

Breaking amendments should revoke open canonical work, not legacy records.

The replacement behavior is:

- find `Outcome`s attached to the superseded `intent_id` and `intent_version`
- locate their open `WorkItem`s
- mark those work items revoked or otherwise terminal according to the canonical work-item status model

The service should not import or query `Case` or `Step`.

## API And Contract Changes

The worker-facing seam uses `work_item` everywhere.

### Worker Surface

Worker lease responses already return `work_item_id`. This pass extends that consistency to all remaining names and messages.

Examples:

- `step not found` becomes `work item not found`
- `step lease belongs to another worker` becomes `work item lease belongs to another worker` only where that concept is surfaced to callers
- internal helper names and logs use `work_item`

### Planner Contracts

`SynthesizedPlan.step_type` becomes `SynthesizedPlan.work_type`.

The planner provider prompt text must request `work_type` explicitly so generated payloads match the canonical model.

### Public Timeline

The public timeline remains a read model, but it now clearly derives only from:

- `IntentVersion`
- `Outcome`
- `WorkItem`
- `Lease`
- `Attempt`
- `OutcomeEvent`

No timeline code should inspect `Case` or `Step`.

## Testing And Verification

This pass needs both behavioral verification and vocabulary verification.

### Behavioral Verification

Run focused suites covering:

- planner contract validation
- coordinator materialization and planning failure handling
- control-plane lease issuance, heartbeat, completion, and governance fan-out
- amendment revocation behavior
- public timeline derivation
- worker API request/response behavior

### Vocabulary Verification

Search the live codebase for:

- `step_type`
- `step_id`
- `class Case`
- `class Step`
- `StepStatus`

Any remaining hits should be limited to:

- migration history that intentionally records the past
- superseded design documents

Live runtime code, live tests, and live API docs should no longer use those terms.

## Migration Strategy

This cutover assumes breaking migration is acceptable.

Recommended approach:

1. add the breaking migration that removes legacy tables and columns
2. land model and service updates in the same change so the app never expects removed schema
3. update tests and fixtures to build only canonical work-item data
4. update docs after the runtime is aligned

Because no compatibility mode is required, there is no need for phased rollout logic inside the application code.

## Risks

### Planner Payload Drift

If any provider or test fixture still emits `step_type`, planning will fail after the contract rename.

Mitigation:

- update provider prompt text
- update typed contracts first
- run planner and coordinator tests early

### Hidden Legacy Runtime Paths

A small number of paths may still import `Case` or `Step` outside the obvious planner/control-plane modules.

Mitigation:

- grep for legacy terms before and after edits
- run targeted tests around amendments and worker control flow

### Migration/Test Fixture Mismatch

Tests that seed `Lease` or `Attempt` through `step_id` will fail once the schema changes.

Mitigation:

- rewrite fixtures alongside the migration
- avoid temporary adapters

## Result

After this pass, GWS should have one execution language:

- intent versions describe desired work
- outcomes track concrete execution state
- planning sessions record planning attempts
- work items are the only leaseable execution units
- outcome events record immutable history

That leaves the system internally consistent and removes the need to explain which of two domain models is the real one.
