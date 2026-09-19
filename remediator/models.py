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
