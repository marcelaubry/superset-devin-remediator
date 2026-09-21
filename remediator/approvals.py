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
    OUTBOX_KIND_SLACK_EPHEMERAL_RESPONSE,
    OUTBOX_KIND_SLACK_STATUS_UPDATE,
    ApprovalDecision,
    ApprovalEvent,
    ApprovalRequest,
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    DeliveryStatus,
    EventStatus,
    NotificationOutbox,
    NotificationStatus,
    OutboxChannel,
    OutboxStatus,
    SlackAction,
    WebhookEvent,
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


def retired_token_hash(token_hash: str) -> str:
    """Hash stored once a request is decided or expired: the live token no longer resolves to
    a decidable request, but a repeat click can still be answered with the current state."""
    return hashlib.sha256(f"retired:{token_hash}".encode()).hexdigest()


def retire_token(request: ApprovalRequest) -> None:
    """Call exactly once, on the PENDING → terminal transition."""
    if request.action_token_hash is not None:
        request.action_token_hash = retired_token_hash(request.action_token_hash)


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
    """Newest approval round for the case. With ``for_update`` the case row is locked first
    and both rows are taken ``FOR NO KEY UPDATE``: every writer (case processor, label
    webhook, outbox dispatcher) shares the order case -> approval request -> outbox row, and
    NO KEY UPDATE lets outbox inserts take their foreign-key KEY SHARE locks meanwhile."""
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.case_id == case_id)
        .order_by(ApprovalRequest.created_at.desc())
        .limit(1)
    )
    if for_update:
        await session.execute(
            select(Case.id).where(Case.id == case_id).with_for_update(key_share=True)
        )
        stmt = stmt.with_for_update(key_share=True)
    request: ApprovalRequest | None = await session.scalar(stmt)
    return request


async def latest_triage_attempt_id(session: AsyncSession, case_id: Any) -> Any | None:
    """The triage attempt whose result is current for the case (newest by start time).

    Cancelled attempts never produced a result: a blocked attempt superseded during
    reconciliation must not shadow the older attempt whose output was ingested."""
    return await session.scalar(
        select(Attempt.id)
        .where(
            Attempt.case_id == case_id,
            Attempt.kind == AttemptKind.TRIAGE,
            Attempt.status != AttemptStatus.CANCELLED,
        )
        .order_by(Attempt.started_at.desc(), Attempt.id.desc())
        .limit(1)
    )


async def is_current_request(session: AsyncSession, request: ApprovalRequest) -> bool:
    """True when `request` is the newest approval round for its case and is bound to the
    case's newest triage attempt. Anything else is a stale round left over from a retry or
    re-triage and must never approve or label."""
    current = await open_request_for_case(session, request.case_id)
    if current is None or current.id != request.id:
        return False
    return await latest_triage_attempt_id(session, request.case_id) == request.attempt_id


async def create_approval_request(
    session: AsyncSession,
    case: Case,
    attempt: Attempt,
    result: TriageResult,
    settings: Settings,
) -> ApprovalRequest:
    """Create the approval round for a schema-valid triage result and enqueue Slack.

    Every outcome gets a round; the human decides after seeing Devin's evidence. The
    recommendation is stored verbatim in the attempt output and rendered with a warning when
    it is not `remediation_candidate`.

    Idempotent per triage attempt: a second call for the same attempt returns the existing
    request without enqueueing another notification. Every earlier undecided round for the
    same case is superseded in this transaction: its token stops resolving to an approvable
    request and its Slack message is updated to drop the buttons.
    """
    existing: ApprovalRequest | None = await session.scalar(
        select(ApprovalRequest).where(ApprovalRequest.attempt_id == attempt.id)
    )
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    stale_rounds = await session.scalars(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.case_id == case.id,
            ApprovalRequest.decision == ApprovalDecision.PENDING,
        )
        .with_for_update()
    )
    for stale in stale_rounds:
        stale.decision = ApprovalDecision.SUPERSEDED
        stale.decided_at = now
        stale.action_token_hash = None
        record_event(
            session,
            stale,
            "superseded",
            "worker",
            f"superseded by new triage attempt {attempt.operation_key}",
        )
        _enqueue_status_update(session, stale)
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
        f"triage {attempt.operation_key} validated with recommendation {result.outcome} "
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
    STALE_TOKEN = "stale_token"
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
    response_url: str | None = None


