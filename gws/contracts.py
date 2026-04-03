from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class PlannerResult(str, enum.Enum):
    SATISFIED = "satisfied"


class SynthesizedPlan(BaseModel):
    title: str
    goal: str
    description: str = ""
    repo: str
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    work_type: str


class WorkerLeaseRequest(BaseModel):
    repo_heads: dict[str, str] = Field(default_factory=dict)
    intent_id: str | None = None
    ttl_seconds: int = 60


class WorkerLeaseResponse(BaseModel):
    lease_id: int
    work_item_id: int
    repo: str
    title: str
    goal: str
    description: str
    work_type: str
    allowed_paths: list[str]
    forbidden_paths: list[str]
    base_commit: str | None = None
    artifact_requirements: list[str] = Field(default_factory=list)
    heartbeat_deadline: str


class WorkerHeartbeatRequest(BaseModel):
    ttl_seconds: int = 60


class WorkerHeartbeatResponse(BaseModel):
    lease_id: int
    heartbeat_deadline: str


class WorkerLeaseExtensionRequest(BaseModel):
    ttl_seconds: int = 60
    reason: str


class WorkerLeaseExtensionResponse(BaseModel):
    lease_id: int
    heartbeat_deadline: str


class WorkerCompletionRequest(BaseModel):
    touched_paths: list[str]
    changed_hunks: list[str]


class WorkerCompletionResponse(BaseModel):
    status: str
