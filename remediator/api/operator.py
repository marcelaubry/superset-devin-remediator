import hmac
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..approvals import approve_remediation, reject_remediation
from ..config import get_settings
from ..db import get_session
from ..lifecycle import CaseState, InvalidTransition, transition
from ..models import Case
from .auth import require_operator
from .dashboard import _humanize, load_case, templates

router = APIRouter()


@router.post("/login")
async def login(request: Request) -> RedirectResponse:
    form = await request.form()
    if not hmac.compare_digest(str(form.get("token", "")), get_settings().operator_token):
        raise HTTPException(status_code=401, detail="invalid token")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("operator_token", get_settings().operator_token, httponly=True)
    return response


@router.get("/api/cases/{repository:path}/{issue_number}")
async def case_json(
    repository: str,
    issue_number: int,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    case = await session.scalar(
        select(Case).where(Case.repository == repository, Case.issue_number == issue_number)
    )
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return {
        "id": str(case.id),
        "state": case.state,
        "issue_number": case.issue_number,
        "repository": case.repository,
        "pr_url": case.pr_url,
        "ci_status": case.ci_status,
    }


async def _case_action(
    case_id: UUID, to_state: CaseState, request: Request, session: AsyncSession
) -> Any:
    case = await session.get(Case, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    try:
        await transition(
            session, case, to_state, f"operator requested {to_state.lower()}", "operator"
        )
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
                {"request": request, "case": refreshed, "humanize": _humanize, "error": str(exc)},
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
            {"request": request, "case": refreshed, "humanize": _humanize, "error": None},
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
                {"request": request, "case": refreshed, "humanize": _humanize, "error": str(exc)},
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
            {"request": request, "case": refreshed, "humanize": _humanize, "error": None},
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
