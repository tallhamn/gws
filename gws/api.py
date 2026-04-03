from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from .auth import WorkerIdentity, WorkerRegistry, build_worker_auth_dependency
from .config import Settings
from .contracts import (
    WorkerCompletionRequest,
    WorkerCompletionResponse,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerLeaseExtensionRequest,
    WorkerLeaseExtensionResponse,
    WorkerLeaseRequest,
    WorkerLeaseResponse,
)
from .control_plane import ControlPlaneService
from .coordinator import PlanningCoordinator
from .db import Base, make_session_factory
from .models import Lease

logger = logging.getLogger(__name__)


class IntentCreate(BaseModel):
    intent_id: str
    brief_text: str
    context: str = ""
    planner_guidance: str = ""


class PlanningUnavailableError(RuntimeError):
    pass


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    app = FastAPI(title="GWS Control Plane")
    settings = settings or Settings()
    worker_registry = WorkerRegistry.from_file(settings.workers_path)
    require_worker = build_worker_auth_dependency(worker_registry)
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
            if (
                request.url.path == "/healthz"
                or request.url.path.startswith("/public/")
                or request.url.path.startswith("/worker/")
            ):
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

    def _control_plane(session) -> ControlPlaneService:
        return ControlPlaneService(session, policy_path=settings.policy_path)

    def _jit_plan_work_item(
        *,
        session,
        worker: WorkerIdentity,
        intent_id: str | None,
        accessible_repos: list[str],
        eligible_repo_heads: dict[str, str],
    ):
        from .models import IntentVersion
        from .planner_client import build_planner_client
        from .policy import PolicyEngine

        if intent_id:
            intent = (
                session.query(IntentVersion)
                .filter(IntentVersion.intent_id == intent_id)
                .order_by(IntentVersion.intent_version.desc())
                .first()
            )
        else:
            intent = session.query(IntentVersion).order_by(IntentVersion.created_at.desc()).first()
        if intent is None:
            return None

        try:
            planner_client = build_planner_client(settings)
            policy = PolicyEngine.from_file(settings.policy_path)
            coordinator = PlanningCoordinator(
                session,
                planner_client=planner_client,
                planner_provider=settings.planner_provider,
                planner_model=settings.planner_model,
                lane_capabilities=policy.lane_capabilities(),
            )
            _outcome, work_item = coordinator.plan_outcome(
                intent_id=intent.intent_id,
                worker_id=worker.worker_id,
                lane=worker.lane,
                available_repos=accessible_repos,
                repo_heads=eligible_repo_heads,
            )
            logger.info("JIT planned work item %d for lane %s (intent=%s)", work_item.id, worker.lane, intent.intent_id)
            return work_item
        except Exception as exc:
            logger.exception("JIT planning failed for lane %s", worker.lane)
            raise PlanningUnavailableError("Planning unavailable") from exc

    async def _complete_work_item_for_worker(
        *,
        work_item_id: int,
        touched_paths: list[str],
        changed_hunks: list[str],
        worker: WorkerIdentity,
    ) -> WorkerCompletionResponse:
        with session_factory() as session:
            service = _control_plane(session)
            try:
                service.apply_attempt_completion(
                    work_item_id=work_item_id,
                    worker_id=worker.worker_id,
                    touched_paths=touched_paths,
                    changed_hunks=changed_hunks,
                )
            except PermissionError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ValueError as exc:
                logger.warning("Work item %d completion failed: %s", work_item_id, str(exc))
                if str(exc) == f"unknown work_item_id: {work_item_id}":
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found") from exc
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            logger.info("Work item %d completed, status=%s", work_item_id, "processed")
            return WorkerCompletionResponse(status="processed")

    @app.post("/worker/lease", response_model=WorkerLeaseResponse)
    def lease_work(
        payload: WorkerLeaseRequest,
        worker: WorkerIdentity = Depends(require_worker),
    ) -> WorkerLeaseResponse:
        with session_factory() as session:
            from .models import WorkItem, WorkItemStatus

            accessible_repos = list(worker.repo_access_set)
            eligible_repo_heads = {
                repo: head for repo, head in payload.repo_heads.items() if repo in worker.repo_access_set
            }

            work_item = (
                session.query(WorkItem)
                .filter(
                    WorkItem.lane == worker.lane,
                    WorkItem.status == WorkItemStatus.READY,
                    WorkItem.repo.in_(accessible_repos),
                )
                .order_by(WorkItem.id)
                .first()
            )

            if work_item is None and eligible_repo_heads:
                try:
                    work_item = _jit_plan_work_item(
                        session=session,
                        worker=worker,
                        intent_id=payload.intent_id,
                        accessible_repos=accessible_repos,
                        eligible_repo_heads=eligible_repo_heads,
                    )
                except PlanningUnavailableError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Planning unavailable"
                    ) from exc

            if work_item is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No eligible work")

            service = _control_plane(session)
            try:
                lease = service.issue_lease(
                    work_item_id=work_item.id,
                    worker_id=worker.worker_id,
                    ttl_seconds=payload.ttl_seconds,
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            return WorkerLeaseResponse(
                lease_id=lease.id,
                work_item_id=work_item.id,
                title=work_item.outcome.title,
                goal=work_item.outcome.goal,
                repo=work_item.repo,
                work_type=work_item.work_type,
                allowed_paths=list(work_item.allowed_paths),
                forbidden_paths=list(work_item.forbidden_paths),
                base_commit=work_item.base_commit,
                artifact_requirements=list(work_item.artifact_requirements),
                heartbeat_deadline=lease.heartbeat_deadline.isoformat(),
            )

    @app.post("/worker/leases/{lease_id}/heartbeat", response_model=WorkerHeartbeatResponse)
    def heartbeat_worker_lease(
        lease_id: int,
        payload: WorkerHeartbeatRequest,
        worker: WorkerIdentity = Depends(require_worker),
    ) -> WorkerHeartbeatResponse:
        with session_factory() as session:
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found")
            if lease.worker_id != worker.worker_id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="lease belongs to another worker")
            service = _control_plane(session)
            try:
                lease = service.heartbeat_lease(lease_id=lease_id, ttl_seconds=payload.ttl_seconds)
            except ValueError as exc:
                if "unknown lease_id" in str(exc):
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found") from exc
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            return WorkerHeartbeatResponse(
                lease_id=lease.id,
                heartbeat_deadline=lease.heartbeat_deadline.isoformat(),
            )

    @app.post("/worker/leases/{lease_id}/extend", response_model=WorkerLeaseExtensionResponse)
    def extend_worker_lease(
        lease_id: int,
        payload: WorkerLeaseExtensionRequest,
        worker: WorkerIdentity = Depends(require_worker),
    ) -> WorkerLeaseExtensionResponse:
        with session_factory() as session:
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found")
            if lease.worker_id != worker.worker_id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="lease belongs to another worker")
            service = _control_plane(session)
            try:
                lease = service.extend_lease(
                    lease_id=lease_id,
                    worker_id=worker.worker_id,
                    ttl_seconds=payload.ttl_seconds,
                    reason=payload.reason,
                )
            except PermissionError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            except ValueError as exc:
                if "unknown lease_id" in str(exc):
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found") from exc
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            return WorkerLeaseExtensionResponse(
                lease_id=lease.id,
                heartbeat_deadline=lease.heartbeat_deadline.isoformat(),
            )

    @app.post("/worker/work-items/{work_item_id}/complete", response_model=WorkerCompletionResponse)
    async def complete_worker_work_item(
        work_item_id: int,
        payload: WorkerCompletionRequest,
        worker: WorkerIdentity = Depends(require_worker),
    ) -> WorkerCompletionResponse:
        return await _complete_work_item_for_worker(
            work_item_id=work_item_id,
            touched_paths=payload.touched_paths,
            changed_hunks=payload.changed_hunks,
            worker=worker,
        )

    @app.post("/leases/expire")
    def expire_leases() -> dict:
        with session_factory() as session:
            service = _control_plane(session)
            count = service.expire_leases()
            return {"expired_count": count}

    @app.get("/public/intents/{intent_id}/timeline")
    def get_public_timeline(intent_id: str) -> dict:
        from .public_timeline import build_public_timeline

        with session_factory() as session:
            payload = build_public_timeline(session, intent_id)
            if payload is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Intent not found")
            return payload

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
