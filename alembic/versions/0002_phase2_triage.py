"""phase 2: durable Devin triage intents, session telemetry and structured output"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_phase2_triage"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_NEW_ATTEMPT_STATUSES = ("RECONCILING", "TERMINATION_PENDING", "TIMED_OUT")
_CREATE_STATES = (
    "PENDING",
    "NOT_SENT",
    "CREATED",
    "UNCERTAIN",
    "RECONCILED",
    "API_ERROR",
    "UNRESOLVED",
)


def upgrade() -> None:
    for value in _NEW_ATTEMPT_STATUSES:
        op.execute(f"ALTER TYPE attempt_status ADD VALUE IF NOT EXISTS '{value}'")
    postgresql.ENUM(*_CREATE_STATES, name="create_state").create(op.get_bind())

    op.add_column("attempts", sa.Column("operation_key", sa.String(255), nullable=True))
    op.execute("UPDATE attempts SET operation_key = 'op:' || idempotency_key")
    op.alter_column("attempts", "operation_key", nullable=False)
    op.create_unique_constraint("uq_attempts_operation_key", "attempts", ["operation_key"])

    op.add_column(
        "attempts",
        sa.Column(
            "create_state",
            postgresql.ENUM(name="create_state", create_type=False),
            nullable=False,
            server_default="CREATED",
        ),
    )
    op.alter_column("attempts", "create_state", server_default=None)
    for column in (
        sa.Column("devin_session_url", sa.Text()),
        sa.Column("devin_tags", postgresql.JSONB()),
        sa.Column("devin_status", sa.String(50)),
        sa.Column("devin_status_detail", sa.String(100)),
        sa.Column("devin_acus_consumed", sa.Float()),
        sa.Column("base_sha", sa.String(64)),
        sa.Column("prompt_version", sa.String(50)),
        sa.Column("max_acu_limit", sa.Integer()),
        sa.Column("create_sent_at", sa.DateTime(timezone=True)),
        sa.Column("first_polled_at", sa.DateTime(timezone=True)),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column("poll_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("timeout_at", sa.DateTime(timezone=True)),
        sa.Column("structured_output", postgresql.JSONB()),
        sa.Column("reconciliation_reason", sa.Text()),
    ):
        op.add_column("attempts", column)
    op.execute(
        "UPDATE attempts SET finished_at = COALESCE(finished_at, started_at) "
        "WHERE status <> 'RUNNING' AND finished_at IS NULL"
    )
    op.create_index(
        "uq_attempts_one_active_per_kind",
        "attempts",
        ["case_id", "kind"],
        unique=True,
        postgresql_where=sa.text("finished_at IS NULL"),
    )
    op.create_index("ix_attempts_devin_session_id", "attempts", ["devin_session_id"])


def downgrade() -> None:
    op.drop_index("ix_attempts_devin_session_id", table_name="attempts")
    op.drop_index("uq_attempts_one_active_per_kind", table_name="attempts")
    for name in (
        "reconciliation_reason",
        "structured_output",
        "timeout_at",
        "poll_count",
        "last_polled_at",
        "first_polled_at",
        "create_sent_at",
        "max_acu_limit",
        "prompt_version",
        "base_sha",
        "devin_acus_consumed",
        "devin_status_detail",
        "devin_status",
        "devin_tags",
        "devin_session_url",
        "create_state",
    ):
        op.drop_column("attempts", name)
    op.drop_constraint("uq_attempts_operation_key", "attempts", type_="unique")
    op.drop_column("attempts", "operation_key")
    postgresql.ENUM(name="create_state").drop(op.get_bind())
    # PostgreSQL cannot drop enum values; the added attempt_status values remain.