@dataclass(frozen=True)
class SlackActionResult:
    outcome: ActionOutcome
    request: ApprovalRequest | None
    case_state: CaseState | None
    detail: str

    @property
    def http_status(self) -> int:
        """Slack treats any non-2xx as a failed interaction and shows a generic warning, so
        every verified request is acknowledged with 200; the user-facing explanation goes out
        through `response_url` (queued on the outbox) and the outcome is in the JSON body."""
        return 200 if self.outcome != ActionOutcome.UNKNOWN_ACTION else 400

    @property
    def user_message(self) -> str | None:
        """Ephemeral text for the clicking user when their click did not count."""
        return {
            ActionOutcome.UNAUTHORIZED: "You are not an authorized approver for this request.",
            ActionOutcome.EXPIRED_TOKEN: "This approval request has expired.",
            ActionOutcome.STALE_TOKEN: (
                "This approval request is stale: the issue was re-triaged. "
                "Use the newest message for this issue."
            ),
            ActionOutcome.ALREADY_DECIDED: f"No change: {self.detail}.",
            ActionOutcome.INCOMPATIBLE_STATE: f"No change: {self.detail}.",
        }.get(self.outcome)


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
        .where(ApprovalRequest.action_token_hash.in_([token_hash, retired_token_hash(token_hash)]))
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

    def _reply(outcome: ActionOutcome, detail: str) -> SlackActionResult:
        result = SlackActionResult(outcome, request, state, detail)
        _enqueue_ephemeral_response(session, request, action, result)
        return result

    if action.slack_user_id not in settings.approver_user_ids:
        _record(ActionOutcome.UNAUTHORIZED)
        record_event(session, request, "unauthorized_action", actor, f"{action.action_id} refused")
        return _reply(ActionOutcome.UNAUTHORIZED, "user is not an authorized approver")
    now = datetime.now(UTC)
    if request.decision == ApprovalDecision.PENDING and request.token_expires_at <= now:
        request.decision = ApprovalDecision.EXPIRED
        request.decided_at = now
        retire_token(request)
        record_event(session, request, "expired", "system", "action token expired before decision")
        _enqueue_status_update(session, request)
    if request.decision != ApprovalDecision.PENDING:
        _record(
            ActionOutcome.EXPIRED_TOKEN
            if request.decision == ApprovalDecision.EXPIRED
            else ActionOutcome.ALREADY_DECIDED
        )
        if request.decision == ApprovalDecision.EXPIRED:
            return _reply(ActionOutcome.EXPIRED_TOKEN, "action token has expired")
        return _reply(
            ActionOutcome.ALREADY_DECIDED, f"request already {request.decision.value.lower()}"
        )
    if not await is_current_request(session, request):
        _record(ActionOutcome.STALE_TOKEN)
        record_event(
            session,
            request,
            "stale_action",
            actor,
            f"{action.action_id} refused: request is not the case's current triage round",
        )
        return _reply(ActionOutcome.STALE_TOKEN, "approval request is stale (issue re-triaged)")
    if state != CaseState.AWAITING_REMEDIATION_APPROVAL:
        _record(ActionOutcome.INCOMPATIBLE_STATE)
        record_event(
            session,
            request,
            "incompatible_state",
            actor,
            f"{action.action_id} ignored while case is {state.value}",
        )
        return _reply(ActionOutcome.INCOMPATIBLE_STATE, f"case is in {state.value}")
    reason = (action.reason or "").strip()[:MAX_REASON_LENGTH] or None
    if action.action_id == ACTION_APPROVE:
        request.decision = ApprovalDecision.APPROVED
        request.decided_by_slack_user_id = action.slack_user_id
        request.decided_at = now
        request.decision_action_id = f"{action.action_id}:{action.action_ts}"
        request.decision_reason = reason
        request.label_operation = f"add_label:{settings.github_remediation_label}"
        request.delivery_status = DeliveryStatus.PENDING
        retire_token(request)
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
                "attempt_id": str(request.attempt_id),
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
    retire_token(request)
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


def _enqueue_ephemeral_response(
    session: AsyncSession,
    request: ApprovalRequest,
    action: SlackActionInput,
    result: SlackActionResult,
) -> None:
    text = result.user_message
    if text is None or not action.response_url:
        return
    enqueue(
        session,
        request,
        OutboxChannel.SLACK,
        OUTBOX_KIND_SLACK_EPHEMERAL_RESPONSE,
        {
            "approval_request_id": str(request.id),
            "response_url": action.response_url,
            "text": text,
            "outcome": result.outcome.value,
        },
    )


