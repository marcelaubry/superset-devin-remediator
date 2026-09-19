"""phase 3: Slack approval requests, GitHub dispatch tracking and retryable outbox"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_phase3_approvals"
down_revision = "0002_phase2_triage"
branch_labels = None
depends_on = None

_NEW_CASE_STATES = ("APPROVAL_DELIVERY_FAILED", "REMEDIATION_APPROVED", "REMEDIATION_REJECTED")
_NOTIFICATION_STATUSES = ("PENDING", "SENDING", "SENT", "FAILED")
_APPROVAL_DECISIONS = ("PENDING", "APPROVED", "REJECTED", "EXPIRED", "SUPERSEDED")
_DELIVERY_STATUSES = ("NOT_REQUESTED", "PENDING", "LABEL_APPLIED", "CONFIRMED", "FAILED")


def upgrade() -> None:
    for value in _NEW_CASE_STATES:
        op.execute(f"ALTER TYPE case_state ADD VALUE IF NOT EXISTS '{value}'")
    bind = op.get_bind()
    postgresql.ENUM(*_NOTIFICATION_STATUSES, name="notification_status").create(bind)
    postgresql.ENUM(*_APPROVAL_DECISIONS, name="approval_decision").create(bind)
    postgresql.ENUM(*_DELIVERY_STATUSES, name="delivery_status").create(bind)

    op.create_table(
        "approval_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("triage_schema_version", sa.String(50), nullable=False),
        sa.Column("triage_result_hash", sa.String(64), nullable=False),
        sa.Column("action_token_hash", sa.String(64), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "notification_status",
            postgresql.ENUM(name="notification_status", create_type=False),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("slack_channel", sa.String(64), nullable=True),
        sa.Column("slack_message_ts", sa.String(64), nullable=True),
        sa.Column(
            "decision",
            postgresql.ENUM(name="approval_decision", create_type=False),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("decided_by_slack_user_id", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_action_id", sa.String(255), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("label_operation", sa.String(255), nullable=True),
        sa.Column(
            "delivery_status",
            postgresql.ENUM(name="delivery_status", create_type=False),
            nullable=False,
            server_default="NOT_REQUESTED",
        ),
        sa.Column("label_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("label_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("label_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("github_comment_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("attempt_id", name="uq_approval_requests_attempt"),
        sa.UniqueConstraint("action_token_hash", name="uq_approval_requests_action_token_hash"),
    )
    op.create_index("ix_approval_requests_case_id", "approval_requests", ["case_id"])

    op.create_table(
        "slack_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "approval_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("slack_user_id", sa.String(64), nullable=False),
        sa.Column("action_id", sa.String(64), nullable=False),
        sa.Column("action_ts", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_slack_actions_dedupe_key"),
    )

    op.create_table(
        "approval_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "approval_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_approval_events_case_id", "approval_events", ["case_id"])

    op.create_table(
        "slack_fake_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("ts", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("blocks", postgresql.JSONB(), nullable=False),
        sa.Column("message_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("ephemeral", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("update_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel", "ts", name="uq_slack_fake_channel_ts"),
    )

    op.add_column(
        "notification_outbox",
        sa.Column(
            "approval_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_requests.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column(
        "notification_outbox",
        sa.Column("attempts_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "notification_outbox",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column("notification_outbox", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "notification_outbox",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("notification_outbox", sa.Column("claimed_by", sa.String(255), nullable=True))
    op.add_column(
        "notification_outbox",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_notification_outbox_pending",
        "notification_outbox",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_pending", table_name="notification_outbox")
    for name in (
        "lease_expires_at",
        "claimed_by",
        "last_attempt_at",
        "last_error",
        "next_attempt_at",
        "attempts_count",
        "approval_request_id",
    ):
        op.drop_column("notification_outbox", name)
    op.drop_table("slack_fake_messages")
    op.drop_index("ix_approval_events_case_id", table_name="approval_events")
    op.drop_table("approval_events")
    op.drop_table("slack_actions")
    op.drop_index("ix_approval_requests_case_id", table_name="approval_requests")
    op.drop_table("approval_requests")
    bind = op.get_bind()
    postgresql.ENUM(name="delivery_status").drop(bind)
    postgresql.ENUM(name="approval_decision").drop(bind)
    postgresql.ENUM(name="notification_status").drop(bind)
    # PostgreSQL cannot drop enum values; the added case_state values remain.
