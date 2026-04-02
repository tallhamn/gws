# Minimal Worker Auth Design

## Goal

Add a minimal worker authentication layer that lets the control plane identify
workers reliably and derive their allowed lane and repo access server-side.

This slice is intentionally narrow. It does not introduce user accounts, OAuth,
token refresh, an admin surface, or multi-tenant auth.

## Scope

In scope:

- static bearer tokens for workers
- a checked-in worker registry file
- request authentication for control-plane endpoints
- server-derived `worker_id`, `lane`, and `repo_access_set`
- lease ownership enforcement on step completion

Out of scope:

- rotating tokens automatically
- external identity providers
- per-endpoint role systems beyond worker identity
- human-facing authentication

## Approach

The runtime will load a local `workers.yaml` registry that maps each token to a
worker definition:

- `token`
- `worker_id`
- `lane`
- `repo_access_set`

FastAPI will use a small auth dependency that reads the `Authorization: Bearer`
header, resolves the worker from the registry, and returns a server-side worker
identity object.

The API stops treating worker identity as client-supplied data. Pull-request
creation will accept only the worker envelope from the client. The server fills
in `worker_id`, `lane`, and `repo_access_set` from the authenticated worker.

Step completion remains addressed by `step_id` in this slice, but the control
plane must verify that the active lease belongs to the authenticated worker
before accepting the completion.

## Data Flow

### Pull Request Creation

1. Worker sends `Authorization: Bearer <token>`.
2. Auth dependency resolves the worker from `workers.yaml`.
3. Request body provides only `envelope`.
4. Server creates the `PullRequest` using server-derived worker identity and
   capability fields.

### Step Completion

1. Worker sends `Authorization: Bearer <token>`.
2. Auth dependency resolves the worker.
3. Request body provides completion payload.
4. Control plane finds the active lease for the step.
5. If the lease belongs to a different worker, reject the request.
6. If the lease belongs to the authenticated worker, continue with existing
   verification behavior.

## Error Handling

- Missing or malformed `Authorization` header: `401`
- Unknown token: `401`
- Authenticated worker attempts to complete a step leased to another worker:
  `403`
- Existing domain errors such as unknown step or no active lease remain `404`
  or `400` as they are today

The auth layer should stay deterministic and fail closed.

## Testing

Add tests for:

- worker registry loading
- successful bearer-token authentication
- rejection of missing or invalid tokens
- pull-request creation deriving worker fields from auth instead of request
- step completion rejecting non-owner workers
- existing happy-path completion still working for the lease owner

## Tradeoffs

Benefits:

- removes client trust for worker identity and capability
- keeps the auth model small and legible
- improves safety without adding platform-style auth complexity

Costs:

- checked-in static tokens are only acceptable for local or early-stage use
- registry updates require file edits and restarts unless hot reload is added

## Follow-On Work

If the system later needs stronger operational guarantees, the next steps should
be token storage/rotation and lease-addressed completion. Those are explicitly
separate from this slice.
