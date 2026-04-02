import enum
from datetime import datetime, timezone
from typing import Optional
import weakref

from sqlalchemy import DateTime, Enum, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint, event, text
from sqlalchemy.ext.mutable import Mutable, MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class StepStatus(str, enum.Enum):
    PLANNING = "planning"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REVOKED = "revoked"


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    cases: Mapped[list["Case"]] = relationship(back_populates="intent_version_ref")


class AmendmentProposal(Base):
    __tablename__ = "amendment_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(128), index=True)
    base_intent_version: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    amended_brief_text: Mapped[str] = mapped_column(Text)
    is_breaking: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class PullRequest(Base):
    __tablename__ = "pull_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(128), index=True)
    lane: Mapped[str] = mapped_column(String(64), index=True)
    intent_id: Mapped[str] = mapped_column(String(128), index=True)
    repo_access_set: Mapped[list[str]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    envelope: Mapped[dict] = mapped_column(DeepMutableDict.as_mutable(JSON), default=dict)
    repo_heads: Mapped[dict] = mapped_column(DeepMutableDict.as_mutable(JSON), default=dict)
    planning_result: Mapped[dict] = mapped_column(DeepMutableDict.as_mutable(JSON), default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending")


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["intent_versions.intent_id", "intent_versions.intent_version"],
            name="fk_cases_intent_versions",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(128), index=True)
    intent_version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="open")
    workflow_status: Mapped[str] = mapped_column(String(32), default="active")
    steps: Mapped[list["Step"]] = relationship(back_populates="case")
    intent_version_ref: Mapped[IntentVersion] = relationship(back_populates="cases")


class Step(Base):
    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    repo: Mapped[str] = mapped_column(String(255), index=True)
    lane: Mapped[str] = mapped_column(String(64), index=True)
    step_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[StepStatus] = mapped_column(
        Enum(
            StepStatus,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            validate_strings=True,
            native_enum=False,
            create_constraint=True,
        ),
        default=StepStatus.PLANNING,
    )
    allowed_paths: Mapped[list[str]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    forbidden_paths: Mapped[list[str]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    base_commit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    target_branch: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    artifact_requirements: Mapped[list[str]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    case: Mapped[Case] = relationship(back_populates="steps")
    leases: Mapped[list["Lease"]] = relationship(back_populates="step")
    attempts: Mapped[list["Attempt"]] = relationship(back_populates="step")


class Lease(Base):
    __tablename__ = "leases"
    __table_args__ = (
        Index(
            "uq_leases_active_step_id",
            "step_id",
            unique=True,
            postgresql_where=text("expired_at IS NULL"),
            sqlite_where=text("expired_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id"), index=True)
    worker_id: Mapped[str] = mapped_column(String(128), index=True)
    lane: Mapped[str] = mapped_column(String(64), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    heartbeat_deadline: Mapped[datetime] = mapped_column(DateTime, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    expired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    base_commit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    step: Mapped[Step] = relationship(back_populates="leases")
    attempt: Mapped[Optional["Attempt"]] = relationship(back_populates="lease", uselist=False)


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("lease_id", name="uq_attempts_lease_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id"), index=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), index=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    result_status: Mapped[AttemptResultStatus] = mapped_column(
        Enum(
            AttemptResultStatus,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            validate_strings=True,
            native_enum=False,
            create_constraint=True,
        ),
        default=AttemptResultStatus.PENDING,
    )
    artifact_refs: Mapped[list[str]] = mapped_column(DeepMutableList.as_mutable(JSON), default=list)
    submitted_diff_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    step: Mapped[Step] = relationship(back_populates="attempts")
    lease: Mapped[Lease] = relationship(back_populates="attempt")
    verdicts: Mapped[list["Verdict"]] = relationship(back_populates="attempt")


class Verdict(Base):
    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id"), index=True)
    result: Mapped[VerdictResult] = mapped_column(
        Enum(
            VerdictResult,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            validate_strings=True,
            native_enum=False,
            create_constraint=True,
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    attempt: Mapped[Attempt] = relationship(back_populates="verdicts")


def _init_json_defaults(target, args, kwargs):
    del args
    if getattr(target, "accepted_amendments", None) is None:
        target.accepted_amendments = DeepMutableList()
    if getattr(target, "repo_access_set", None) is None:
        target.repo_access_set = DeepMutableList()
    if getattr(target, "envelope", None) is None:
        target.envelope = DeepMutableDict()
    if getattr(target, "repo_heads", None) is None:
        target.repo_heads = DeepMutableDict()
    if getattr(target, "planning_result", None) is None:
        target.planning_result = DeepMutableDict()
    if getattr(target, "allowed_paths", None) is None:
        target.allowed_paths = DeepMutableList()
    if getattr(target, "forbidden_paths", None) is None:
        target.forbidden_paths = DeepMutableList()
    if getattr(target, "artifact_requirements", None) is None:
        target.artifact_requirements = DeepMutableList()
    if getattr(target, "artifact_refs", None) is None:
        target.artifact_refs = DeepMutableList()


event.listen(IntentVersion, "init", _init_json_defaults, propagate=True)
event.listen(PullRequest, "init", _init_json_defaults, propagate=True)
event.listen(Step, "init", _init_json_defaults, propagate=True)
event.listen(Attempt, "init", _init_json_defaults, propagate=True)
