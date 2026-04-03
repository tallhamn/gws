import enum
import weakref
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    text,
)
from sqlalchemy.ext.mutable import Mutable, MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _enum_column(enum_cls: type[enum.Enum]) -> Enum:
    return Enum(
        enum_cls,
        values_callable=lambda mapped_enum: [member.value for member in mapped_enum],
        validate_strings=True,
        native_enum=False,
        create_constraint=True,
    )


class VerdictResult(str, enum.Enum):
    PASS = "pass"
    FAIL_AND_REPLAN = "fail_and_replan"
    APPEND_GOVERNANCE_STEP = "append_governance_step"
    QUARANTINE = "quarantine"
    SUPERSEDED = "superseded"


class AttemptResultStatus(str, enum.Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class OutcomePhase(str, enum.Enum):
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"


class OutcomeResult(str, enum.Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"


class PlanningSessionStatus(str, enum.Enum):
    PENDING = "pending"
    MATERIALIZING = "materializing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AmendmentProposalStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"


class WorkItemStatus(str, enum.Enum):
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REVOKED = "revoked"


class DeepMutableBase(Mutable):
    def __init__(self):
        self._mutable_parents: list[weakref.ReferenceType["DeepMutableBase"]] = []

    def _add_mutable_parent(self, parent: "DeepMutableBase") -> None:
        for parent_ref in self._mutable_parents:
            if parent_ref() is parent:
                return
        self._mutable_parents.append(weakref.ref(parent))

    def _remove_mutable_parent(self, parent: "DeepMutableBase") -> None:
        self._mutable_parents = [parent_ref for parent_ref in self._mutable_parents if parent_ref() is not parent]

    def _attach_child(self, value) -> None:
        if isinstance(value, DeepMutableBase):
            value._add_mutable_parent(self)

    def _detach_child(self, value) -> None:
        if isinstance(value, DeepMutableBase):
            value._remove_mutable_parent(self)

    def _coerce_child(self, value):
        if isinstance(value, DeepMutableBase):
            return value
        if isinstance(value, dict):
            return DeepMutableDict.coerce(None, value)
        if isinstance(value, list):
            return DeepMutableList.coerce(None, value)
        return value

    def changed(self) -> None:
        live_parents: list[weakref.ReferenceType["DeepMutableBase"]] = []
        for parent_ref in self._mutable_parents:
            parent = parent_ref()
            if parent is None:
                continue
            parent.changed()
            live_parents.append(parent_ref)
        self._mutable_parents = live_parents
        super().changed()


class DeepMutableDict(DeepMutableBase, MutableDict):
    def __init__(self, *args, **kwargs):
        DeepMutableBase.__init__(self)
        dict.__init__(self)
        if args or kwargs:
            self.update(*args, **kwargs)

    def __setitem__(self, key, value) -> None:
        if key in self:
            self._detach_child(self[key])
        value = self._coerce_child(value)
        dict.__setitem__(self, key, value)
        self._attach_child(value)
        self.changed()

    def __delitem__(self, key) -> None:
        self._detach_child(self[key])
        dict.__delitem__(self, key)
        self.changed()

    def update(self, *args, **kwargs) -> None:
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, value=None):
        if key not in self:
            self[key] = value
        return self[key]

    def pop(self, *args):
        key = args[0]
        result = self[key] if key in self else None
        if result is not None:
            self._detach_child(result)
        result = dict.pop(self, *args)
        self.changed()
        return result

    def popitem(self):
        if not self:
            raise KeyError("popitem(): dictionary is empty")
        key, result = next(reversed(self.items()))
        self._detach_child(result)
        result = dict.popitem(self)
        self.changed()
        return result

    def clear(self) -> None:
        for value in list(self.values()):
            self._detach_child(value)
        dict.clear(self)
        self.changed()

    @classmethod
    def coerce(cls, key, value):
        if not isinstance(value, cls):
            if isinstance(value, dict):
                return cls(value)
            return Mutable.coerce(key, value)
        return value

    def __getstate__(self):
        return dict(self)


class DeepMutableList(DeepMutableBase, MutableList):
    def __init__(self, iterable=()):
        DeepMutableBase.__init__(self)
        list.__init__(self)
        self.extend(iterable)

    def _coerce_iterable(self, values):
        return [self._coerce_child(value) for value in values]

    def __setitem__(self, index, value) -> None:
        if isinstance(index, slice):
            old_values = list(self[index])
            for item in old_values:
                self._detach_child(item)
            values = self._coerce_iterable(value)
            list.__setitem__(self, index, values)
            for item in values:
                self._attach_child(item)
        else:
            self._detach_child(self[index])
            value = self._coerce_child(value)
            list.__setitem__(self, index, value)
            self._attach_child(value)
        self.changed()

    def append(self, value) -> None:
        value = self._coerce_child(value)
        list.append(self, value)
        self._attach_child(value)
        self.changed()

    def extend(self, values) -> None:
        values = self._coerce_iterable(values)
        list.extend(self, values)
        for value in values:
            self._attach_child(value)
        self.changed()

    def insert(self, index, value) -> None:
        value = self._coerce_child(value)
        list.insert(self, index, value)
        self._attach_child(value)
        self.changed()

    def pop(self, *args):
        index = args[0] if args else -1
        result = self[index]
        self._detach_child(result)
        result = list.pop(self, *args)
        self.changed()
        return result

    def remove(self, value) -> None:
        index = list.index(self, value)
        self.pop(index)

    def __delitem__(self, index) -> None:
        if isinstance(index, slice):
            for item in list(self[index]):
                self._detach_child(item)
            list.__delitem__(self, index)
        else:
            self._detach_child(self[index])
            list.__delitem__(self, index)
        self.changed()

    def clear(self) -> None:
        for value in list(self):
            self._detach_child(value)
        list.clear(self)
        self.changed()

    def sort(self, **kw) -> None:
        list.sort(self, **kw)
        self.changed()

    def reverse(self) -> None:
        list.reverse(self)
        self.changed()

    def __iadd__(self, values):
        self.extend(values)
        return self

    @classmethod
    def coerce(cls, key, value):
        if not isinstance(value, cls):
            if isinstance(value, list):
                return cls(value)
            return Mutable.coerce(key, value)
        return value

    def __reduce_ex__(self, proto):
        return (self.__class__, (list(self),))

    def __setstate__(self, state):
        self[:] = state


class IntentVersion(Base):
    __tablename__ = "intent_versions"
    __table_args__ = (
        UniqueConstraint("intent_id", "intent_version", name="uq_intent_versions_intent_id_intent_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(128), index=True)
    intent_version: Mapped[int] = mapped_column(Integer)
    brief_text: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="")
    planner_guidance: Mapped[str] = mapped_column(Text, default="")
    accepted_amendments: Mapped[list[dict]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)


class Outcome(Base):
    __tablename__ = "outcomes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["intent_versions.intent_id", "intent_versions.intent_version"],
            name="fk_outcomes_intent_versions",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(128), index=True)
    intent_version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    goal: Mapped[str] = mapped_column(Text)
    phase: Mapped[OutcomePhase] = mapped_column(_enum_column(OutcomePhase), default=OutcomePhase.PLANNING)
    result: Mapped[Optional[OutcomeResult]] = mapped_column(_enum_column(OutcomeResult), nullable=True)
    selected_repo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    current_work_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("work_items.id"), nullable=True)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    result_commit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    work_items: Mapped[list["WorkItem"]] = relationship(
        back_populates="outcome",
        foreign_keys="WorkItem.outcome_id",
        order_by=lambda: (WorkItem.sequence_index, WorkItem.id),
    )
    planning_sessions: Mapped[list["PlanningSession"]] = relationship(back_populates="outcome")
    events: Mapped[list["OutcomeEvent"]] = relationship(back_populates="outcome")
    current_work_item: Mapped[Optional["WorkItem"]] = relationship(
        "WorkItem", foreign_keys=[current_work_item_id], post_update=True
    )


