"""work item cutover

Revision ID: 7c3b1d5c8f1a
Revises: 3d1fbc5097dc
Create Date: 2026-04-02 09:15:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c3b1d5c8f1a"
down_revision: Union[str, Sequence[str], None] = "3d1fbc5097dc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    """Upgrade schema."""
    intent_version_columns = _column_names("intent_versions")
    missing_intent_version_columns = {
        "context": sa.Column("context", sa.Text(), nullable=False, server_default=""),
        "planner_guidance": sa.Column("planner_guidance", sa.Text(), nullable=False, server_default=""),
    }
    if any(column_name not in intent_version_columns for column_name in missing_intent_version_columns):
        with op.batch_alter_table("intent_versions") as batch_op:
            for column_name, column in missing_intent_version_columns.items():
                if column_name not in intent_version_columns:
                    batch_op.add_column(column)

    op.create_table(
        "outcomes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("intent_id", sa.String(length=128), nullable=False),
        sa.Column("intent_version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column(
            "phase",
            sa.Enum(
                "planning",
                "ready",
                "running",
                "completed",
                name="outcomephase",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "result",
            sa.Enum(
                "succeeded",
                "failed",
                "superseded",
                "abandoned",
                name="outcomeresult",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("selected_repo", sa.String(length=255), nullable=True),
        sa.Column("current_work_item_id", sa.Integer(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=False),
        sa.Column("result_commit", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["intent_versions.intent_id", "intent_versions.intent_version"],
            name="fk_outcomes_intent_versions",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outcomes_intent_id", "outcomes", ["intent_id"], unique=False)

    op.create_table(
        "planning_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("outcome_id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("lane", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "materializing",
                "succeeded",
                "failed",
                name="planningsessionstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("planner_provider", sa.String(length=64), nullable=False),
        sa.Column("planner_model", sa.String(length=255), nullable=True),
        sa.Column("available_repos", sa.JSON(), nullable=False),
        sa.Column("repo_heads", sa.JSON(), nullable=False),
        sa.Column("planning_context", sa.JSON(), nullable=False),
        sa.Column("plan_payload", sa.JSON(), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["outcome_id"], ["outcomes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_planning_sessions_outcome_id", "planning_sessions", ["outcome_id"], unique=False)
    op.create_index("ix_planning_sessions_worker_id", "planning_sessions", ["worker_id"], unique=False)
    op.create_index("ix_planning_sessions_lane", "planning_sessions", ["lane"], unique=False)

    op.create_table(
        "work_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("outcome_id", sa.Integer(), nullable=False),
        sa.Column("sequence_index", sa.Integer(), nullable=False),
        sa.Column("blocked_by_work_item_id", sa.Integer(), nullable=True),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("lane", sa.String(length=64), nullable=False),
        sa.Column("work_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "ready",
                "leased",
                "running",
                "verifying",
                "succeeded",
                "failed",
                "revoked",
                name="workitemstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("allowed_paths", sa.JSON(), nullable=False),
        sa.Column("forbidden_paths", sa.JSON(), nullable=False),
        sa.Column("base_commit", sa.String(length=64), nullable=True),
        sa.Column("target_branch", sa.String(length=255), nullable=True),
        sa.Column("artifact_requirements", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["outcome_id"], ["outcomes.id"]),
        sa.ForeignKeyConstraint(
            ["outcome_id", "blocked_by_work_item_id"],
            ["work_items.outcome_id", "work_items.id"],
            name="fk_work_items_blocked_by_same_outcome",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("outcome_id", "id", name="uq_work_items_outcome_id_id"),
        sa.UniqueConstraint("outcome_id", "sequence_index", name="uq_work_items_outcome_sequence_index"),
    )
    op.create_index("ix_work_items_outcome_id", "work_items", ["outcome_id"], unique=False)
    op.create_index("ix_work_items_repo", "work_items", ["repo"], unique=False)
    op.create_index("ix_work_items_lane", "work_items", ["lane"], unique=False)

    op.create_table(
        "outcome_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("outcome_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["outcome_id"], ["outcomes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outcome_events_outcome_id", "outcome_events", ["outcome_id"], unique=False)
    op.create_index("ix_outcome_events_event_type", "outcome_events", ["event_type"], unique=False)

    op.drop_index("ix_verdicts_attempt_id", table_name="verdicts")
    op.drop_table("verdicts")
    op.drop_table("attempts")
    op.drop_table("leases")
    op.drop_table("steps")
    op.drop_table("cases")

    op.create_table(
        "leases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_item_id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("lane", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("heartbeat_deadline", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("expired_at", sa.DateTime(), nullable=True),
        sa.Column("base_commit", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_leases_work_item_id", "leases", ["work_item_id"], unique=False)
    op.create_index("ix_leases_worker_id", "leases", ["worker_id"], unique=False)
    op.create_index("ix_leases_lane", "leases", ["lane"], unique=False)
    op.create_index("ix_leases_heartbeat_deadline", "leases", ["heartbeat_deadline"], unique=False)
    op.create_index("ix_leases_expires_at", "leases", ["expires_at"], unique=False)
    op.create_index(
        "uq_leases_active_work_item_id",
        "leases",
        ["work_item_id"],
        unique=True,
        postgresql_where=sa.text("expired_at IS NULL"),
        sqlite_where=sa.text("expired_at IS NULL"),
    )

    op.create_table(
        "attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_item_id", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column(
            "result_status",
            sa.Enum(
                "pending",
                "submitted",
                "accepted",
                "rejected",
                name="attemptresultstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("artifact_refs", sa.JSON(), nullable=False),
        sa.Column("submitted_diff_ref", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["lease_id"], ["leases.id"]),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lease_id", name="uq_attempts_lease_id"),
    )
    op.create_index("ix_attempts_work_item_id", "attempts", ["work_item_id"], unique=False)
    op.create_index("ix_attempts_repo", "attempts", ["repo"], unique=False)
    op.create_index("ix_attempts_worker_id", "attempts", ["worker_id"], unique=False)

    op.create_table(
        "verdicts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column(
            "result",
            sa.Enum(
                "pass",
                "fail_and_replan",
                "append_governance_step",
                "quarantine",
                "superseded",
                name="verdictresult",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["attempts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_verdicts_attempt_id", "verdicts", ["attempt_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("work item cutover is intentionally irreversible")
