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


@pytest.mark.parametrize("root_text", ["[]\n", "false\n", "0\n", '""\n'])
def test_worker_registry_rejects_falsy_malformed_roots(root_text, tmp_path):
    path = tmp_path / "bad-workers-falsy-root.yaml"
    path.write_text(root_text, encoding="utf-8")

    with pytest.raises(ValueError, match="workers registry root must be a mapping"):
        WorkerRegistry.from_file(str(path))


def test_worker_registry_rejects_workers_entry_that_is_not_a_list(tmp_path):
    path = tmp_path / "bad-workers-not-a-list.yaml"
    path.write_text("workers: token-coder-1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="workers registry workers entry must be a list"):
        WorkerRegistry.from_file(str(path))


def test_worker_registry_rejects_worker_entry_that_is_not_a_mapping(tmp_path):
    path = tmp_path / "bad-worker-entry-not-mapping.yaml"
    path.write_text(
        """workers:
  - token-coder-1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="each worker entry must be a mapping"):
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


def test_worker_registry_rejects_string_repo_access_set(tmp_path):
    path = tmp_path / "bad-worker-string-repo-access-set.yaml"
    path.write_text(
        """workers:
  - token: token-coder-1
    worker_id: coder-1
    lane: coder
    repo_access_set: repo-a
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="repo_access_set must be a list"):
        WorkerRegistry.from_file(str(path))


def test_worker_registry_rejects_non_string_repo_access_entry(tmp_path):
    path = tmp_path / "bad-worker-non-string-repo-access-entry.yaml"
    path.write_text(
        """workers:
  - token: token-coder-1
    worker_id: coder-1
    lane: coder
    repo_access_set:
      - repo-a
      - 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="repo_access_set entries must be strings"):
        WorkerRegistry.from_file(str(path))


def test_worker_registry_rejects_non_string_token(tmp_path):
    path = tmp_path / "bad-worker-non-string-token.yaml"
    path.write_text(
        """workers:
  - token: 123
    worker_id: coder-1
    lane: coder
    repo_access_set:
      - repo-a
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="token must be a string"):
        WorkerRegistry.from_file(str(path))


def test_worker_registry_rejects_non_string_worker_id(tmp_path):
    path = tmp_path / "bad-worker-non-string-worker-id.yaml"
    path.write_text(
        """workers:
  - token: token-coder-1
    worker_id: 123
    lane: coder
    repo_access_set:
      - repo-a
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="worker_id must be a string"):
        WorkerRegistry.from_file(str(path))


def test_worker_registry_rejects_non_string_lane(tmp_path):
    path = tmp_path / "bad-worker-non-string-lane.yaml"
    path.write_text(
        """workers:
  - token: token-coder-1
    worker_id: coder-1
    lane: 123
    repo_access_set:
      - repo-a
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="lane must be a string"):
        WorkerRegistry.from_file(str(path))
