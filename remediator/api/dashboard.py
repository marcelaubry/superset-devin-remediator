from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..lifecycle import TERMINAL_STATES, CaseState
from ..models import (
    ApprovalRequest,
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    EventStatus,
    StateTransition,
    WebhookEvent,
)
from .auth import require_operator

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "templates"))


def _humanize(value: datetime | None) -> str:
    if not value:
        return "-"
    seconds = max(0, int((datetime.now(UTC) - value).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _until(value: datetime | None) -> str:
    if not value:
        return "-"
    seconds = int((value - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return "expired"
    return f"in {_humanize(datetime.now(UTC) - timedelta(seconds=seconds))}"


def _attempt_elapsed(attempt: Attempt) -> str:
    start = attempt.create_sent_at or attempt.started_at
    if attempt.finished_at and start:
        return f"{int((attempt.finished_at - start).total_seconds())}s"
    return _humanize(start)


def latest_attempt(case: Case, kind: AttemptKind | None = None) -> Attempt | None:
    attempts = [a for a in case.attempts if kind is None or a.kind == kind]
    if not attempts:
        return None
    floor = datetime.min.replace(tzinfo=UTC)
    return max(attempts, key=lambda attempt: attempt.started_at or floor)


def latest_triage_result(case: Case) -> dict[str, Any] | None:
    attempt = latest_attempt(case, AttemptKind.TRIAGE)
    if attempt is None or attempt.status != AttemptStatus.SUCCEEDED:
        return None
    return attempt.structured_output


def latest_approval(case: Case) -> ApprovalRequest | None:
    if not case.approval_requests:
        return None
    return max(case.approval_requests, key=lambda request: request.created_at)


def approval_json(case: Case) -> dict[str, Any] | None:
    approval = latest_approval(case)
    if approval is None:
        return None
    return {
        "id": str(approval.id),
        "attempt_id": str(approval.attempt_id),
        "triage_schema_version": approval.triage_schema_version,
        "triage_result_hash": approval.triage_result_hash,
        "token_expires_at": approval.token_expires_at.isoformat(),
        "notification_status": approval.notification_status.value,
        "slack_channel": approval.slack_channel,
        "slack_message_ts": approval.slack_message_ts,
        "decision": approval.decision.value,
        "decided_by_slack_user_id": approval.decided_by_slack_user_id,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "decision_action_id": approval.decision_action_id,
        "decision_reason": approval.decision_reason,
        "label_operation": approval.label_operation,
        "delivery_status": approval.delivery_status.value,
        "label_applied_at": (
            approval.label_applied_at.isoformat() if approval.label_applied_at else None
        ),
        "label_confirmed_at": (
            approval.label_confirmed_at.isoformat() if approval.label_confirmed_at else None
        ),
        "github_comment_id": approval.github_comment_id,
        "actions": [
            {
                "slack_user_id": action.slack_user_id,
                "action_id": action.action_id,
                "action_ts": action.action_ts,
                "outcome": action.outcome,
                "created_at": action.created_at.isoformat(),
            }
            for action in approval.actions
        ],
        "events": [
            {
                "seq": event.seq,
                "kind": event.kind,
                "actor": event.actor,
                "detail": event.detail,
                "created_at": event.created_at.isoformat(),
            }
            for event in approval.events
        ],
    }


TEMPLATE_HELPERS: dict[str, object] = {
    "latest_approval": latest_approval,
    "humanize": _humanize,
    "until": _until,
    "attempt_elapsed": _attempt_elapsed,
    "latest_attempt": latest_attempt,
    "latest_triage_result": latest_triage_result,
}


async def load_case(session: AsyncSession, case_id: UUID) -> Case | None:
    return cast(
        Case | None,
        await session.scalar(
            select(Case)
            .options(
                selectinload(Case.attempts),
                selectinload(Case.transitions),
                selectinload(Case.outbox),
                selectinload(Case.approval_requests).selectinload(ApprovalRequest.events),
                selectinload(Case.approval_requests).selectinload(ApprovalRequest.actions),
            )
            .where(Case.id == case_id)
        ),
    )


async def _context(session: AsyncSession) -> dict[str, object]:
    cases = list(
        (
            await session.scalars(
                select(Case)
                .options(selectinload(Case.attempts))
                .order_by(Case.created_at.desc())
                .limit(100)
            )
        ).all()
    )
    events = list(
        (
            await session.scalars(
                select(WebhookEvent).order_by(WebhookEvent.received_at.desc()).limit(100)
            )
        ).all()
    )
    transitions = list(
        (
            await session.scalars(
                select(StateTransition).order_by(StateTransition.seq.desc()).limit(50)
            )
        ).all()
    )
    state_counts = {state.value: 0 for state in CaseState}
    for state, count in (
        await session.execute(select(Case.state, func.count()).group_by(Case.state))
    ).all():
        state_counts[CaseState(state).value] = count
    recommendation_counts = {
        str(recommendation): count
        for recommendation, count in (
            await session.execute(
                select(Case.recommendation, func.count())
                .where(Case.recommendation.is_not(None))
                .group_by(Case.recommendation)
            )
        ).all()
    }
    active = [
        case
        for case in cases
        if case.state not in TERMINAL_STATES and case.state != CaseState.HUMAN_BLOCKED
    ]
    completed = [case for case in cases if case.state in TERMINAL_STATES]
    processing_events = [event for event in events if event.status == EventStatus.PROCESSING]
    failures: list[object] = [
        case
        for case in cases
        if case.state in {CaseState.FAILED, CaseState.HUMAN_BLOCKED, CaseState.TIMED_OUT}
    ]
    failures.extend(event for event in events if event.status == EventStatus.FAILED)
    now = datetime.now(UTC)
    received_1h = await session.scalar(
        select(func.count()).select_from(Case).where(Case.created_at >= now - timedelta(hours=1))
    )
    received_24h = await session.scalar(
        select(func.count()).select_from(Case).where(Case.created_at >= now - timedelta(hours=24))
    )
    accepted_1h = await session.scalar(
        select(func.count())
        .select_from(WebhookEvent)
        .where(WebhookEvent.received_at >= now - timedelta(hours=1))
    )
    accepted_24h = await session.scalar(
        select(func.count())
        .select_from(WebhookEvent)
        .where(WebhookEvent.received_at >= now - timedelta(hours=24))
    )
    bucket_rows = (
        await session.execute(
            select(
                Case.state,
                func.count(),
                func.avg(func.extract("epoch", Case.completed_at - Case.created_at)),
            )
            .where(
                Case.state.in_(
                    {
                        CaseState.CI_PASSED,
                        CaseState.POLICY_REJECTED,
                        CaseState.FAILED,
                        CaseState.TIMED_OUT,
                        CaseState.CANCELLED,
                    }
                ),
                Case.completed_at.is_not(None),
            )
            .group_by(Case.state)
        )
    ).all()
    buckets = {
        "succeeded": {"count": 0, "mean_time": "-"},
        "rejected": {"count": 0, "mean_time": "-"},
        "failed": {"count": 0, "mean_time": "-"},
    }
    for state, count, mean in bucket_rows:
        bucket = (
            "succeeded"
            if state == CaseState.CI_PASSED
            else ("rejected" if state == CaseState.POLICY_REJECTED else "failed")
        )
        buckets[bucket] = {
            "count": count,
            "mean_time": f"{float(mean):.1f}s" if mean is not None else "-",
        }
    return {
        "cases": cases,
        "events": events,
        "transitions": transitions,
        "state_counts": state_counts,
        "recommendation_counts": recommendation_counts,
        "active": active,
        "processing_events": processing_events,
        "completed": completed,
        "failures": failures,
        "throughput": {
            "received_1h": received_1h,
            "received_24h": received_24h,
            "accepted_1h": accepted_1h,
            "accepted_24h": accepted_24h,
            "buckets": buckets,
        },
        **TEMPLATE_HELPERS,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", {"request": request, **await _context(session)}
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"request": request})


@router.get("/cases/{case_id}", response_class=HTMLResponse)
async def case_detail(
    case_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    case = await load_case(session, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return templates.TemplateResponse(
        request, "case.html", {"request": request, "case": case, **TEMPLATE_HELPERS}
    )


@router.get("/partials/case/{case_id}", response_class=HTMLResponse)
async def case_detail_partial(
    case_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    case = await load_case(session, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return templates.TemplateResponse(
        request,
        "partials/case_detail.html",
        {"request": request, "case": case, **TEMPLATE_HELPERS, "error": None},
    )


@router.get("/partials/{name}", response_class=HTMLResponse)
async def partial(
    name: str,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    context = await _context(session)
    if name not in {
        "active",
        "completed",
        "failures",
        "timeline",
        "overview",
        "throughput",
        "cases",
    }:
        raise HTTPException(status_code=404, detail="partial not found")
    return templates.TemplateResponse(
        request, f"partials/{name}.html", {"request": request, **context}
    )
