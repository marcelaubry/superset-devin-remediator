"""Phase 5: database-backed capacity leases, capacity-wait visibility, official ACU report"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_phase5_capacity"
down_revision = "0005_attempt_ordinal"
branch_labels = None
depends_on = None

capacity_lease_kind = postgresql.ENUM(
    "TRIAGE", "REMEDIATION", "PROBE", "RESOURCE", name="capacity_lease_kind", create_type=False
)


def upgrade() -> None:
    capacity_lease_kind.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "capacity_leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", capacity_lease_kind, nullable=False),
        sa.Column("scope", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("attempts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column(
            "acquired_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "uq_capacity_leases_one_active_per_case_kind",
        "capacity_leases",
        ["case_id", "kind", "scope"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "ix_capacity_leases_kind_active",
        "capacity_leases",
        ["kind", "expires_at", "released_at"],
    )
    op.add_column("cases", sa.Column("waiting_for", sa.String(255), nullable=True))
    op.add_column("cases", sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("attempts", sa.Column("acu_report_status", sa.String(32), nullable=True))
    op.add_column("attempts", sa.Column("acu_reported", sa.Float(), nullable=True))
    op.add_column("attempts", sa.Column("acu_report_detail", sa.Text(), nullable=True))
    op.add_column(
        "attempts", sa.Column("acu_reported_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("probe_executions", sa.Column("request_id", sa.String(32), nullable=True))
    op.add_column("probe_executions", sa.Column("failure_stage", sa.String(32), nullable=True))
    op.add_column(
        "probe_executions",
        sa.Column("tool_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("probe_executions", "tool_versions")
    op.drop_column("probe_executions", "failure_stage")
    op.drop_column("probe_executions", "request_id")
    op.drop_column("attempts", "acu_reported_at")
    op.drop_column("attempts", "acu_report_detail")
    op.drop_column("attempts", "acu_reported")
    op.drop_column("attempts", "acu_report_status")
    op.drop_column("cases", "waiting_since")
    op.drop_column("cases", "waiting_for")
    op.drop_index("ix_capacity_leases_kind_active", table_name="capacity_leases")
    op.drop_index("uq_capacity_leases_one_active_per_case_kind", table_name="capacity_leases")
    op.drop_table("capacity_leases")
    capacity_lease_kind.drop(op.get_bind(), checkfirst=True)
