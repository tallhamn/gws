from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .config import Settings
from .control_plane import ControlPlaneService
from .db import Base, make_session_factory
from .models import PullRequest


class PullRequestCreate(BaseModel):
    worker_id: str
    lane: str
    intent_id: str
    repo_access_set: list[str] = Field(default_factory=list)
    envelope: dict = Field(default_factory=dict)


class PullRequestResponse(BaseModel):
    status: str


class CompletedDiffIn(BaseModel):
    touched_paths: list[str]
    changed_hunks: list[str]


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    app = FastAPI(title="GWS Control Plane")
    settings = settings or Settings()
    session_factory, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/pull-requests", status_code=status.HTTP_202_ACCEPTED)
    def create_pull_request(payload: PullRequestCreate) -> dict[str, int]:
        with session_factory() as session:
            pull_request = PullRequest(
                worker_id=payload.worker_id,
                lane=payload.lane,
                intent_id=payload.intent_id,
                repo_access_set=payload.repo_access_set,
                envelope=payload.envelope,
            )

            session.add(pull_request)
            session.commit()
            session.refresh(pull_request)

            return {"pull_request_id": pull_request.id}

    @app.get("/pull-requests/{pull_request_id}", response_model=PullRequestResponse)
    def get_pull_request(pull_request_id: int) -> PullRequestResponse:
        with session_factory() as session:
            pull_request = session.get(PullRequest, pull_request_id)
            if pull_request is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pull request not found")

            return PullRequestResponse(status=pull_request.status)

    @app.post("/steps/{step_id}/complete")
    def complete_step(step_id: int, payload: CompletedDiffIn) -> dict[str, str]:
        with session_factory() as session:
            service = ControlPlaneService(session)
            try:
                service.apply_completed_diff(
                    step_id=step_id,
                    touched_paths=payload.touched_paths,
                    changed_hunks=payload.changed_hunks,
                )
            except ValueError as exc:
                if str(exc) == f"unknown step_id: {step_id}":
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found") from exc
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            return {"status": "processed"}

    return app
