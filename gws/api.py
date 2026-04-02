from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sqlalchemy.exc import IntegrityError

from .config import Settings
from .control_plane import ControlPlaneService
from .db import Base, make_session_factory
from .models import PullRequest

logger = logging.getLogger(__name__)


class PullRequestCreate(BaseModel):
    worker_id: str
    lane: str
    intent_id: str
    repo_access_set: list[str] = Field(default_factory=list)
    envelope: dict = Field(default_factory=dict)


class PullRequestResponse(BaseModel):
    status: str


class LeaseRequest(BaseModel):
    worker_id: str
    ttl_seconds: int = 60
    repo_heads: dict[str, str] = Field(default_factory=dict)
    intent_id: str | None = None


class HeartbeatRequest(BaseModel):
    ttl_seconds: int = 60


class CompletedDiffIn(BaseModel):
    touched_paths: list[str]
    changed_hunks: list[str]


class IntentCreate(BaseModel):
    intent_id: str
    brief_text: str
    context: str = ""
    planner_guidance: str = ""


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    app = FastAPI(title="GWS Control Plane")
    settings = settings or Settings()
    session_factory, engine = make_session_factory(
        settings.database_url,
        pool_size=settings.db_pool_size,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=settings.db_pool_pre_ping,
    )
    Base.metadata.create_all(engine)
    logger.info("GWS app created, database_url=%s", settings.database_url.split("@")[-1])

    if settings.api_key:

        @app.middleware("http")
        async def check_api_key(request: Request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != settings.api_key:
                logger.warning("Unauthorized request to %s", request.url.path)
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Invalid or missing API key"},
                )
            return await call_next(request)

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
            logger.info("Pull request %d created by worker %s for lane %s", pull_request.id, payload.worker_id, payload.lane)

            return {"pull_request_id": pull_request.id}

    @app.get("/pull-requests/{pull_request_id}", response_model=PullRequestResponse)
    def get_pull_request(pull_request_id: int) -> PullRequestResponse:
        with session_factory() as session:
            pull_request = session.get(PullRequest, pull_request_id)
            if pull_request is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pull request not found")

            return PullRequestResponse(status=pull_request.status)

    @app.post("/steps/{step_id}/complete")
    async def complete_step(step_id: int, payload: CompletedDiffIn) -> dict[str, str]:
        with session_factory() as session:
            service = ControlPlaneService(session)
            try:
                service.apply_completed_diff(
                    step_id=step_id,
                    touched_paths=payload.touched_paths,
                    changed_hunks=payload.changed_hunks,
                )
            except ValueError as exc:
                logger.warning("Step %d completion failed: %s", step_id, str(exc))
                if str(exc) == f"unknown step_id: {step_id}":
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found") from exc
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

            # Run artifact verification if the step passed scope checks and has requirements
            from .models import Step, StepStatus
            step = session.get(Step, step_id)
            if step and step.status == StepStatus.SUCCEEDED and step.artifact_requirements:
                from .verifier import verify_artifacts
                gateway_url = settings.gateway_url
                if gateway_url:
                    artifact_result = await verify_artifacts(
                        requirements=list(step.artifact_requirements),
                        gateway_url=gateway_url,
                        repo=step.repo,
                    )
                    if not artifact_result.passed:
                        step.status = StepStatus.FAILED
                        session.commit()
                        logger.info("Step %d artifact verification failed", step_id)
                        return {"status": "artifact_verification_failed"}

            logger.info("Step %d completed, status=%s", step_id, "processed")
            return {"status": "processed"}

    @app.post("/lanes/{lane}/pull")
    def pull_step(lane: str, payload: LeaseRequest) -> dict:
        with session_factory() as session:
            from .models import IntentVersion, Step, StepStatus

            # 1. Check for existing READY steps in this lane
            step = (
                session.query(Step)
                .filter(Step.lane == lane, Step.status == StepStatus.READY)
                .order_by(Step.id)
                .first()
            )

            # 2. If none, try JIT planning from the latest intent
            if step is None and payload.intent_id and payload.repo_heads:
                intent = (
                    session.query(IntentVersion)
                    .filter(IntentVersion.intent_id == payload.intent_id)
                    .order_by(IntentVersion.intent_version.desc())
                    .first()
                )
                if intent is not None:
                    from .planner import PlannerService
                    from .planner_client import build_planner_client
                    from .policy import PolicyEngine

                    try:
                        planner_client = build_planner_client(settings)
                        policy = PolicyEngine.from_file("policy.yaml")
                        planner_service = PlannerService(
                            session, planner_client,
                            lane_capabilities=policy.lane_capabilities(),
                        )
                        pr = PullRequest(
                            worker_id=payload.worker_id,
                            lane=lane,
                            intent_id=payload.intent_id,
                            repo_access_set=list(payload.repo_heads.keys()),
                        )
                        session.add(pr)
                        session.commit()
                        session.refresh(pr)
                        _case, step = planner_service.plan_pull_request(pr.id, payload.repo_heads)
                        logger.info("JIT planned step %d for lane %s (intent=%s)", step.id, lane, payload.intent_id)
                    except Exception as exc:
                        logger.warning("JIT planning failed for lane %s: %s", lane, exc)
                        step = None

            if step is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No ready steps in lane: {lane}")

            service = ControlPlaneService(session)
            try:
                lease = service.issue_lease(step_id=step.id, worker_id=payload.worker_id, ttl_seconds=payload.ttl_seconds)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            return {
                "lease_id": lease.id,
                "step_id": step.id,
                "title": step.case.title,
                "goal": step.case.goal,
                "repo": step.repo,
                "lane": step.lane,
                "step_type": step.step_type,
                "allowed_paths": list(step.allowed_paths),
                "forbidden_paths": list(step.forbidden_paths),
                "base_commit": step.base_commit,
                "artifact_requirements": list(step.artifact_requirements),
            }

    @app.post("/leases/{lease_id}/heartbeat")
    def heartbeat_lease(lease_id: int, payload: HeartbeatRequest) -> dict:
        with session_factory() as session:
            service = ControlPlaneService(session)
            try:
                lease = service.heartbeat_lease(lease_id=lease_id, ttl_seconds=payload.ttl_seconds)
            except ValueError as exc:
                if "unknown lease_id" in str(exc):
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found") from exc
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            return {"lease_id": lease.id, "heartbeat_deadline": lease.heartbeat_deadline.isoformat()}

    @app.post("/leases/expire")
    def expire_leases() -> dict:
        with session_factory() as session:
            service = ControlPlaneService(session)
            count = service.expire_leases()
            return {"expired_count": count}

    @app.post("/intents", status_code=status.HTTP_201_CREATED)
    def create_intent(payload: IntentCreate) -> dict:
        with session_factory() as session:
            from .models import IntentVersion
            latest_version = (
                session.query(IntentVersion.intent_version)
                .filter(IntentVersion.intent_id == payload.intent_id)
                .order_by(IntentVersion.intent_version.desc())
                .limit(1)
                .scalar()
            )
            new_version = (latest_version or 0) + 1
            intent = IntentVersion(
                intent_id=payload.intent_id,
                intent_version=new_version,
                brief_text=payload.brief_text,
                context=payload.context,
                planner_guidance=payload.planner_guidance,
            )
            session.add(intent)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Concurrent intent version conflict, retry",
                ) from exc
            logger.info("Intent %s v%d created", payload.intent_id, new_version)
            return {"intent_id": payload.intent_id, "intent_version": new_version}

    @app.get("/intents/{intent_id}")
    def get_intent(intent_id: str) -> dict:
        with session_factory() as session:
            from .models import IntentVersion
            intent = (
                session.query(IntentVersion)
                .filter(IntentVersion.intent_id == intent_id)
                .order_by(IntentVersion.intent_version.desc())
                .first()
            )
            if intent is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Intent not found")
            return {
                "intent_id": intent.intent_id,
                "intent_version": intent.intent_version,
                "brief_text": intent.brief_text,
                "context": intent.context,
                "planner_guidance": intent.planner_guidance,
            }

    return app
