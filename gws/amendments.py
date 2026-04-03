from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import AmendmentProposal, AmendmentProposalStatus, IntentVersion, Outcome, WorkItem, WorkItemStatus

logger = logging.getLogger(__name__)


class AmendmentService:
    OPEN_WORK_ITEM_STATUSES = {
        WorkItemStatus.READY,
        WorkItemStatus.LEASED,
        WorkItemStatus.RUNNING,
        WorkItemStatus.VERIFYING,
    }

    def __init__(self, session: Session):
        self.session = session

    def accept_proposal(self, proposal_id: int) -> IntentVersion:
        proposal = self.session.get(AmendmentProposal, proposal_id)
        if proposal is None:
            logger.warning("Unknown proposal_id: %d", proposal_id)
            raise ValueError(f"unknown proposal_id: {proposal_id}")
        if proposal.status != AmendmentProposalStatus.PENDING:
            logger.warning("Proposal %d is not pending (status=%s)", proposal_id, proposal.status)
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
            self._revoke_open_work_items(prior_intent.intent_id, prior_intent.intent_version)

        proposal.status = AmendmentProposalStatus.ACCEPTED
        proposal.accepted_at = datetime.now(timezone.utc).replace(tzinfo=None)

        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            logger.warning("Proposal %d acceptance failed: base intent version is stale", proposal_id)
            raise ValueError("proposal base intent version is stale") from exc
        logger.info(
            "Amendment proposal %d accepted, intent %s v%d -> v%d",
            proposal_id,
            prior_intent.intent_id,
            prior_intent.intent_version,
            new_intent.intent_version,
        )
        return new_intent

    def _revoke_open_work_items(self, intent_id: str, intent_version: int) -> None:
        work_items = (
            self.session.query(WorkItem)
            .join(Outcome, WorkItem.outcome_id == Outcome.id)
            .filter(
                Outcome.intent_id == intent_id,
                Outcome.intent_version == intent_version,
                WorkItem.status.in_(self.OPEN_WORK_ITEM_STATUSES),
            )
            .all()
        )
        for work_item in work_items:
            work_item.status = WorkItemStatus.REVOKED
        logger.info(
            "Breaking amendment: revoked %d open work items for intent %s v%d",
            len(work_items),
            intent_id,
            intent_version,
        )
