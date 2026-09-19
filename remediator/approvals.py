"""Phase 3 approval domain logic.

Everything here is pure database work executed inside the caller's transaction. Network
side effects (Slack posts/updates, GitHub labels/comments) are only ever enqueued on the
transactional outbox and performed later by the worker's outbox dispatcher. Nothing in this
module can create a Devin session.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .devin.triage import TriageResult
from .lifecycle import CaseState, InvalidTransition, transition
from .models import (
    OUTBOX_KIND_GITHUB_APPLY_LABEL,
    OUTBOX_KIND_GITHUB_REJECTION_COMMENT,
    OUTBOX_KIND_SLACK_APPROVAL_REQUEST,
    OUTBOX_KIND_SLACK_STATUS_UPDATE,
    ApprovalDecision,
    ApprovalEvent,
    ApprovalRequest,
    Attempt,
    Case,
    DeliveryStatus,
    NotificationOutbox,
    NotificationStatus,
    OutboxChannel,
    OutboxStatus,
    SlackAction,
)
from .slack.blocks import ACTION_APPROVE, ACTION_REJECT

SLACK_ACTOR_PREFIX = "slack:"
MAX_REASON_LENGTH = 1000


def triage_result_hash(raw: dict[str, Any]) -> str:
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def hash_action_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_action_token() -> str:
    return secrets.token_urlsafe(32)


def record_event(
    session: AsyncSession, request: ApprovalRequest, kind: str, actor: str, detail: str
) -> ApprovalEvent:
    event = ApprovalEvent(
        approval_request_id=request.id,
        case_id=request.case_id,
        kind=kind,
        actor=actor,
        detail=detail,
    )
    session.add(event)
    return event


def enqueue(
    session: AsyncSession,
    request: ApprovalRequest,
    channel: OutboxChannel,
    kind: str,
    payload: dict[str, Any],
) -> NotificationOutbox:
    row = NotificationOutbox(
        case_id=request.case_id,
        approval_request_id=request.id,
        channel=channel,
        kind=kind,
        payload=payload,
        status=OutboxStatus.PENDING,
    )
    session.add(row)
    return row


async def open_request_for_case(
    session: AsyncSession, case_id: Any, *, for_update: bool = False
) -> ApprovalRequest | None:
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.case_id == case_id)
        .order_by(ApprovalRequest.created_at.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    request: ApprovalRequest | None = await session.scalar(stmt)
    return request


async def create_approval_request(
    session: AsyncSession,
    case: Case,
    attempt: Attempt,
    result: TriageResult,
    settings: Settings,
) -> ApprovalRequest:
    """Create the approval round for a validated `remediation_candidate` and enqueue Slack.

    Idempotent per triage attempt: a second call for the same attempt returns the existing
    request without enqueueing another notification.
    """
    existing: ApprovalRequest | None = await session.scalar(
        select(ApprovalRequest).where(ApprovalRequest.attempt_id == attempt.id)
    )
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    request = ApprovalRequest(
        case_id=case.id,
        attempt_id=attempt.id,
        triage_schema_version=str(result.raw.get("schema_version", "")),
        triage_result_hash=triage_result_hash(result.raw),
        action_token_hash=None,
        token_expires_at=now + timedelta(seconds=settings.slack_action_token_ttl_seconds),
        notification_status=NotificationStatus.PENDING,
        decision=ApprovalDecision.PENDING,
        delivery_status=DeliveryStatus.NOT_REQUESTED,
        created_at=now,
    )
    session.add(request)
    await session.flush()
    record_event(
        session,
        request,
        "approval_requested",
        "worker",
        f"triage {attempt.operation_key} validated as remediation_candidate "
        f"(result sha256 {request.triage_result_hash[:12]})",
    )
    enqueue(
        session,
        request,
        OutboxChannel.SLACK,
        OUTBOX_KIND_SLACK_APPROVAL_REQUEST,
        {"approval_request_id": str(request.id), "issue_number": case.issue_number},
    )
    return request


class ActionOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    ALREADY_DECIDED = "already_decided"
    UNKNOWN_TOKEN = "unknown_token"
    EXPIRED_TOKEN = "expired_token"
    UNAUTHORIZED = "unauthorized"
    INCOMPATIBLE_STATE = "incompatible_state"
    UNKNOWN_ACTION = "unknown_action"


TERMINAL_OUTCOMES = frozenset({ActionOutcome.APPROVED, ActionOutcome.REJECTED})


@dataclass(frozen=True)
class SlackActionInput:
    token: str
    slack_user_id: str
    action_id: str
    action_ts: str
    reason: str | None = None


@dataclass(frozen=True)
class SlackActionResult:
    outcome: ActionOutcome
    request: ApprovalRequest | None
    case_state: CaseState | None
    detail: str

    @property
    def http_status(self) -> int:
        return {
            ActionOutcome.APPROVED: 200,
            ActionOutcome.REJECTED: 200,
            ActionOutcome.DUPLICATE: 200,
            ActionOutcome.ALREADY_DECIDED: 200,
            ActionOutcome.UNKNOWN_TOKEN: 404,
            ActionOutcome.EXPIRED_TOKEN: 410,
            ActionOutcome.UNAUTHORIZED: 403,
            ActionOutcome.INCOMPATIBLE_STATE: 409,
            ActionOutcome.UNKNOWN_ACTION: 400,
        }[self.outcome]


def _dedupe_key(token_hash: str, action: SlackActionInput) -> str:
    return f"{token_hash[:24]}:{action.action_id}:{action.action_ts}:{action.slack_user_id}"


async def process_slack_action(
    session: AsyncSession, settings: Settings, action: SlackActionInput
) -> SlackActionResult:
    """Record a verified Slack button click. The caller commits.

    Only the opaque token identifies the approval request; repository, issue and case ids
    from the Slack payload are never consulted.
    """
    if action.action_id not in {ACTION_APPROVE, ACTION_REJECT}:
        return SlackActionResult(ActionOutcome.UNKNOWN_ACTION, None, None, "unknown action")
    token_hash = hash_action_token(action.token)
    request: ApprovalRequest | None = await session.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.action_token_hash == token_hash)
        .with_for_update()
    )
    if request is None:
        return SlackActionResult(ActionOutcome.UNKNOWN_TOKEN, None, None, "unknown action token")
    case: Case | None = await session.scalar(
        select(Case).where(Case.id == request.case_id).with_for_update()
    )
    if case is None:
        return SlackActionResult(ActionOutcome.UNKNOWN_TOKEN, None, None, "unknown action token")
    state = CaseState(case.state)
    dedupe_key = _dedupe_key(token_hash, action)
    prior: SlackAction | None = await session.scalar(
        select(SlackAction).where(SlackAction.dedupe_key == dedupe_key)
    )
    if prior is not None:
        return SlackActionResult(
            ActionOutcome.DUPLICATE,
            request,
            state,
            f"duplicate of action already recorded as {prior.outcome}",
        )
    actor = f"{SLACK_ACTOR_PREFIX}{action.slack_user_id}"

    def _record(outcome: ActionOutcome) -> None:
        session.add(
            SlackAction(
                approval_request_id=request.id,
                dedupe_key=dedupe_key,
                slack_user_id=action.slack_user_id,
                action_id=action.action_id,
                action_ts=action.action_ts,
                outcome=outcome.value,
            )
        )

    if action.slack_user_id not in settings.approver_user_ids:
        _record(ActionOutcome.UNAUTHORIZED)
        record_event(session, request, "unauthorized_action", actor, f"{action.action_id} refused")
        return SlackActionResult(
            ActionOutcome.UNAUTHORIZED, request, state, "user is not an authorized approver"
        )
    now = datetime.now(UTC)
    if request.decision == ApprovalDecision.PENDING and request.token_expires_at <= now:
        request.decision = ApprovalDecision.EXPIRED
        request.decided_at = now
        record_event(session, request, "expired", "system", "action token expired before decision")
        _enqueue_status_update(session, request)
    if request.decision != ApprovalDecision.PENDING:
        _record(
            ActionOutcome.EXPIRED_TOKEN
            if request.decision == ApprovalDecision.EXPIRED
            else ActionOutcome.ALREADY_DECIDED
        )
        if request.decision == ApprovalDecision.EXPIRED:
            return SlackActionResult(
                ActionOutcome.EXPIRED_TOKEN, request, state, "action token has expired"
            )
        return SlackActionResult(
            ActionOutcome.ALREADY_DECIDED,
            request,
            state,
            f"request already {request.decision.value.lower()}",
        )
    if state != CaseState.AWAITING_REMEDIATION_APPROVAL:
        _record(ActionOutcome.INCOMPATIBLE_STATE)
        record_event(
            session,
            request,
            "incompatible_state",
            actor,
            f"{action.action_id} ignored while case is {state.value}",
        )
        return SlackActionResult(
            ActionOutcome.INCOMPATIBLE_STATE, request, state, f"case is in {state.value}"
        )
    reason = (action.reason or "").strip()[:MAX_REASON_LENGTH] or None
    if action.action_id == ACTION_APPROVE:
        request.decision = ApprovalDecision.APPROVED
        request.decided_by_slack_user_id = action.slack_user_id
        request.decided_at = now
        request.decision_action_id = f"{action.action_id}:{action.action_ts}"
        request.decision_reason = reason
        request.label_operation = f"add_label:{settings.github_remediation_label}"
        request.delivery_status = DeliveryStatus.PENDING
        _record(ActionOutcome.APPROVED)
        record_event(
            session,
            request,
            "approved",
            actor,
            f"approved triage result {request.triage_result_hash[:12]}; "
            f"queued {request.label_operation}",
        )
        enqueue(
            session,
            request,
            OutboxChannel.GITHUB,
            OUTBOX_KIND_GITHUB_APPLY_LABEL,
            {
                "approval_request_id": str(request.id),
                "triage_result_hash": request.triage_result_hash,
                "label": settings.github_remediation_label,
            },
        )
        _enqueue_status_update(session, request)
        return SlackActionResult(
            ActionOutcome.APPROVED, request, state, "approval recorded; GitHub dispatch queued"
        )
    request.decision = ApprovalDecision.REJECTED
    request.decided_by_slack_user_id = action.slack_user_id
    request.decided_at = now
    request.decision_action_id = f"{action.action_id}:{action.action_ts}"
    request.decision_reason = reason
    request.delivery_status = DeliveryStatus.NOT_REQUESTED
    _record(ActionOutcome.REJECTED)
    record_event(
        session,
        request,
        "rejected",
        actor,
        "rejected" + (f": {reason}" if reason else ""),
    )
    await transition(
        session,
        case,
        CaseState.REMEDIATION_REJECTED,
        "remediation rejected via Slack approval",
        "slack",
    )
    enqueue(
        session,
        request,
        OutboxChannel.GITHUB,
        OUTBOX_KIND_GITHUB_REJECTION_COMMENT,
        {"approval_request_id": str(request.id)},
    )
    _enqueue_status_update(session, request)
    return SlackActionResult(
        ActionOutcome.REJECTED, request, CaseState.REMEDIATION_REJECTED, "rejection recorded"
    )


def _enqueue_status_update(session: AsyncSession, request: ApprovalRequest) -> None:
    if request.slack_message_ts is None:
        return
    enqueue(
        session,
        request,
        OutboxChannel.SLACK,
        OUTBOX_KIND_SLACK_STATUS_UPDATE,
        {"approval_request_id": str(request.id)},
    )


def enqueue_slack_status_update(session: AsyncSession, request: ApprovalRequest) -> None:
    _enqueue_status_update(session, request)


async def confirm_label_webhook(
    session: AsyncSession, case: Case, label: str, delivery_id: str
) -> bool:
    """Handle a signed GitHub `labeled` webhook for the remediation label.

    The case advances to REMEDIATION_APPROVED only when a recorded APPROVED decision
    exists; a label applied without one never advances state.
    """
    request = await open_request_for_case(session, case.id, for_update=True)
    state = CaseState(case.state)
    if request is None or request.decision != ApprovalDecision.APPROVED:
        return False
    if state not in {CaseState.AWAITING_REMEDIATION_APPROVAL, CaseState.APPROVAL_DELIVERY_FAILED}:
        if state == CaseState.REMEDIATION_APPROVED:
            record_event(
                session,
                request,
                "label_webhook_duplicate",
                "github",
                f"delivery {delivery_id} repeated `{label}` confirmation",
            )
        return False
    now = datetime.now(UTC)
    request.delivery_status = DeliveryStatus.CONFIRMED
    request.label_confirmed_at = now
    record_event(
        session,
        request,
        "label_confirmed",
        "github",
        f"signed webhook delivery {delivery_id} confirmed `{label}` on the issue",
    )
    try:
        await transition(
            session,
            case,
            CaseState.REMEDIATION_APPROVED,
            f"GitHub confirmed `{label}` (delivery {delivery_id})",
            "github",
        )
    except InvalidTransition:
        return False
    _enqueue_status_update(session, request)
    return True


async def expire_request(session: AsyncSession, request: ApprovalRequest, actor: str) -> bool:
    if request.decision != ApprovalDecision.PENDING:
        return False
    now = datetime.now(UTC)
    request.token_expires_at = now
    request.decision = ApprovalDecision.EXPIRED
    request.decided_at = now
    record_event(session, request, "expired", actor, "action token expired by operator")
    _enqueue_status_update(session, request)
    return True
