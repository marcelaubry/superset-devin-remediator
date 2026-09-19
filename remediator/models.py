import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
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
    SENT = "SENT"
    FAILED = "FAILED"


class ApprovalDecision(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


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


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    delivery_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
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
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    kind: Mapped[AttemptKind] = mapped_column(Enum(AttemptKind, name="attempt_kind"))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
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
    case: Mapped[Case] = relationship(back_populates="attempts")


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
    """One Slack approval round for a validated `remediation_candidate` triage result."""

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
    label_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    case: Mapped[Case] = relationship(back_populates="approval_requests")
    attempt: Mapped[Attempt] = relationship()
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
    update_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
