"""work item cutover

Revision ID: 7c3b1d5c8f1a
Revises: 3d1fbc5097dc
Create Date: 2026-04-02 09:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7c3b1d5c8f1a"
down_revision: Union[str, Sequence[str], None] = "3d1fbc5097dc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("uq_leases_active_step_id", table_name="leases")

    with op.batch_alter_table("leases", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_leases_exactly_one_target", type_="check")
        batch_op.drop_index("ix_leases_step_id")
        batch_op.drop_column("step_id")
        batch_op.alter_column("work_item_id", existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table("attempts", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_attempts_exactly_one_target", type_="check")
        batch_op.drop_index("ix_attempts_step_id")
        batch_op.drop_column("step_id")
        batch_op.alter_column("work_item_id", existing_type=sa.Integer(), nullable=False)

    op.drop_index("ix_steps_repo", table_name="steps")
    op.drop_index("ix_steps_lane", table_name="steps")
    op.drop_table("steps")
    op.drop_index("ix_cases_intent_id", table_name="cases")
    op.drop_table("cases")


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("work item cutover is intentionally irreversible")
