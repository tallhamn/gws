# GWS Domain Remodel Design

Date: 2026-04-02

## Goal

Replace the current transitional runtime model with a domain model that is explicit, durable, and pleasant to reason about.

The current shape has two core problems:

- the top-level durable record is not the thing operators actually care about
- the final result of a requested outcome is reconstructed indirectly from scattered planning, step, attempt, and verdict state

This redesign makes one requested outcome the center of the system.

## Design Principles

- One durable top-level record per requested result.
- Final outcome must be explicit on that record.
- Planning history and execution history must remain inspectable without becoming the domain center.
- Worker leases remain governed by GWS, not self-extended indefinitely by workers.
- The public interface may break if that produces a cleaner and more stable system.

## Durable Domain Model

### IntentVersion

Immutable desired state.

Fields:

- `intent_id`
- `intent_version`
- `brief_text`
- `context`
- `planner_guidance`
- `accepted_amendments`
- `created_at`

### Outcome

The main durable record. One outcome represents one requested result derived from one intent version.

Fields:

- `id`
- `intent_id`
- `intent_version`
- `title`
- `goal`
- `phase`
- `result`
- `selected_repo`
- `current_work_item_id`
- `result_summary`
- `result_commit`
- `created_at`
- `completed_at`

Notes:

- `phase` and `result` are separate.
- `phase` answers "where is this outcome in the workflow?"
- `result` answers "how did it end?"
- `result` is nullable until completion.

Allowed `phase` values:

- `planning`
- `ready`
- `running`
- `completed`

Allowed `result` values:

- `succeeded`
- `failed`
- `superseded`
- `abandoned`

### PlanningSession

One planning attempt for an outcome.

Fields:

- `id`
- `outcome_id`
- `worker_id`
- `lane`
- `status`
- `planner_provider`
- `planner_model`
- `available_repos`
- `repo_heads`
- `planning_context`
- `plan_payload`
- `error_detail`
- `created_at`
- `completed_at`

Allowed `status` values:

- `pending`
- `succeeded`
- `failed`

Notes:

- This replaces the current `PullRequest` concept entirely.
- It is the durable record of how GWS decided what bounded work to create.

### WorkItem

One bounded executable unit under an outcome.

Fields:

- `id`
- `outcome_id`
- `sequence_index`
- `blocked_by_work_item_id`
- `repo`
- `lane`
- `work_type`
- `status`
- `allowed_paths`
- `forbidden_paths`
- `base_commit`
- `target_branch`
- `artifact_requirements`
- `created_at`
- `completed_at`

Allowed `status` values:

- `ready`
- `leased`
- `running`
- `verifying`
- `succeeded`
- `failed`
- `revoked`

Notes:

- `sequence_index` provides stable ordering.
- `blocked_by_work_item_id` provides minimal dependency support without introducing a full DAG subsystem.

### Lease

Temporary ownership of a work item by a worker.

Fields:

- `id`
- `work_item_id`
- `worker_id`
- `lane`
- `issued_at`
- `heartbeat_deadline`
- `expires_at`
- `expired_at`
- `ended_at`
- `end_reason`
- `base_commit`

Allowed `end_reason` values:

- `completed`
- `expired`
- `revoked`
- `replaced`

Notes:

- Heartbeats keep a lease alive within its granted window.
- Lease extensions are recorded as events, not as a separate first-class table.

### Attempt

One concrete worker submission against a lease.

Fields:

- `id`
- `work_item_id`
- `lease_id`
- `worker_id`
- `repo`
- `status`
- `submitted_diff_ref`
- `artifact_refs`
- `created_at`
- `submitted_at`

Allowed `status` values:

- `pending`
- `accepted`
- `rejected`

### Verdict

Governance decision on an attempt.

Fields:

- `id`
- `attempt_id`
- `result`
- `summary`
- `created_at`

Allowed `result` values:

- `pass`
- `fail_and_replan`
- `append_governance_step`
- `quarantine`
- `superseded`

### OutcomeEvent

Append-only audit/history stream for major transitions.

Fields:

- `id`
- `outcome_id`
- `event_type`
- `payload`
- `created_at`

Representative `event_type` values:

- `planning_started`
- `planning_succeeded`
- `planning_failed`
- `work_item_created`
- `lease_granted`
- `lease_extended`
- `lease_expired`
- `attempt_submitted`
- `verdict_issued`
- `outcome_completed`
- `outcome_superseded`

Notes:

- This is the canonical history stream.
- Lease extensions, retries, and failures belong here.
- The core tables stay small because detailed chronology lives in events.

## Public Interface

The public API remains small and worker-scoped, but the implementation beneath it changes to reflect the new domain:

- `POST /intents`
- `POST /worker/lease`
- `POST /worker/leases/{lease_id}/heartbeat`
- `POST /worker/leases/{lease_id}/extend`
- `POST /worker/work-items/{work_item_id}/complete`

Notes:

- The current `step` vocabulary should be retired in favor of `work_item`.
- Lease extension is explicitly modeled and governed.
- The internal runtime no longer exposes or depends on `PullRequest`.

## Lease Extension Policy

Workers may request more time, but they do not control their own budget.

Rules:

- Workers may request an extension with a short reason.
- GWS may grant, cap, or deny the extension.
- Granted extensions update the lease deadline and emit an `OutcomeEvent`.
- Lane policy will determine maximum extension behavior.

This keeps the runtime honest:

- workers can report reality
- GWS remains the governor
- operators can inspect extension history later

## Runtime Responsibilities

### API Layer

The API layer should only:

- authenticate requests
- translate HTTP input/output
- map domain errors to HTTP errors

It should not contain planning orchestration logic.

### Planning Coordinator

A dedicated planning coordinator should:

- resolve the active intent version
- choose the planner
- synthesize a plan
- persist a `PlanningSession`
- materialize the `Outcome`
- materialize initial `WorkItem` records
- emit `OutcomeEvent` history

### Execution / Governance Layer

The control plane should:

- issue leases
- heartbeat or extend leases
- accept attempts
- verify scope and policy
- create verdicts
- update `Outcome` phase/result explicitly
- emit `OutcomeEvent` history

## Data Migration Strategy

This remodel assumes breaking schema migration is acceptable.

Migration shape:

1. Introduce the new tables and enums.
2. Migrate current durable records into `Outcome`, `PlanningSession`, and `WorkItem`.
3. Move current attempt/verdict history forward.
4. Populate explicit `Outcome.phase` and `Outcome.result`.
5. Remove the old `PullRequest` and `Step` runtime usage.
6. Retire old schema artifacts once the runtime is fully switched.

## Testing Strategy

Required coverage:

- model tests for enums, defaults, and constraints
- migration tests for old-to-new data translation
- API tests for worker lease, heartbeat, extension, and completion
- coordinator tests for planning session creation and outcome materialization
- control-plane tests for explicit outcome completion and event emission
- end-to-end tests for planning -> lease -> attempt -> verdict -> completed outcome

## Success Criteria

The remodel is successful when:

- one requested result corresponds to one explicit `Outcome`
- the final result is readable directly from that outcome
- history is readable from `OutcomeEvent` without reconstructive archaeology
- `PullRequest` disappears from runtime code and schema
- `Step` disappears from the public and internal vocabulary in favor of `WorkItem`
- API handlers become translation layers instead of orchestration code