class PlanningSession(Base):
    __tablename__ = "planning_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outcome_id: Mapped[int] = mapped_column(ForeignKey("outcomes.id"), index=True)
    worker_id: Mapped[str] = mapped_column(String(128), index=True)
    lane: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[PlanningSessionStatus] = mapped_column(
        _enum_column(PlanningSessionStatus), default=PlanningSessionStatus.PENDING
    )
    planner_provider: Mapped[str] = mapped_column(String(64))
    planner_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    available_repos: Mapped[list[str]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    repo_heads: Mapped[dict] = mapped_column(DeepMutableDict.as_mutable(JSON), default=dict)
    planning_context: Mapped[dict] = mapped_column(DeepMutableDict.as_mutable(JSON), default=dict)
    plan_payload: Mapped[dict] = mapped_column(DeepMutableDict.as_mutable(JSON), default=dict)
    error_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[Outcome] = relationship(back_populates="planning_sessions")


class WorkItem(Base):
    __tablename__ = "work_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["outcome_id", "blocked_by_work_item_id"],
            ["work_items.outcome_id", "work_items.id"],
            name="fk_work_items_blocked_by_same_outcome",
        ),
        UniqueConstraint("outcome_id", "id", name="uq_work_items_outcome_id_id"),
        UniqueConstraint("outcome_id", "sequence_index", name="uq_work_items_outcome_sequence_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outcome_id: Mapped[int] = mapped_column(ForeignKey("outcomes.id"), index=True)
    sequence_index: Mapped[int] = mapped_column(Integer)
    blocked_by_work_item_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    lane: Mapped[str] = mapped_column(String(64), index=True)
    work_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[WorkItemStatus] = mapped_column(_enum_column(WorkItemStatus), default=WorkItemStatus.READY)
    allowed_paths: Mapped[list[str]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    forbidden_paths: Mapped[list[str]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    base_commit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    target_branch: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    artifact_requirements: Mapped[list[str]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[Outcome] = relationship(back_populates="work_items", foreign_keys=[outcome_id])
    blocked_by_work_item: Mapped[Optional["WorkItem"]] = relationship(
        "WorkItem",
        foreign_keys=[outcome_id, blocked_by_work_item_id],
        remote_side=[outcome_id, id],
        back_populates="dependent_work_items",
        overlaps="outcome,work_items",
    )
    dependent_work_items: Mapped[list["WorkItem"]] = relationship(
        "WorkItem",
        foreign_keys=[outcome_id, blocked_by_work_item_id],
        back_populates="blocked_by_work_item",
        overlaps="outcome,work_items",
    )
    leases: Mapped[list["Lease"]] = relationship(back_populates="work_item")
    attempts: Mapped[list["Attempt"]] = relationship(back_populates="work_item")


class OutcomeEvent(Base):
    __tablename__ = "outcome_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outcome_id: Mapped[int] = mapped_column(ForeignKey("outcomes.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(DeepMutableDict.as_mutable(JSON), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    outcome: Mapped[Outcome] = relationship(back_populates="events")


def _prevent_outcome_event_update(mapper, connection, target):
    del mapper, connection, target
    raise ValueError("OutcomeEvent is append-only and cannot be updated")


def _prevent_outcome_event_delete(mapper, connection, target):
    del mapper, connection, target
    raise ValueError("OutcomeEvent is append-only and cannot be deleted")


def _validate_outcome_current_work_item_scope(mapper, connection, target):
    del mapper
    if target.current_work_item_id is None:
        return

    work_item_row = connection.execute(
        text("SELECT outcome_id FROM work_items WHERE id = :work_item_id"),
        {"work_item_id": target.current_work_item_id},
    ).one_or_none()
    if work_item_row is None:
        raise ValueError("Outcome current_work_item_id must reference an existing work item")

    # New outcomes may only bind through an in-memory relationship to their own pending item.
    if target.id is None:
        current_item = target.current_work_item
        if current_item is None or current_item.outcome is not target:
            raise ValueError("Outcome must only reference a current work item from the same outcome")
        return

    if work_item_row.outcome_id != target.id:
        raise ValueError("Outcome must only reference a current work item from the same outcome")


def _validate_work_item_reassignment_scope(mapper, connection, target):
    del mapper
    if target.id is None:
        return

    outcome_id_history = inspect(target).attrs.outcome_id.history
    if not outcome_id_history.has_changes():
        return

    referencing_outcome_id = connection.execute(
        text("SELECT id FROM outcomes WHERE current_work_item_id = :work_item_id"),
        {"work_item_id": target.id},
    ).scalar_one_or_none()
    if referencing_outcome_id is None:
        return

    if referencing_outcome_id != target.outcome_id:
        raise ValueError("Cannot move a current work item to a different outcome")


class AmendmentProposal(Base):
    __tablename__ = "amendment_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(128), index=True)
    base_intent_version: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    amended_brief_text: Mapped[str] = mapped_column(Text)
    is_breaking: Mapped[bool] = mapped_column(default=False)
    status: Mapped[AmendmentProposalStatus] = mapped_column(
        _enum_column(AmendmentProposalStatus), default=AmendmentProposalStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Lease(Base):
    __tablename__ = "leases"
    __table_args__ = (
        Index(
            "uq_leases_active_work_item_id",
            "work_item_id",
            unique=True,
            postgresql_where=text("expired_at IS NULL"),
            sqlite_where=text("expired_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id"), index=True)
    worker_id: Mapped[str] = mapped_column(String(128), index=True)
    lane: Mapped[str] = mapped_column(String(64), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    heartbeat_deadline: Mapped[datetime] = mapped_column(DateTime, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    expired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    base_commit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    work_item: Mapped["WorkItem"] = relationship(back_populates="leases")
    attempt: Mapped[Optional["Attempt"]] = relationship(back_populates="lease", uselist=False)


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("lease_id", name="uq_attempts_lease_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id"), index=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), index=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    result_status: Mapped[AttemptResultStatus] = mapped_column(
        _enum_column(AttemptResultStatus),
        default=AttemptResultStatus.PENDING,
    )
    artifact_refs: Mapped[list[str]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    submitted_diff_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    work_item: Mapped["WorkItem"] = relationship(back_populates="attempts")
    lease: Mapped[Lease] = relationship(back_populates="attempt")
    verdicts: Mapped[list["Verdict"]] = relationship(back_populates="attempt")


class Verdict(Base):
    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id"), index=True)
    result: Mapped[VerdictResult] = mapped_column(_enum_column(VerdictResult))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    attempt: Mapped[Attempt] = relationship(back_populates="verdicts")


def _init_json_defaults(target, args, kwargs):
    del args
    if getattr(target, "accepted_amendments", None) is None:
        target.accepted_amendments = DeepMutableList()
    if getattr(target, "available_repos", None) is None:
        target.available_repos = DeepMutableList()
    if getattr(target, "repo_heads", None) is None:
        target.repo_heads = DeepMutableDict()
    if getattr(target, "planning_context", None) is None:
        target.planning_context = DeepMutableDict()
    if getattr(target, "plan_payload", None) is None:
        target.plan_payload = DeepMutableDict()
    if getattr(target, "allowed_paths", None) is None:
        target.allowed_paths = DeepMutableList()
    if getattr(target, "forbidden_paths", None) is None:
        target.forbidden_paths = DeepMutableList()
    if getattr(target, "artifact_requirements", None) is None:
        target.artifact_requirements = DeepMutableList()
    if getattr(target, "artifact_refs", None) is None:
        target.artifact_refs = DeepMutableList()
    if getattr(target, "payload", None) is None:
        target.payload = DeepMutableDict()


event.listen(IntentVersion, "init", _init_json_defaults, propagate=True)
event.listen(PlanningSession, "init", _init_json_defaults, propagate=True)
event.listen(WorkItem, "init", _init_json_defaults, propagate=True)
event.listen(OutcomeEvent, "init", _init_json_defaults, propagate=True)
event.listen(Outcome, "before_insert", _validate_outcome_current_work_item_scope, propagate=True)
event.listen(Outcome, "before_update", _validate_outcome_current_work_item_scope, propagate=True)
event.listen(WorkItem, "before_update", _validate_work_item_reassignment_scope, propagate=True)
event.listen(OutcomeEvent, "before_update", _prevent_outcome_event_update, propagate=True)
event.listen(OutcomeEvent, "before_delete", _prevent_outcome_event_delete, propagate=True)
event.listen(Attempt, "init", _init_json_defaults, propagate=True)