async def confirm_label_webhook(
    session: AsyncSession,
    case: Case,
    label: str,
    delivery_id: str,
    *,
    issue_labels: tuple[str, ...] | None = None,
) -> bool:
    """Handle a signed GitHub `labeled` webhook for the remediation label.

    The case advances to REMEDIATION_APPROVED only when the case's current approval round
    is APPROVED *and* the worker has already applied the label through the outbox
    (`label_applied_at` set). A label applied by anyone else, or a webhook arriving before
    our own delivery, is recorded and ignored: the outbox row stays authoritative, so the
    audit comment is still posted and the label call is never skipped.
    """
    request = await open_request_for_case(session, case.id, for_update=True)
    state = CaseState(case.state)
    if request is None or request.decision != ApprovalDecision.APPROVED:
        return False
    if not await is_current_request(session, request):
        record_event(
            session,
            request,
            "label_webhook_unexpected",
            "github",
            f"delivery {delivery_id}: approval round is not the case's current triage round",
        )
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
    if request.label_applied_at is None or request.delivery_status != DeliveryStatus.LABEL_APPLIED:
        record_event(
            session,
            request,
            "label_webhook_unexpected",
            "github",
            f"delivery {delivery_id} reported `{label}` before the remediator applied it "
            f"(delivery status {request.delivery_status.value}); state unchanged",
        )
        return False
    if issue_labels is not None and label not in issue_labels:
        record_event(
            session,
            request,
            "label_webhook_unexpected",
            "github",
            f"delivery {delivery_id}: issue label snapshot does not contain `{label}`",
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


def issue_label_names(issue: Any) -> tuple[str, ...] | None:
    """Label names from a webhook's issue snapshot; None when the payload has none."""
    if not isinstance(issue, dict) or not isinstance(issue.get("labels"), list):
        return None
    return tuple(
        str(label.get("name", ""))
        for label in issue["labels"]
        if isinstance(label, dict) and label.get("name")
    )


def is_remediation_label_event(event: WebhookEvent, label: str) -> bool:
    if event.event_type != "issues" or event.action != "labeled":
        return False
    payload_label = event.payload.get("label", {})
    name = str(payload_label.get("name", "")) if isinstance(payload_label, dict) else ""
    return name.lower() == label.lower()


async def replay_label_webhooks_after_delivery(
    session: AsyncSession, case: Case, request: ApprovalRequest, label: str
) -> bool:
    """Confirm `labeled` deliveries that GitHub sent while our label write was in flight.

    GitHub fires the webhook as soon as the label POST lands, which can be before the worker
    commits `label_applied_at`; such a delivery is processed as `label_webhook_unexpected`
    and GitHub never resends it. Called right after `label_applied_at` is committed, this
    re-runs the confirmation for deliveries of this case that were processed after the label
    was requested, so the case cannot wedge in AWAITING_REMEDIATION_APPROVAL. Deliveries
    processed before our intent existed (someone else labelled the issue) are not replayed.
    """
    if request.label_requested_at is None or request.label_applied_at is None:
        return False
    events = await session.scalars(
        select(WebhookEvent)
        .where(
            WebhookEvent.repository == case.repository,
            WebhookEvent.event_type == "issues",
            WebhookEvent.action == "labeled",
            WebhookEvent.status == EventStatus.PROCESSED,
            WebhookEvent.processed_at >= request.label_requested_at,
            WebhookEvent.payload["issue"]["number"].as_integer() == case.issue_number,
        )
        .order_by(WebhookEvent.received_at)
    )
    for event in events:
        if not is_remediation_label_event(event, label):
            continue
        if await confirm_label_webhook(
            session,
            case,
            label,
            event.delivery_id,
            issue_labels=issue_label_names(event.payload.get("issue")),
        ):
            event.case_id = case.id
            event.last_error = None
            record_event(
                session,
                request,
                "label_webhook_replayed",
                "worker",
                f"delivery {event.delivery_id} had arrived before our label commit; confirmed",
            )
            return True
    return False


async def expire_request(session: AsyncSession, request: ApprovalRequest, actor: str) -> bool:
    if request.decision != ApprovalDecision.PENDING:
        return False
    now = datetime.now(UTC)
    request.token_expires_at = now
    request.decision = ApprovalDecision.EXPIRED
    request.decided_at = now
    retire_token(request)
    record_event(session, request, "expired", actor, "action token expired by operator")
    _enqueue_status_update(session, request)
    return True
