from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from fastapi import Header, HTTPException, status


@dataclass(frozen=True)
class WorkerIdentity:
    worker_id: str
    lane: str
    repo_access_set: tuple[str, ...]


class WorkerRegistry:
    def __init__(self, workers_by_token: dict[str, WorkerIdentity]):
        self._workers_by_token = workers_by_token

    @classmethod
    def from_file(cls, path: str) -> "WorkerRegistry":
        registry_path = Path(path)
        if not registry_path.is_absolute():
            registry_path = Path(__file__).resolve().parent.parent / registry_path

        with registry_path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        if data is None:
            data = {}

        if not isinstance(data, dict):
            raise ValueError("workers registry root must be a mapping")

        raw_workers = data.get("workers", [])
        if raw_workers is None:
            raw_workers = []
        if not isinstance(raw_workers, list):
            raise ValueError("workers registry workers entry must be a list")

        workers_by_token: dict[str, WorkerIdentity] = {}
        required_keys = {"token", "worker_id", "lane", "repo_access_set"}
        for raw_worker in raw_workers:
            if not isinstance(raw_worker, dict):
                raise ValueError("each worker entry must be a mapping")

            missing_keys = sorted(required_keys - raw_worker.keys())
            if missing_keys:
                raise ValueError(f"missing required worker keys: {', '.join(missing_keys)}")

            token = raw_worker["token"]
            if not isinstance(token, str):
                raise ValueError("token must be a string")

            worker_id = raw_worker["worker_id"]
            if not isinstance(worker_id, str):
                raise ValueError("worker_id must be a string")

            lane = raw_worker["lane"]
            if not isinstance(lane, str):
                raise ValueError("lane must be a string")

            repo_access_set = raw_worker["repo_access_set"]
            if not isinstance(repo_access_set, list):
                raise ValueError("repo_access_set must be a list")
            if any(not isinstance(repo_access_entry, str) for repo_access_entry in repo_access_set):
                raise ValueError("repo_access_set entries must be strings")

            if token in workers_by_token:
                raise ValueError(f"duplicate worker token: {token}")
            workers_by_token[token] = WorkerIdentity(
                worker_id=worker_id,
                lane=lane,
                repo_access_set=tuple(repo_access_set),
            )
        return cls(workers_by_token)

    def get_by_token(self, token: str) -> WorkerIdentity | None:
        return self._workers_by_token.get(token)


def authenticate_worker(
    registry: WorkerRegistry,
    authorization: str | None,
) -> WorkerIdentity:
    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
        )

    scheme, sep, token = authorization.partition(" ")
    if sep == "" or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
        )

    token = token.strip()
    worker = registry.get_by_token(token)
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid worker token",
        )
    return worker


def build_worker_auth_dependency(registry: WorkerRegistry):
    def require_worker(authorization: str | None = Header(default=None)) -> WorkerIdentity:
        return authenticate_worker(registry, authorization)

    return require_worker
