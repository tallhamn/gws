"""data model cleanup

Revision ID: a4c2e8f91b3d
Revises: 7c3b1d5c8f1a
Create Date: 2026-04-03 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c2e8f91b3d"
down_revision: Union[str, None] = "7c3b1d5c8f1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add description column to work_items (defaults empty string)
    op.add_column("work_items", sa.Column("description", sa.Text(), nullable=False, server_default=""))

    # Replace bare varchar status on amendment_proposals with constrained enum
    # The existing values ("pending", "accepted") are valid members of the new enum,
    # so no data migration is needed — just add the check constraint.
    op.create_check_constraint(
        "ck_amendment_proposals_status_amendmentproposalstatus",
        "amendment_proposals",
        sa.column("status").in_(["pending", "accepted"]),
    )


def downgrade() -> None:
    op.drop_constraint("ck_amendment_proposals_status_amendmentproposalstatus", "amendment_proposals", type_="check")
    op.drop_column("work_items", "description")
