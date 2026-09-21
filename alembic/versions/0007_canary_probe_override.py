"""Canary-only probe override: append-only audit table and per-attempt marker"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_canary_probe_override"
down_revision = "0006_phase5_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canary_probe_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("triage_result_hash", sa.String(64), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("warning", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("case_id", "triage_result_hash", name="uq_canary_override_case_hash"),
    )
    op.add_column(
        "attempts",
        sa.Column(
            "canary_probe_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("attempts", "canary_probe_override")
    op.drop_table("canary_probe_overrides")
