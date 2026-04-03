"""intent completion

Revision ID: b7d3f2a41e9c
Revises: a4c2e8f91b3d
Create Date: 2026-04-03 14:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d3f2a41e9c"
down_revision: Union[str, None] = "a4c2e8f91b3d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "intent_versions",
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
    )
    op.create_check_constraint(
        "ck_intent_versions_status_intentstatus",
        "intent_versions",
        sa.column("status").in_(["active", "satisfied"]),
    )


def downgrade() -> None:
    op.drop_constraint("ck_intent_versions_status_intentstatus", "intent_versions", type_="check")
    op.drop_column("intent_versions", "status")
