"""add target_branch to intent_versions

Revision ID: c8f4a1d2e7b6
Revises: b7d3f2a41e9c
Create Date: 2026-04-03 18:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f4a1d2e7b6"
down_revision: Union[str, None] = "b7d3f2a41e9c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("intent_versions", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column("target_branch", sa.String(255), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("intent_versions", recreate="always") as batch_op:
        batch_op.drop_column("target_branch")
