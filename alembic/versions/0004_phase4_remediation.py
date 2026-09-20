"""phase 4: remediation lifecycle states, immutable probe snapshots, PR/probe/CI evidence"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_phase4_remediation"
down_revision = "0003_phase3_approvals"
branch_labels = None
depends_on = None

_NEW_CASE_STATES = (
    "REMEDIATION_RECONCILING_CREATE",
    "REMEDIATION_HUMAN_BLOCKED",
    "PR_DISCOVERED",
    "PR_VALIDATING",
    "PROBE_VALIDATING_BASE",
    "PROBE_VALIDATING_HEAD",
    "PROBE_INFRASTRUCTURE_BLOCKED",
    "CI_FAILED",
    "REMEDIATION_FAILED",
    "REMEDIATION_TERMINATION_PENDING",
    "REMEDIATION_TIMED_OUT",
    "REMEDIATION_CANCELLED",
)
_PROBE_TARGETS = ("BASE", "HEAD")
_PROBE_VERDICTS = ("MATCHED", "MISMATCHED", "INFRASTRUCTURE")


def _uuid_fk(column: str, target: str, ondelete: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        column,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
    )


def _ts(name: str, *, nullable: bool = True, default: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if default else None,
    )


def upgrade() -> None:
    for value in _NEW_CASE_STATES:
        op.execute(f"ALTER TYPE case_state ADD VALUE IF NOT EXISTS '{value}'")
    bind = op.get_bind()
    postgresql.ENUM(*_PROBE_TARGETS, name="probe_target").create(bind)
    postgresql.ENUM(*_PROBE_VERDICTS, name="probe_verdict").create(bind)

    op.create_table(
        "probe_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid_fk("case_id", "cases.id", "CASCADE"),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("issue_number", sa.Integer, nullable=False),
        sa.Column("probe_identifier", sa.String(255), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("manifest_path", sa.Text, nullable=False),
        sa.Column("script_path", sa.Text, nullable=False),
        sa.Column("manifest", postgresql.JSONB, nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("script_hash", sa.String(64), nullable=False),
        sa.Column("script_content", sa.Text, nullable=False),
        sa.Column("registry_commit", sa.String(64), nullable=True),
        sa.Column("expected_base_exit_code", sa.Integer, nullable=False),
        sa.Column("expected_head_exit_code", sa.Integer, nullable=False),
        sa.Column("timeout_seconds", sa.Integer, nullable=False),
        sa.Column("runtime", postgresql.JSONB, nullable=False),
        _ts("created_at", nullable=False, default=True),
    )
    op.create_index("ix_probe_snapshots_case_id", "probe_snapshots", ["case_id"])

    op.add_column(
        "attempts",
        sa.Column(
            "approval_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "approval_requests.id",
                ondelete="SET NULL",
                name="fk_attempts_approval_request_id",
            ),
            nullable=True,
        ),
    )
    op.add_column("attempts", sa.Column("triage_result_hash", sa.String(64), nullable=True))
    op.add_column(
        "attempts",
        _uuid_fk("probe_snapshot_id", "probe_snapshots.id", "SET NULL", nullable=True),
    )
    op.add_column("attempts", sa.Column("devin_pull_requests", postgresql.JSONB, nullable=True))
    op.add_column("attempts", sa.Column("pr_url", sa.Text, nullable=True))
    op.add_column("attempts", sa.Column("pr_number", sa.Integer, nullable=True))
    op.add_column("attempts", sa.Column("branch", sa.String(255), nullable=True))
    op.add_column("attempts", sa.Column("head_sha", sa.String(64), nullable=True))
    op.add_column("attempts", _ts("ci_deadline_at"))
    op.add_column("attempts", sa.Column("failure_stage", sa.String(64), nullable=True))
    op.add_column("attempts", sa.Column("failure_class", sa.String(32), nullable=True))

    op.create_table(
        "probe_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid_fk("case_id", "cases.id", "CASCADE"),
        _uuid_fk("attempt_id", "attempts.id", "CASCADE", nullable=True),
        _uuid_fk("probe_snapshot_id", "probe_snapshots.id", "CASCADE"),
        sa.Column(
            "target", postgresql.ENUM(name="probe_target", create_type=False), nullable=False
        ),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("script_hash", sa.String(64), nullable=False),
        sa.Column("runner_mode", sa.String(32), nullable=False),
        sa.Column("command_identity", sa.Text, nullable=False),
        sa.Column("expected_exit_code", sa.Integer, nullable=False),
        sa.Column("exit_code", sa.Integer, nullable=True),
        sa.Column(
            "verdict", postgresql.ENUM(name="probe_verdict", create_type=False), nullable=False
        ),
        sa.Column("timed_out", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("stdout", sa.Text, nullable=False, server_default=""),
        sa.Column("stderr", sa.Text, nullable=False, server_default=""),
        sa.Column("output_truncated", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("error", sa.Text, nullable=True),
        _ts("started_at", nullable=False, default=True),
        _ts("finished_at"),
    )
    op.create_index("ix_probe_executions_attempt_id", "probe_executions", ["attempt_id"])

    op.create_table(
        "pull_request_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid_fk("case_id", "cases.id", "CASCADE"),
        _uuid_fk("attempt_id", "attempts.id", "CASCADE"),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("pr_number", sa.Integer, nullable=False),
        sa.Column("pr_url", sa.Text, nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("draft", sa.Boolean, nullable=False),
        sa.Column("merged", sa.Boolean, nullable=False),
        sa.Column("base_ref", sa.String(255), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("head_ref", sa.String(255), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("head_repository", sa.String(255), nullable=True),
        sa.Column("author_login", sa.String(255), nullable=True),
        sa.Column("author_type", sa.String(64), nullable=True),
        sa.Column("compare_status", sa.String(32), nullable=True),
        sa.Column("ahead_by", sa.Integer, nullable=True),
        sa.Column("behind_by", sa.Integer, nullable=True),
        sa.Column("changed_files", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("closing_reference_source", sa.String(32), nullable=True),
        sa.Column("closing_issue_numbers", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("checks", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("valid", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("verdict", sa.Text, nullable=False, server_default=""),
        _ts("created_at", nullable=False, default=True),
    )
    op.create_index("ix_pull_request_evidence_attempt_id", "pull_request_evidence", ["attempt_id"])

    op.create_table(
        "ci_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid_fk("case_id", "cases.id", "CASCADE"),
        _uuid_fk("attempt_id", "attempts.id", "CASCADE"),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("overall", sa.String(32), nullable=False),
        sa.Column("checks", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("required_checks", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("missing_required", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        _ts("observed_at", nullable=False, default=True),
    )
    op.create_index("ix_ci_snapshots_attempt_id", "ci_snapshots", ["attempt_id"])


def downgrade() -> None:
    op.drop_index("ix_ci_snapshots_attempt_id", table_name="ci_snapshots")
    op.drop_table("ci_snapshots")
    op.drop_index("ix_pull_request_evidence_attempt_id", table_name="pull_request_evidence")
    op.drop_table("pull_request_evidence")
    op.drop_index("ix_probe_executions_attempt_id", table_name="probe_executions")
    op.drop_table("probe_executions")
    for name in (
        "failure_class",
        "failure_stage",
        "ci_deadline_at",
        "head_sha",
        "branch",
        "pr_number",
        "pr_url",
        "devin_pull_requests",
        "probe_snapshot_id",
        "triage_result_hash",
        "approval_request_id",
    ):
        op.drop_column("attempts", name)
    op.drop_index("ix_probe_snapshots_case_id", table_name="probe_snapshots")
    op.drop_table("probe_snapshots")
    bind = op.get_bind()
    postgresql.ENUM(name="probe_verdict").drop(bind)
    postgresql.ENUM(name="probe_target").drop(bind)
    # PostgreSQL cannot drop enum values; the added case_state values remain.
