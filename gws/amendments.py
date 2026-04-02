from __future__ import annotations

from datetime import UTC, datetime
import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import AmendmentProposal, Case, IntentVersion, Step, StepStatus


class AmendmentService:
    OPEN_STEP_STATUSES = {
        StepStatus.PLANNING,
        StepStatus.READY,
        StepStatus.LEASED,
        StepStatus.RUNNING,
        StepStatus.VERIFYING,
    }

    def __init__(self, session: Session):
        self.session = session

    def accept_proposal(self, proposal_id: int) -> IntentVersion:
        proposal = self.session.get(AmendmentProposal, proposal_id)
        if proposal is None:
            raise ValueError(f"unknown proposal_id: {proposal_id}")
        if proposal.status != "pending":
            raise ValueError(f"proposal {proposal_id} is not pending")

        prior_intent = (
            self.session.query(IntentVersion)
            .filter(
                IntentVersion.intent_id == proposal.intent_id,
                IntentVersion.intent_version == proposal.base_intent_version,
            )
            .one_or_none()
        )
        if prior_intent is None:
            raise ValueError(
                f"unknown intent version for intent_id={proposal.intent_id} version={proposal.base_intent_version}"
            )
        latest_intent_version = (
            self.session.query(IntentVersion.intent_version)
            .filter(IntentVersion.intent_id == proposal.intent_id)
            .order_by(IntentVersion.intent_version.desc())
            .limit(1)
            .scalar()
        )
        if latest_intent_version != proposal.base_intent_version:
            raise ValueError("proposal base intent version is stale")

        accepted_summary = {
            "summary": proposal.summary,
            "is_breaking": proposal.is_breaking,
        }
        new_intent = IntentVersion(
            intent_id=prior_intent.intent_id,
            intent_version=prior_intent.intent_version + 1,
            brief_text=proposal.amended_brief_text,
            accepted_amendments=[*json.loads(json.dumps(prior_intent.accepted_amendments)), accepted_summary],
        )
        self.session.add(new_intent)

        if proposal.is_breaking:
            self._revoke_open_steps(prior_intent.intent_id, prior_intent.intent_version)

        proposal.status = "accepted"
        proposal.accepted_at = datetime.now(UTC).replace(tzinfo=None)

        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ValueError("proposal base intent version is stale") from exc
        return new_intent

    def _revoke_open_steps(self, intent_id: str, intent_version: int) -> None:
        steps = (
            self.session.query(Step)
            .join(Case)
            .filter(
                Case.intent_id == intent_id,
                Case.intent_version == intent_version,
                Step.status.in_(self.OPEN_STEP_STATUSES),
            )
            .all()
        )
        for step in steps:
            step.status = StepStatus.REVOKED
