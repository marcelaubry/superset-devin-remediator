"""Acceptance-probe policy: decision table and per-attempt probe status

`canary_probe_overrides` and `attempts.canary_probe_override` are retained untouched as
historical evidence of cases dispatched under the removed override.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008_probe_policy"
down_revision = "0007_canary_probe_override"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "probe_policy_decisions",
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
        sa.Column("policy", sa.String(32), nullable=False),
        sa.Column("probe_status", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("case_id", "triage_result_hash", name="uq_probe_policy_case_hash"),
    )
    op.add_column(
        "attempts",
        sa.Column(
            "probe_status",
            sa.String(32),
            nullable=False,
            server_default="verified",
        ),
    )
    # Historical attempts dispatched under the override never had probe evidence either.
    op.execute(
        "UPDATE attempts SET probe_status = 'not_configured' WHERE canary_probe_override IS TRUE"
    )


def downgrade() -> None:
    op.drop_column("attempts", "probe_status")
    op.drop_table("probe_policy_decisions")
