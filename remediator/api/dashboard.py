from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..lifecycle import TERMINAL_STATES, CaseState
from ..models import Case, EventStatus, StateTransition, WebhookEvent
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


async def load_case(session: AsyncSession, case_id: UUID) -> Case | None:
    return cast(
        Case | None,
        await session.scalar(
            select(Case)
            .options(selectinload(Case.attempts), selectinload(Case.transitions))
            .where(Case.id == case_id)
        ),
    )


async def _context(session: AsyncSession) -> dict[str, object]:
    cases = list(
        (await session.scalars(select(Case).order_by(Case.created_at.desc()).limit(100))).all()
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
        "humanize": _humanize,
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
        request, "case.html", {"request": request, "case": case, "humanize": _humanize}
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
        {"request": request, "case": case, "humanize": _humanize, "error": None},
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
