# Contributing to GWS

## Development Setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

To work with the Anthropic planner backend:

```bash
.venv/bin/pip install -e ".[anthropic]"
```

## Running Tests

```bash
.venv/bin/pytest -q
```

Tests use an in-memory SQLite database by default. No external services needed.

## Code Style

This project uses [ruff](https://docs.astral.sh/ruff/) for linting and formatting. CI will check both.

```bash
ruff check .
ruff format --check .
```

To auto-fix:

```bash
ruff check --fix .
ruff format .
```

## Pull Requests

- Keep PRs focused. One concern per PR. If you find something else to fix along the way, open a separate PR.
- All tests must pass. If you change behavior, update or add tests.
- Include a clear description of what changed and why. The diff shows the what; the PR description should explain the why.
- Follow existing patterns. Read the surrounding code before introducing new abstractions.

## Commit Messages

Use conventional commit prefixes:

- `feat:` new functionality
- `fix:` bug fix
- `refactor:` restructuring without behavior change
- `test:` test additions or changes
- `docs:` documentation
- `chore:` maintenance, dependency updates
- `ops:` deployment and infrastructure

## Architecture

Read `DESIGN_DECISIONS.md` before proposing structural changes. The core concepts (Intent, Outcome, Work Item, Lane, Lease) and their relationships are documented in the README.

The control plane stays provider-agnostic. Planner backends live under `gws/providers/` and implement the client interface in `gws/planner_client.py`.

## Reporting Issues

Open a GitHub issue. Include:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Python version and OS
