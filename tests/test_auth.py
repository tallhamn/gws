import pytest
from fastapi import HTTPException

from gws.auth import WorkerRegistry, authenticate_worker


def test_worker_registry_loads_workers_from_file(worker_registry_path):
    registry = WorkerRegistry.from_file(str(worker_registry_path))

    worker = registry.get_by_token("token-coder-1")

    assert worker is not None
    assert worker.worker_id == "coder-1"
    assert worker.lane == "coder"
    assert list(worker.repo_access_set) == ["repo-a", "repo-b"]


def test_authenticate_worker_rejects_missing_and_unknown_tokens(worker_registry_path):
    registry = WorkerRegistry.from_file(str(worker_registry_path))

    with pytest.raises(HTTPException) as missing_exc:
        authenticate_worker(registry, None)
    assert missing_exc.value.status_code == 401

    with pytest.raises(HTTPException) as invalid_exc:
        authenticate_worker(registry, "Bearer wrong-token")
    assert invalid_exc.value.status_code == 401


def test_authenticate_worker_accepts_valid_bearer_token(worker_registry_path):
    registry = WorkerRegistry.from_file(str(worker_registry_path))

    worker = authenticate_worker(registry, "Bearer token-coder-1")

    assert worker.worker_id == "coder-1"
    assert worker.lane == "coder"
    assert list(worker.repo_access_set) == ["repo-a", "repo-b"]


def test_authenticate_worker_accepts_lowercase_bearer_scheme(worker_registry_path):
    registry = WorkerRegistry.from_file(str(worker_registry_path))

    worker = authenticate_worker(registry, "bearer token-coder-1")

    assert worker.worker_id == "coder-1"
    assert worker.lane == "coder"
    assert list(worker.repo_access_set) == ["repo-a", "repo-b"]


def test_worker_registry_rejects_malformed_root(worker_registry_path, tmp_path):
    path = tmp_path / "bad-workers-root.yaml"
    path.write_text("- token: token-coder-1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="workers registry root must be a mapping"):
        WorkerRegistry.from_file(str(path))


def test_worker_registry_rejects_workers_missing_required_keys(tmp_path):
    path = tmp_path / "bad-workers-missing-keys.yaml"
    path.write_text(
        """workers:
  - token: token-coder-1
    worker_id: coder-1
    lane: coder
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required worker keys: repo_access_set"):
        WorkerRegistry.from_file(str(path))
