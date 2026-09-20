import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .lifecycle import CaseState


class Base(DeclarativeBase):
    pass


class EventStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class AttemptKind(str, enum.Enum):
    TRIAGE = "TRIAGE"
    REMEDIATION = "REMEDIATION"


class AttemptStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    RECONCILING = "RECONCILING"
    TERMINATION_PENDING = "TERMINATION_PENDING"
    TIMED_OUT = "TIMED_OUT"


ACTIVE_ATTEMPT_STATUSES = frozenset(
    {AttemptStatus.RUNNING, AttemptStatus.RECONCILING, AttemptStatus.TERMINATION_PENDING}
)

UNRESOLVED_CREATE_ACK = "operator confirmed no live Devin session carries this operation key"
CANCEL_TERMINATION_REASON = "operator requested cancel"
WORKER_ERROR_TERMINATION_PREFIX = "worker error"


class CreateState(str, enum.Enum):
    """Outcome of the single POST /sessions issued for an attempt."""

    PENDING = "PENDING"
    NOT_SENT = "NOT_SENT"
    CREATED = "CREATED"
    UNCERTAIN = "UNCERTAIN"
    RECONCILED = "RECONCILED"
    API_ERROR = "API_ERROR"
    UNRESOLVED = "UNRESOLVED"


class Recommendation(str, enum.Enum):
    ELIGIBLE_FOR_DEVIN_TRIAGE = "ELIGIBLE_FOR_DEVIN_TRIAGE"
    NEEDS_SCOPING = "NEEDS_SCOPING"
    USE_DETERMINISTIC_AUTOMATION = "USE_DETERMINISTIC_AUTOMATION"
    HUMAN_LED = "HUMAN_LED"


class OutboxChannel(str, enum.Enum):
    GITHUB = "github"
    SLACK = "slack"


class OutboxStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class NotificationStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class ApprovalDecision(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class DeliveryStatus(str, enum.Enum):
    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    LABEL_APPLIED = "LABEL_APPLIED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


OUTBOX_KIND_SLACK_APPROVAL_REQUEST = "remediation_approval_requested"
OUTBOX_KIND_SLACK_STATUS_UPDATE = "approval_status_update"
OUTBOX_KIND_GITHUB_NOT_FEASIBLE = "triage_not_feasible"
OUTBOX_KIND_GITHUB_APPLY_LABEL = "apply_remediation_label"
OUTBOX_KIND_GITHUB_REJECTION_COMMENT = "rejection_comment"
OUTBOX_KIND_SLACK_EPHEMERAL_RESPONSE = "slack_ephemeral_response"
# Phase 4: progress of the remediation pipeline rendered onto the approval Slack message.
OUTBOX_KIND_SLACK_REMEDIATION_UPDATE = "remediation_status_update"
# Phase 1/2 record-only intents: kept on the outbox as an audit trail, never delivered.
OUTBOX_RECORD_ONLY_KINDS: frozenset[str] = frozenset(
    {
        "eligibility_rejected",
        "case_completed",
        "case_failed",
        "human_blocked",
        "case_cancelled",
        "case_timed_out",
        "remediation_failed",
        "remediation_human_blocked",
        "remediation_cancelled",
        "remediation_timed_out",
        "remediation_ready_for_review",
    }
)


class ProbeTarget(str, enum.Enum):
    BASE = "BASE"
    HEAD = "HEAD"


class ProbeVerdict(str, enum.Enum):
    """Outcome of one independent probe execution."""

    MATCHED = "MATCHED"  # exit code equals the approved expectation
    MISMATCHED = "MISMATCHED"  # probe ran to completion (or timed out) with another exit code
    INFRASTRUCTURE = "INFRASTRUCTURE"  # checkout/runtime failure; says nothing about the code


FAILURE_CLASS_INFRASTRUCTURE = "infrastructure"
FAILURE_CLASS_VERIFICATION = "verification"
FAILURE_CLASS_POLICY = "policy"
FAILURE_CLASS_SESSION = "session"


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("delivery_id", name="webhook_events_delivery_id_key"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    delivery_id: Mapped[str] = mapped_column(String(255), index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    repository: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus, name="event_status"), default=EventStatus.PENDING
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    case: Mapped["Case | None"] = relationship(back_populates="events")


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("repository", "issue_number", name="uq_case_repo_issue"),
        Index("ix_cases_state", "state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_number: Mapped[int] = mapped_column(Integer)
    repository: Mapped[str] = mapped_column(String(255))
    issue_title: Mapped[str] = mapped_column(Text)
    issue_url: Mapped[str] = mapped_column(Text)
    state: Mapped[CaseState] = mapped_column(
        Enum(CaseState, name="case_state"), default=CaseState.RECEIVED
    )
    recommendation: Mapped[Recommendation | None] = mapped_column(
        Enum(Recommendation, name="recommendation")
    )
    rubric: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    devin_session_id: Mapped[str | None] = mapped_column(String(255))
    devin_session_url: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(Text)
    ci_status: Mapped[str | None] = mapped_column(String(100))
    state_entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set while the case is parked because a configured concurrency limit is saturated;
    # cleared when capacity is acquired. Waiting spends nothing and changes no state.
    waiting_for: Mapped[str | None] = mapped_column(String(255))
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    events: Mapped[list[WebhookEvent]] = relationship(back_populates="case")
    attempts: Mapped[list["Attempt"]] = relationship(back_populates="case")
    transitions: Mapped[list["StateTransition"]] = relationship(
        back_populates="case", order_by="StateTransition.seq"
    )
    approval_requests: Mapped[list["ApprovalRequest"]] = relationship(
        back_populates="case", order_by="ApprovalRequest.created_at"
    )
    outbox: Mapped[list["NotificationOutbox"]] = relationship(
        order_by="NotificationOutbox.created_at"
    )
    probe_snapshots: Mapped[list["ProbeSnapshot"]] = relationship(
        order_by="ProbeSnapshot.created_at"
    )


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        Index(
            "uq_attempts_one_active_per_kind",
            "case_id",
            "kind",
            unique=True,
            postgresql_where=text("finished_at IS NULL"),
        ),
        Index("ix_attempts_devin_session_id", "devin_session_id"),
        Index(
            "uq_attempts_case_kind_ordinal",
            "case_id",
            "kind",
            "ordinal",
            unique=True,
            postgresql_where=text("ordinal IS NOT NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    kind: Mapped[AttemptKind] = mapped_column(Enum(AttemptKind, name="attempt_kind"))
    # 1-based per (case, kind); allocated under a case row lock (DevinRunner._create) and
    # backed by the partial unique index above. Legacy rows may be NULL.
    ordinal: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    # Full operation identity (case, phase, complete triage-result hash, complete base SHA,
    # ordinal); also the exact Devin session tag. Never truncated.
    operation_key: Mapped[str] = mapped_column(String(255), unique=True)
    create_state: Mapped[CreateState] = mapped_column(
        Enum(CreateState, name="create_state"), default=CreateState.PENDING
    )
    devin_session_id: Mapped[str | None] = mapped_column(String(255))
    devin_session_url: Mapped[str | None] = mapped_column(Text)
    devin_tags: Mapped[list[str] | None] = mapped_column(JSONB)
    devin_status: Mapped[str | None] = mapped_column(String(50))
    devin_status_detail: Mapped[str | None] = mapped_column(String(100))
    devin_acus_consumed: Mapped[float | None] = mapped_column(Float)
    base_sha: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    max_acu_limit: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[AttemptStatus] = mapped_column(
        Enum(AttemptStatus, name="attempt_status"), default=AttemptStatus.RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    create_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    poll_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    timeout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    structured_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    reconciliation_reason: Mapped[str | None] = mapped_column(Text)
    # Phase 4 provenance: what this remediation attempt was authorised against.
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "approval_requests.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_attempts_approval_request_id",
        )
    )
    triage_result_hash: Mapped[str | None] = mapped_column(String(64))
    probe_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("probe_snapshots.id", ondelete="SET NULL")
    )
    devin_pull_requests: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    pr_url: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    branch: Mapped[str | None] = mapped_column(String(255))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    ci_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_stage: Mapped[str | None] = mapped_column(String(64))
    failure_class: Mapped[str | None] = mapped_column(String(32))
    # Official consumption API result (Phase 5). `devin_acus_consumed` above is the value the
    # session object itself reports; this is the billing figure, when the plan exposes it.
    acu_report_status: Mapped[str | None] = mapped_column(String(32))
    acu_reported: Mapped[float | None] = mapped_column(Float)
    acu_report_detail: Mapped[str | None] = mapped_column(Text)
    acu_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    case: Mapped[Case] = relationship(back_populates="attempts")
    probe_snapshot: Mapped["ProbeSnapshot | None"] = relationship(foreign_keys=[probe_snapshot_id])
    probe_executions: Mapped[list["ProbeExecution"]] = relationship(
        back_populates="attempt", order_by="ProbeExecution.started_at"
    )
    pull_request_evidence: Mapped[list["PullRequestEvidence"]] = relationship(
        back_populates="attempt", order_by="PullRequestEvidence.created_at"
    )
    ci_snapshots: Mapped[list["CiSnapshot"]] = relationship(
        back_populates="attempt", order_by="CiSnapshot.observed_at"
    )


class ProbeSnapshot(Base):
    """Immutable copy of the approved probe taken at dispatch.

    Execution always uses ``script_content`` from this row, never a file that may have
    changed on disk or a copy carried by the remediation branch.
    """

    __tablename__ = "probe_snapshots"
    __table_args__ = (Index("ix_probe_snapshots_case_id", "case_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    repository: Mapped[str] = mapped_column(String(255))
    issue_number: Mapped[int] = mapped_column(Integer)
    probe_identifier: Mapped[str] = mapped_column(String(255))
    base_sha: Mapped[str] = mapped_column(String(64))
    manifest_path: Mapped[str] = mapped_column(Text)
    script_path: Mapped[str] = mapped_column(Text)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    script_hash: Mapped[str] = mapped_column(String(64))
    script_content: Mapped[str] = mapped_column(Text)
    registry_commit: Mapped[str | None] = mapped_column(String(64))
    expected_base_exit_code: Mapped[int] = mapped_column(Integer)
    expected_head_exit_code: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    runtime: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(UTC)
    )
    executions: Mapped[list["ProbeExecution"]] = relationship(
        back_populates="snapshot", order_by="ProbeExecution.started_at"
    )


class ProbeExecution(Base):
    __tablename__ = "probe_executions"
    __table_args__ = (Index("ix_probe_executions_attempt_id", "attempt_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    # NULL until a Devin attempt is authorised: the BASE run happens before any attempt
    # exists and is the evidence that permits the durable create intent.
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE")
    )
    probe_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("probe_snapshots.id", ondelete="CASCADE")
    )
    target: Mapped[ProbeTarget] = mapped_column(Enum(ProbeTarget, name="probe_target"))
    commit_sha: Mapped[str] = mapped_column(String(64))
    script_hash: Mapped[str] = mapped_column(String(64))
    runner_mode: Mapped[str] = mapped_column(String(32))
    command_identity: Mapped[str] = mapped_column(Text)
    expected_exit_code: Mapped[int] = mapped_column(Integer)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    verdict: Mapped[ProbeVerdict] = mapped_column(Enum(ProbeVerdict, name="probe_verdict"))
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    output_truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    error: Mapped[str | None] = mapped_column(Text)
    # Phase 5 evidence: verifier request id (idempotency key), stage of an infrastructure
    # failure and the tool versions the verifier reported.
    request_id: Mapped[str | None] = mapped_column(String(32))
    failure_stage: Mapped[str | None] = mapped_column(String(32))
    tool_versions: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[Attempt | None] = relationship(back_populates="probe_executions")
    snapshot: Mapped["ProbeSnapshot"] = relationship(back_populates="executions")


class CapacityLeaseKind(str, enum.Enum):
    TRIAGE = "TRIAGE"
    REMEDIATION = "REMEDIATION"
    PROBE = "PROBE"
    RESOURCE = "RESOURCE"


ACU_REPORT_AVAILABLE = "available"
ACU_REPORT_UNAVAILABLE = "unavailable"
ACU_REPORT_SIMULATED = "simulated"
ACU_REPORT_NOT_ATTEMPTED = "not_attempted"


class CapacityLease(Base):
    """One unit of a database-backed semaphore (Phase 5).

    A lease counts against `kind`'s limit (and against `scope`'s per-repository / resource
    limit) while `released_at IS NULL AND expires_at > now()`. Acquisition happens under
    `pg_advisory_xact_lock` so two workers can never both observe free capacity; expiry
    lets a lease held by a crashed worker recover without operator action.
    """

    __tablename__ = "capacity_leases"
    __table_args__ = (
        Index(
            "uq_capacity_leases_one_active_per_case_kind",
            "case_id",
            "kind",
            "scope",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        Index("ix_capacity_leases_kind_active", "kind", "expires_at", "released_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[CapacityLeaseKind] = mapped_column(
        Enum(CapacityLeaseKind, name="capacity_lease_kind")
    )
    # Repository for TRIAGE/REMEDIATION, "" for PROBE, the resource key for RESOURCE.
    scope: Mapped[str] = mapped_column(String(255), default="")
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attempts.id", ondelete="SET NULL")
    )
    owner: Mapped[str] = mapped_column(String(255))
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(Text)


class PullRequestEvidence(Base):
    """What GitHub said about the discovered PR when it was validated."""

    __tablename__ = "pull_request_evidence"
    __table_args__ = (Index("ix_pull_request_evidence_attempt_id", "attempt_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"))
    repository: Mapped[str] = mapped_column(String(255))
    pr_number: Mapped[int] = mapped_column(Integer)
    pr_url: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32))
    draft: Mapped[bool] = mapped_column(Boolean)
    merged: Mapped[bool] = mapped_column(Boolean)
    base_ref: Mapped[str] = mapped_column(String(255))
    base_sha: Mapped[str] = mapped_column(String(64))
    head_ref: Mapped[str] = mapped_column(String(255))
    head_sha: Mapped[str] = mapped_column(String(64))
    head_repository: Mapped[str | None] = mapped_column(String(255))
    author_login: Mapped[str | None] = mapped_column(String(255))
    author_type: Mapped[str | None] = mapped_column(String(64))
    compare_status: Mapped[str | None] = mapped_column(String(32))
    ahead_by: Mapped[int | None] = mapped_column(Integer)
    behind_by: Mapped[int | None] = mapped_column(Integer)
    changed_files: Mapped[list[str]] = mapped_column(JSONB, default=list)
    closing_reference_source: Mapped[str | None] = mapped_column(String(32))
    closing_issue_numbers: Mapped[list[int]] = mapped_column(JSONB, default=list)
    checks: Mapped[list[str]] = mapped_column(JSONB, default=list)
    valid: Mapped[bool] = mapped_column(Boolean, default=False)
    verdict: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(UTC)
    )
    attempt: Mapped[Attempt] = relationship(back_populates="pull_request_evidence")


class CiSnapshot(Base):
    __tablename__ = "ci_snapshots"
    __table_args__ = (Index("ix_ci_snapshots_attempt_id", "attempt_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"))
    head_sha: Mapped[str] = mapped_column(String(64))
    overall: Mapped[str] = mapped_column(String(32))
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    required_checks: Mapped[list[str]] = mapped_column(JSONB, default=list)
    missing_required: Mapped[list[str]] = mapped_column(JSONB, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(UTC)
    )
    attempt: Mapped[Attempt] = relationship(back_populates="ci_snapshots")


class StateTransition(Base):
    __tablename__ = "state_transitions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=False)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    from_state: Mapped[CaseState | None] = mapped_column(
        Enum(CaseState, name="case_state", create_type=False)
    )
    to_state: Mapped[CaseState] = mapped_column(
        Enum(CaseState, name="case_state", create_type=False)
    )
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    case: Mapped[Case] = relationship(back_populates="transitions")


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (Index("ix_notification_outbox_pending", "status", "next_attempt_at"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    channel: Mapped[OutboxChannel] = mapped_column(
        Enum(
            OutboxChannel,
            name="outbox_channel",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        )
    )
    kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status"), default=OutboxStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approval_requests.id", ondelete="CASCADE")
    )
    attempts_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalRequest(Base):
    """One Slack approval round for a schema-valid triage result (any recommendation)."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_approval_requests_attempt"),
        Index("ix_approval_requests_case_id", "case_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"))
    triage_schema_version: Mapped[str] = mapped_column(String(50))
    triage_result_hash: Mapped[str] = mapped_column(String(64))
    action_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notification_status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name="notification_status"), default=NotificationStatus.PENDING
    )
    slack_channel: Mapped[str | None] = mapped_column(String(64))
    slack_message_ts: Mapped[str | None] = mapped_column(String(64))
    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(ApprovalDecision, name="approval_decision"), default=ApprovalDecision.PENDING
    )
    decided_by_slack_user_id: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_action_id: Mapped[str | None] = mapped_column(String(255))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    label_operation: Mapped[str | None] = mapped_column(String(255))
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status"), default=DeliveryStatus.NOT_REQUESTED
    )
    label_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    case: Mapped[Case] = relationship(back_populates="approval_requests")
    attempt: Mapped[Attempt] = relationship(foreign_keys=[attempt_id])
    actions: Mapped[list["SlackAction"]] = relationship(
        back_populates="request", order_by="SlackAction.created_at"
    )
    events: Mapped[list["ApprovalEvent"]] = relationship(
        back_populates="request", order_by="ApprovalEvent.seq"
    )

    @property
    def is_open(self) -> bool:
        return self.decision == ApprovalDecision.PENDING


class SlackAction(Base):
    """Every verified Slack interaction, keyed for idempotent replay handling."""

    __tablename__ = "slack_actions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("approval_requests.id", ondelete="CASCADE")
    )
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    slack_user_id: Mapped[str] = mapped_column(String(64))
    action_id: Mapped[str] = mapped_column(String(64))
    action_ts: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    request: Mapped[ApprovalRequest] = relationship(back_populates="actions")


class ApprovalEvent(Base):
    """Append-only approval timeline (never updated or deleted by application code)."""

    __tablename__ = "approval_events"
    __table_args__ = (Index("ix_approval_events_case_id", "case_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=False)
    approval_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("approval_requests.id", ondelete="CASCADE")
    )
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(128))
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    request: Mapped[ApprovalRequest] = relationship(back_populates="events")


class SlackFakeMessage(Base):
    """Message store for the fake Slack adapter; shared between api and worker processes."""

    __tablename__ = "slack_fake_messages"
    __table_args__ = (UniqueConstraint("channel", "ts", name="uq_slack_fake_channel_ts"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel: Mapped[str] = mapped_column(String(64))
    ts: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    message_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ephemeral: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    update_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
