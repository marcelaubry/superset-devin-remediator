"""initial schema"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    case_state = postgresql.ENUM(
        "RECEIVED",
        "ELIGIBILITY_EVALUATED",
        "TRIAGE_CREATE_INTENT",
        "TRIAGING",
        "TRIAGED",
        "AWAITING_REMEDIATION_APPROVAL",
        "REMEDIATION_CREATE_INTENT",
        "REMEDIATING",
        "OUTPUT_VALIDATING",
        "PR_VALIDATED",
        "CI_PENDING",
        "CI_PASSED",
        "HUMAN_BLOCKED",
        "RECONCILING_CREATE",
        "TERMINATION_PENDING",
        "TIMED_OUT",
        "POLICY_REJECTED",
        "FAILED",
        "CANCELLED",
        name="case_state",
    )
    case_state.create(op.get_bind())
    for name, values in [
        ("event_status", ["PENDING", "PROCESSING", "PROCESSED", "FAILED"]),
        ("attempt_kind", ["TRIAGE", "REMEDIATION"]),
        ("attempt_status", ["RUNNING", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"]),
        (
            "recommendation",
            [
                "ELIGIBLE_FOR_DEVIN_TRIAGE",
                "NEEDS_SCOPING",
                "USE_DETERMINISTIC_AUTOMATION",
                "HUMAN_LED",
            ],
        ),
        ("outbox_channel", ["github", "slack"]),
        ("outbox_status", ["PENDING", "SENT", "FAILED"]),
    ]:
        postgresql.ENUM(*values, name=name).create(op.get_bind())
    u = postgresql.UUID(as_uuid=True)
    j = postgresql.JSONB()
    op.create_table(
        "cases",
        sa.Column("id", u, primary_key=True),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("issue_title", sa.Text(), nullable=False),
        sa.Column("issue_url", sa.Text(), nullable=False),
        sa.Column("state", postgresql.ENUM(name="case_state", create_type=False), nullable=False),
        sa.Column("recommendation", postgresql.ENUM(name="recommendation", create_type=False)),
        sa.Column("rubric", j),
        sa.Column("devin_session_id", sa.String(255)),
        sa.Column("devin_session_url", sa.Text()),
        sa.Column("pr_number", sa.Integer()),
        sa.Column("pr_url", sa.Text()),
        sa.Column("ci_status", sa.String(100)),
        sa.Column(
            "state_entered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failure_reason", sa.Text()),
        sa.UniqueConstraint("repository", "issue_number", name="uq_case_repo_issue"),
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", u, primary_key=True),
        sa.Column("delivery_id", sa.String(255), nullable=False, unique=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("payload", j, nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "status", postgresql.ENUM(name="event_status", create_type=False), nullable=False
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("attempts_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("case_id", u, sa.ForeignKey("cases.id", ondelete="SET NULL")),
    )
    op.create_table(
        "attempts",
        sa.Column("id", u, primary_key=True),
        sa.Column("case_id", u, sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", postgresql.ENUM(name="attempt_kind", create_type=False), nullable=False),
        sa.Column("devin_session_id", sa.String(255), nullable=False),
        sa.Column(
            "status", postgresql.ENUM(name="attempt_status", create_type=False), nullable=False
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
    )
    op.create_table(
        "state_transitions",
        sa.Column("id", u, primary_key=True),
        sa.Column("case_id", u, sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "from_state", postgresql.ENUM(name="case_state", create_type=False), nullable=True
        ),
        sa.Column(
            "to_state", postgresql.ENUM(name="case_state", create_type=False), nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(50), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", u, primary_key=True),
        sa.Column("case_id", u, sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "channel", postgresql.ENUM(name="outbox_channel", create_type=False), nullable=False
        ),
        sa.Column("kind", sa.String(100), nullable=False),
        sa.Column("payload", j, nullable=False),
        sa.Column(
            "status", postgresql.ENUM(name="outbox_status", create_type=False), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_cases_state", "cases", ["state"])
    op.create_index("ix_webhook_events_delivery_id", "webhook_events", ["delivery_id"])


def downgrade() -> None:
    for table in [
        "notification_outbox",
        "state_transitions",
        "attempts",
        "webhook_events",
        "cases",
    ]:
        op.drop_table(table)
    for name in [
        "outbox_status",
        "outbox_channel",
        "recommendation",
        "attempt_status",
        "attempt_kind",
        "event_status",
        "case_state",
    ]:
        postgresql.ENUM(name=name).drop(op.get_bind())
