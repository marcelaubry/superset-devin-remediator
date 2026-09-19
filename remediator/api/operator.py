import hmac
import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..approvals import approve_remediation, reject_remediation
from ..config import Settings, get_settings
from ..db import get_session
from ..lifecycle import CaseState, InvalidTransition, transition
from ..models import (
    ACTIVE_ATTEMPT_STATUSES,
    UNRESOLVED_CREATE_ACK,
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    CreateState,
)
from .auth import COOKIE_NAME, _cookie_value, require_operator
from .dashboard import TEMPLATE_HELPERS, load_case, templates

router = APIRouter()


@router.post("/login")
async def login(request: Request, settings: Settings = Depends(get_settings)) -> RedirectResponse:
    form = await request.form()
    if not hmac.compare_digest(str(form.get("token", "")), settings.operator_token):
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
        .options(selectinload(Case.attempts))
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
        "attempts": [_attempt_json(attempt) for attempt in case.attempts],
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
        if to_state == CaseState.CANCELLED and (
            CaseState(case.state) in {CaseState.TRIAGING, CaseState.REMEDIATING}
            or await _may_own_session(session, case)
        ):
            to_state = CaseState.TERMINATION_PENDING
            reason = "operator requested cancel; terminating Devin session"
        else:
            reason = f"operator requested {to_state.lower()}"
        if to_state == CaseState.RECEIVED:
            await _acknowledge_unresolved(session, case, request)
            counts = await session.execute(
                select(Attempt.kind, func.count())
                .where(Attempt.case_id == case.id)
                .group_by(Attempt.kind)
            )
            if any(count >= get_settings().max_attempts_per_kind for _, count in counts.all()):
                raise InvalidTransition("attempt cap reached")
            latest = await session.scalar(
                select(Attempt)
                .where(Attempt.case_id == case.id)
                .order_by(Attempt.started_at.desc())
                .limit(1)
            )
            triage_succeeded = await session.scalar(
                select(Attempt.id).where(
                    Attempt.case_id == case.id,
                    Attempt.kind == AttemptKind.TRIAGE,
                    Attempt.status == AttemptStatus.SUCCEEDED,
                )
            )
            if (
                latest is not None
                and latest.kind == AttemptKind.REMEDIATION
                and triage_succeeded is not None
            ):
                to_state = CaseState.REMEDIATION_CREATE_INTENT
                reason = "retry remediation"
        await transition(session, case, to_state, reason, "operator")
        await session.commit()
    except InvalidTransition as exc:
        await session.rollback()
        if request.headers.get("HX-Request") == "true":
            refreshed = await load_case(session, case_id)
            if not refreshed:
                raise HTTPException(status_code=404, detail="case not found") from exc
            return templates.TemplateResponse(
                request,
                "partials/case_detail.html",
                {"request": request, "case": refreshed, **TEMPLATE_HELPERS, "error": str(exc)},
                status_code=200,
            )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if request.headers.get("HX-Request") == "true":
        refreshed = await load_case(session, case_id)
        if not refreshed:
            raise HTTPException(status_code=404, detail="case not found")
        return templates.TemplateResponse(
            request,
            "partials/case_detail.html",
            {"request": request, "case": refreshed, **TEMPLATE_HELPERS, "error": None},
        )
    refreshed = await load_case(session, case_id)
    if not refreshed:
        raise HTTPException(status_code=404, detail="case not found")
    return {"id": str(refreshed.id), "state": refreshed.state}


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


async def _remediation_approval_action(
    case_id: UUID,
    request: Request,
    session: AsyncSession,
    approve: bool,
) -> Any:
    case = await session.get(Case, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    try:
        if approve:
            await approve_remediation(session, case, "operator")
        else:
            await reject_remediation(session, case, "operator")
        await session.commit()
    except InvalidTransition as exc:
        await session.rollback()
        if request.headers.get("HX-Request") == "true":
            refreshed = await load_case(session, case_id)
            if not refreshed:
                raise HTTPException(status_code=404, detail="case not found") from exc
            return templates.TemplateResponse(
                request,
                "partials/case_detail.html",
                {"request": request, "case": refreshed, **TEMPLATE_HELPERS, "error": str(exc)},
                status_code=200,
            )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if request.headers.get("HX-Request") == "true":
        refreshed = await load_case(session, case_id)
        if not refreshed:
            raise HTTPException(status_code=404, detail="case not found")
        return templates.TemplateResponse(
            request,
            "partials/case_detail.html",
            {"request": request, "case": refreshed, **TEMPLATE_HELPERS, "error": None},
        )
    refreshed = await load_case(session, case_id)
    if not refreshed:
        raise HTTPException(status_code=404, detail="case not found")
    return {"id": str(refreshed.id), "state": refreshed.state}


@router.post("/operator/cases/{case_id}/approve-remediation")
async def approve_case(
    case_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> Any:
    return await _remediation_approval_action(case_id, request, session, True)


@router.post("/operator/cases/{case_id}/reject-remediation")
async def reject_case(
    case_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> Any:
    return await _remediation_approval_action(case_id, request, session, False)
