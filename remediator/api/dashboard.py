from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
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


async def _context(session: AsyncSession) -> dict[str, object]:
    cases = list((await session.scalars(select(Case).order_by(Case.created_at.desc()))).all())
    events = list(
        (
            await session.scalars(select(WebhookEvent).order_by(WebhookEvent.received_at.desc()))
        ).all()
    )
    transitions = list(
        (
            await session.scalars(
                select(StateTransition).order_by(StateTransition.created_at.desc()).limit(50)
            )
        ).all()
    )
    state_counts = {state.value: sum(case.state == state for case in cases) for state in CaseState}
    recommendation_counts: dict[str, int] = {}
    for case in cases:
        if case.recommendation:
            recommendation_counts[case.recommendation.value] = (
                recommendation_counts.get(case.recommendation.value, 0) + 1
            )
    active = [case for case in cases if case.state not in TERMINAL_STATES]
    completed = [case for case in cases if case.state in TERMINAL_STATES]
    processing_events = [event for event in events if event.status == EventStatus.PROCESSING]
    failures: list[object] = [
        case
        for case in cases
        if case.state in {CaseState.FAILED, CaseState.HUMAN_BLOCKED, CaseState.TIMED_OUT}
    ]
    failures.extend(event for event in events if event.status == EventStatus.FAILED)
    now = datetime.now(UTC)
    received_1h = sum(
        1
        for case in cases
        if case.created_at is not None and case.created_at >= now - timedelta(hours=1)
    )
    received_24h = sum(
        1
        for case in cases
        if case.created_at is not None and case.created_at >= now - timedelta(hours=24)
    )
    completed_cases = [case for case in cases if case.completed_at is not None]
    durations = [
        (case.completed_at - case.created_at).total_seconds()
        for case in completed_cases
        if case.completed_at and case.created_at
    ]
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
            "completed_total": len(completed_cases),
            "ci_passed": sum(1 for case in cases if case.state == CaseState.CI_PASSED),
            "policy_rejected": sum(1 for case in cases if case.state == CaseState.POLICY_REJECTED),
            "mean_time_to_complete": (
                f"{sum(durations) / len(durations):.1f}s" if durations else "-"
            ),
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
    case = await session.scalar(
        select(Case)
        .options(selectinload(Case.attempts), selectinload(Case.transitions))
        .where(Case.id == case_id)
    )
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return templates.TemplateResponse(
        request, "case.html", {"request": request, "case": case, "humanize": _humanize}
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
    }:
        raise HTTPException(status_code=404, detail="partial not found")
    return templates.TemplateResponse(
        request, f"partials/{name}.html", {"request": request, **context}
    )
