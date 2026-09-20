import hmac
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..approvals import expire_request, record_event
from ..config import Settings, get_settings
from ..db import get_session
from ..lifecycle import (
    REMEDIATION_PHASE,
    REMEDIATION_RETRYABLE_STATES,
    TERMINAL_STATES,
    CaseState,
    InvalidTransition,
    phase_for_state,
    transition,
)
from ..models import (
    ACTIVE_ATTEMPT_STATUSES,
    FAILURE_CLASS_INFRASTRUCTURE,
    OUTBOX_KIND_GITHUB_APPLY_LABEL,
    OUTBOX_KIND_SLACK_APPROVAL_REQUEST,
    UNRESOLVED_CREATE_ACK,
    ApprovalRequest,
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    CreateState,
    DeliveryStatus,
    NotificationOutbox,
    NotificationStatus,
    OutboxStatus,
    SlackFakeMessage,
)
from ..worker.remediation import enqueue_remediation_update, latest_remediation_attempt
from .auth import COOKIE_NAME, _cookie_value, require_operator
from .dashboard import (
    ATTEMPT_EVIDENCE_OPTIONS,
    TEMPLATE_HELPERS,
    approval_json,
    load_case,
    page_context,
    remediation_json,
    templates,
)

router = APIRouter()


@router.post("/login")
async def login(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    form = await request.form()
    if not hmac.compare_digest(str(form.get("token", "")), settings.operator_token):
        if "text/html" in request.headers.get("accept", ""):
            return templates.TemplateResponse(
                request,
                "login.html",
                {**page_context(request, settings), "error": "Invalid token"},
                status_code=401,
            )
        raise HTTPException(status_code=401, detail="invalid token")
    response = RedirectResponse("/", status_code=303)
    exp = str(int(time.time()) + 43200)
    response.set_cookie(
        COOKIE_NAME,
        _cookie_value(settings.operator_token, exp),
        max_age=43200,
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
    )
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/api/cases/{repository:path}/{issue_number}")
async def case_json(
    repository: str,
    issue_number: int,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    case = await session.scalar(
        select(Case)
        .options(
            *ATTEMPT_EVIDENCE_OPTIONS,
            selectinload(Case.outbox),
            selectinload(Case.approval_requests).selectinload(ApprovalRequest.events),
            selectinload(Case.approval_requests).selectinload(ApprovalRequest.actions),
        )
        .where(Case.repository == repository, Case.issue_number == issue_number)
    )
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return {
        "id": str(case.id),
        "state": case.state,
        "issue_number": case.issue_number,
        "repository": case.repository,
        "failure_reason": case.failure_reason,
        "devin_session_url": case.devin_session_url,
        "pr_url": case.pr_url,
        "ci_status": case.ci_status,
        "waiting_for": case.waiting_for,
        "advisory_recommendation": case.recommendation.value if case.recommendation else None,
        "eligibility": _eligibility_json(case),
        "attempts": [_attempt_json(attempt) for attempt in case.attempts],
        "approval": approval_json(case),
        "remediation": remediation_json(case),
        "outbox": [_outbox_json(row) for row in case.outbox],
    }


def _eligibility_json(case: Case) -> dict[str, Any] | None:
    if not case.rubric:
        return None
    checks = [dict(check) for check in case.rubric]
    missing = [str(check.get("reason", "")) for check in checks if not check.get("passed")]
    return {"eligible": not missing, "missing": missing, "checks": checks}


def _outbox_json(row: NotificationOutbox) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "channel": row.channel.value,
        "kind": row.kind,
        "status": row.status.value,
        "attempts_count": row.attempts_count,
        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else None,
        "last_error": row.last_error,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
    }


def _attempt_json(attempt: Attempt) -> dict[str, Any]:
    return {
        "id": str(attempt.id),
        "kind": attempt.kind,
        "status": attempt.status,
        "operation_key": attempt.operation_key,
        "create_state": attempt.create_state,
        "devin_session_id": attempt.devin_session_id,
        "devin_session_url": attempt.devin_session_url,
        "devin_status": attempt.devin_status,
        "devin_status_detail": attempt.devin_status_detail,
        "devin_tags": list(attempt.devin_tags or []),
        "devin_acus_consumed": attempt.devin_acus_consumed,
        "base_sha": attempt.base_sha,
        "prompt_version": attempt.prompt_version,
        "max_acu_limit": attempt.max_acu_limit,
        "poll_count": attempt.poll_count,
        "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
        "last_polled_at": attempt.last_polled_at.isoformat() if attempt.last_polled_at else None,
        "timeout_at": attempt.timeout_at.isoformat() if attempt.timeout_at else None,
        "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
        "structured_output": attempt.structured_output,
        "reconciliation_reason": attempt.reconciliation_reason,
        "error": attempt.error,
        "approval_request_id": (
            str(attempt.approval_request_id) if attempt.approval_request_id else None
        ),
        "triage_result_hash": attempt.triage_result_hash,
        "probe_snapshot_id": str(attempt.probe_snapshot_id) if attempt.probe_snapshot_id else None,
        "devin_pull_requests": attempt.devin_pull_requests,
        "pr_url": attempt.pr_url,
        "pr_number": attempt.pr_number,
        "branch": attempt.branch,
        "head_sha": attempt.head_sha,
        "ci_deadline_at": attempt.ci_deadline_at.isoformat() if attempt.ci_deadline_at else None,
        "failure_stage": attempt.failure_stage,
        "failure_class": attempt.failure_class,
    }


async def _may_own_session(session: AsyncSession, case: Case) -> bool:
    """True when an attempt of this case sent a create and has not been closed out."""
    owner = await session.scalar(
        select(Attempt.id).where(
            Attempt.case_id == case.id,
            Attempt.status.in_([*ACTIVE_ATTEMPT_STATUSES, AttemptStatus.BLOCKED]),
            Attempt.create_sent_at.is_not(None),
        )
    )
    return owner is not None


async def _acknowledge_unresolved(session: AsyncSession, case: Case, request: Request) -> None:
    """Retry after an unresolved create needs an explicit operator confirmation.

    The first POST may have created a session we could not find by tag; a silent
    retry would issue a second paid create. The operator must assert that no live
    session carries the operation key (``confirm_no_session=true``).
    """
    unresolved = list(
        (
            await session.scalars(
                select(Attempt).where(
                    Attempt.case_id == case.id,
                    Attempt.create_state == CreateState.UNRESOLVED,
                    Attempt.status.in_([AttemptStatus.BLOCKED, AttemptStatus.RECONCILING]),
                )
            )
        ).all()
    )
    pending = [a for a in unresolved if a.reconciliation_reason != UNRESOLVED_CREATE_ACK]
    if not pending:
        return
    confirmed = request.query_params.get("confirm_no_session")
    if confirmed is None and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        form = await request.form()
        confirmed = str(form.get("confirm_no_session", "")) or None
    if str(confirmed or "").lower() not in {"true", "1", "yes", "on"}:
        keys = ", ".join(a.operation_key for a in pending)
        raise InvalidTransition(
            f"create outcome unresolved for {keys}; confirm in the Devin console that no "
            "session carries this tag, then retry with confirm_no_session=true"
        )
    for attempt in pending:
        attempt.reconciliation_reason = UNRESOLVED_CREATE_ACK


async def _case_action(
    case_id: UUID, to_state: CaseState, request: Request, session: AsyncSession
) -> Any:
    case = await session.get(Case, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    try:
        current = CaseState(case.state)
        remediation_phase = phase_for_state(current) is REMEDIATION_PHASE
        if to_state == CaseState.CANCELLED and (
            current in {CaseState.TRIAGING, CaseState.REMEDIATING}
            or await _may_own_session(session, case)
        ):
            to_state = (
                CaseState.REMEDIATION_TERMINATION_PENDING
                if remediation_phase
                else CaseState.TERMINATION_PENDING
            )
            reason = "operator requested cancel; terminating Devin session"
        elif (
            to_state == CaseState.CANCELLED and remediation_phase and current not in TERMINAL_STATES
        ):
            to_state = CaseState.REMEDIATION_CANCELLED
            reason = "operator requested cancel"
        else:
            reason = f"operator requested {to_state.lower()}"
        if to_state == CaseState.RECEIVED and remediation_phase:
            # A remediation retry never re-runs triage and never re-enters the create
            # intent directly: it goes back through the zero-ACU dispatch preconditions,
            # which mint a new attempt and operation key.
            if current not in REMEDIATION_RETRYABLE_STATES:
                raise InvalidTransition(f"cannot retry remediation from {current}")
            await _acknowledge_unresolved(session, case, request)
            await _refuse_if_active_attempt(session, case)
            count = await session.scalar(
                select(func.count())
                .select_from(Attempt)
                .where(Attempt.case_id == case.id, Attempt.kind == AttemptKind.REMEDIATION)
            )
            if int(count or 0) >= get_settings().max_attempts_per_kind:
                raise InvalidTransition("attempt cap reached")
            to_state = CaseState.REMEDIATION_APPROVED
            reason = "operator requested remediation retry (new attempt)"
        elif to_state == CaseState.RECEIVED:
            await _acknowledge_unresolved(session, case, request)
            counts = await session.execute(
                select(Attempt.kind, func.count())
                .where(Attempt.case_id == case.id)
                .group_by(Attempt.kind)
            )
            if any(count >= get_settings().max_attempts_per_kind for _, count in counts.all()):
                raise InvalidTransition("attempt cap reached")
        await transition(session, case, to_state, reason, "operator")
        if remediation_phase:
            await _notify_remediation(session, case)
        await session.commit()
    except InvalidTransition as exc:
        await session.rollback()
        if request.headers.get("HX-Request") == "true":
            refreshed = await load_case(session, case_id)
            if not refreshed:
                raise HTTPException(status_code=404, detail="case not found") from exc
            return _render_case(request, refreshed, str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if request.headers.get("HX-Request") == "true":
        refreshed = await load_case(session, case_id)
        if not refreshed:
            raise HTTPException(status_code=404, detail="case not found")
        return _render_case(request, refreshed)
    refreshed = await load_case(session, case_id)
    if not refreshed:
        raise HTTPException(status_code=404, detail="case not found")
    return {"id": str(refreshed.id), "state": refreshed.state}


async def _refuse_if_active_attempt(session: AsyncSession, case: Case) -> None:
    active = await session.scalar(
        select(Attempt.operation_key).where(
            Attempt.case_id == case.id,
            Attempt.kind == AttemptKind.REMEDIATION,
            Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
        )
    )
    if active is not None:
        raise InvalidTransition(f"remediation attempt {active} is still active")


async def _notify_remediation(session: AsyncSession, case: Case) -> None:
    request = await session.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.case_id == case.id)
        .order_by(ApprovalRequest.created_at.desc())
        .limit(1)
    )
    if request is not None:
        enqueue_remediation_update(session, request)


async def _verified_attempt_action(
    case_id: UUID,
    request: Request,
    session: AsyncSession,
    *,
    to_state: CaseState,
    allowed_from: frozenset[CaseState],
    stages: frozenset[str],
    infrastructure_only: bool,
    reason: str,
) -> Any:
    """Re-run a verification step on the *existing* verified attempt (no new Devin session).

    The attempt must have failed in one of `stages`; with `infrastructure_only` the failure
    must additionally be classified as infrastructure, so a probe that genuinely failed at
    head or a red CI can never be "retried" into a pass.
    """
    case = await session.get(Case, case_id, with_for_update=True)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    try:
        current = CaseState(case.state)
        if current not in allowed_from:
            raise InvalidTransition(f"{reason} is not available from {current}")
        attempt = await latest_remediation_attempt(session, case.id)
        if attempt is None or attempt.head_sha is None or attempt.pr_url is None:
            raise InvalidTransition("no verified remediation attempt to re-check")
        if attempt.status in ACTIVE_ATTEMPT_STATUSES:
            raise InvalidTransition(f"attempt {attempt.operation_key} still owns a session")
        if attempt.failure_stage not in stages:
            raise InvalidTransition(
                f"attempt failed at stage {attempt.failure_stage!r}, not one of {sorted(stages)}"
            )
        if infrastructure_only and attempt.failure_class != FAILURE_CLASS_INFRASTRUCTURE:
            raise InvalidTransition(
                f"attempt failure is classified {attempt.failure_class!r}; only "
                "infrastructure failures may be re-run"
            )
        attempt.failure_stage = None
        attempt.failure_class = None
        attempt.error = None
        if to_state == CaseState.CI_PENDING:
            attempt.ci_deadline_at = None
            case.ci_status = None
        case.failure_reason = None
        await transition(session, case, to_state, f"operator requested {reason}", "operator")
        await _notify_remediation(session, case)
        await session.commit()
    except InvalidTransition as exc:
        await session.rollback()
        refreshed = await load_case(session, case_id)
        if not refreshed:
            raise HTTPException(status_code=404, detail="case not found") from exc
        return _render_case(request, refreshed, str(exc))
    refreshed = await load_case(session, case_id)
    if not refreshed:
        raise HTTPException(status_code=404, detail="case not found")
    return _render_case(request, refreshed)


@router.post("/operator/cases/{case_id}/retry-ci")
async def retry_ci(
    case_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Re-synchronise GitHub checks for the verified head SHA (e.g. after a manual re-run
    on GitHub, or after a CI timeout / GitHub outage). Never re-runs Devin."""
    return await _verified_attempt_action(
        case_id,
        request,
        session,
        to_state=CaseState.CI_PENDING,
        allowed_from=frozenset({CaseState.CI_FAILED, CaseState.REMEDIATION_FAILED}),
        stages=frozenset({"ci"}),
        infrastructure_only=False,
        reason="CI synchronisation retry",
    )


@router.post("/operator/cases/{case_id}/retry-probe")
async def retry_probe(
    case_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Re-run probe verification only when the *runner* failed (missing tools, clone or
    checkout errors). A probe that ran and produced the wrong exit code is a verification
    verdict and cannot be retried.

    Before a session exists (PROBE_INFRASTRUCTURE_BLOCKED) this re-runs BASE on the
    persisted snapshot at zero ACUs; after a PR exists it re-runs only HEAD on the
    already-verified attempt. Neither path creates a Devin session."""
    case = await session.get(Case, case_id, with_for_update=True)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    if CaseState(case.state) == CaseState.PROBE_INFRASTRUCTURE_BLOCKED:
        try:
            await _refuse_if_active_attempt(session, case)
            case.failure_reason = None
            await transition(
                session,
                case,
                CaseState.PROBE_VALIDATING_BASE,
                "operator requested base probe retry after infrastructure failure",
                "operator",
            )
            await _notify_remediation(session, case)
            await session.commit()
        except InvalidTransition as exc:
            await session.rollback()
            refreshed = await load_case(session, case_id)
            if not refreshed:
                raise HTTPException(status_code=404, detail="case not found") from exc
            return _render_case(request, refreshed, str(exc))
        refreshed = await load_case(session, case_id)
        if not refreshed:
            raise HTTPException(status_code=404, detail="case not found")
        return _render_case(request, refreshed)
    await session.rollback()
    return await _verified_attempt_action(
        case_id,
        request,
        session,
        to_state=CaseState.PROBE_VALIDATING_HEAD,
        allowed_from=frozenset({CaseState.REMEDIATION_FAILED}),
        stages=frozenset({"probe_head"}),
        infrastructure_only=True,
        reason="head probe verification retry",
    )


@router.post("/operator/cases/{case_id}/retry")
async def retry_case(
    case_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> Any:
    return await _case_action(case_id, CaseState.RECEIVED, request, session)


@router.post("/operator/cases/{case_id}/cancel")
async def cancel_case(
    case_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> Any:
    return await _case_action(case_id, CaseState.CANCELLED, request, session)


def _render_case(request: Request, case: Case, error: str | None = None) -> Any:
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "partials/case_detail.html",
            {
                "request": request,
                "rendered_at": datetime.now(UTC),
                "case": case,
                **TEMPLATE_HELPERS,
                "error": error,
            },
        )
    if error is not None:
        raise HTTPException(status_code=409, detail=error)
    return {"id": str(case.id), "state": case.state}


@router.post("/operator/outbox/{outbox_id}/retry")
async def retry_outbox(
    outbox_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Re-queue a terminally failed outbox row without asking anyone to approve again."""
    row = await session.get(NotificationOutbox, outbox_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="outbox row not found")
    error: str | None = None
    if row.status != OutboxStatus.FAILED:
        error = f"outbox row is {row.status.value}, only FAILED rows can be retried"
        if request.headers.get("HX-Request") != "true":
            raise HTTPException(status_code=409, detail=error)
    else:
        row.status = OutboxStatus.PENDING
        row.attempts_count = 0
        row.next_attempt_at = datetime.now(UTC)
        row.claimed_by = None
        row.lease_expires_at = None
        if row.approval_request_id is not None:
            approval = await session.get(ApprovalRequest, row.approval_request_id)
            if approval is not None:
                if (
                    row.kind == OUTBOX_KIND_GITHUB_APPLY_LABEL
                    and approval.delivery_status == DeliveryStatus.FAILED
                ):
                    approval.delivery_status = DeliveryStatus.PENDING
                if (
                    row.kind == OUTBOX_KIND_SLACK_APPROVAL_REQUEST
                    and approval.notification_status == NotificationStatus.FAILED
                ):
                    approval.notification_status = NotificationStatus.PENDING
                record_event(
                    session,
                    approval,
                    "operator_retry",
                    "operator",
                    f"outbox {row.id} ({row.channel.value}/{row.kind}) re-queued",
                )
        await session.commit()
    case = await load_case(session, row.case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return _render_case(request, case, error)


@router.post("/operator/approvals/{approval_id}/expire")
async def expire_approval(
    approval_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Invalidate a pending Slack action token. The case stays AWAITING_REMEDIATION_APPROVAL
    so an operator can cancel or re-trigger triage; the Slack buttons stop working."""
    approval = await session.get(ApprovalRequest, approval_id, with_for_update=True)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval request not found")
    error: str | None = None
    if not await expire_request(session, approval, "operator"):
        error = f"approval request already {approval.decision.value.lower()}"
    else:
        await session.commit()
    case = await load_case(session, approval.case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return _render_case(request, case, error)


@router.get("/api/slack/fake/messages")
async def fake_slack_messages(
    _: str = Depends(require_operator),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
    channel: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fake-adapter channel contents (operator only). Not served in live Slack mode."""
    if settings.slack_live:
        raise HTTPException(status_code=404, detail="fake Slack adapter is not active")
    stmt = select(SlackFakeMessage).order_by(SlackFakeMessage.created_at.desc())
    if channel:
        stmt = stmt.where(SlackFakeMessage.channel == channel)
    rows = (await session.scalars(stmt.limit(max(1, min(limit, 200))))).all()
    return [
        {
            "channel": row.channel,
            "ts": row.ts,
            "text": row.text,
            "blocks": row.blocks,
            "update_count": row.update_count,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
        for row in rows
    ]
