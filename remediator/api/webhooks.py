import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import get_session
from ..github.signature import verify_signature
from ..models import EventStatus, WebhookEvent

router = APIRouter()


def _labels(payload: dict[str, Any]) -> set[str]:
    return {
        str(item.get("name", "")).lower() for item in payload.get("issue", {}).get("labels", [])
    }


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_github_delivery: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    body = await request.body()
    if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        return JSONResponse({"detail": "invalid signature"}, status_code=401)
    if not x_github_delivery:
        return JSONResponse({"detail": "missing delivery id"}, status_code=400)
    if not x_github_event:
        return JSONResponse({"detail": "missing event type"}, status_code=400)
    try:
        payload: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"detail": "invalid JSON"}, status_code=400)
    action = str(payload.get("action", ""))
    repository = payload.get("repository", {}).get("full_name")
    if repository != settings.github_repository:
        return JSONResponse(
            {"accepted": False, "reason": "repository not allowed"}, status_code=202
        )
    if x_github_event not in settings.allowed_events:
        return JSONResponse({"accepted": False, "reason": "event not allowed"}, status_code=202)
    if action not in settings.allowed_actions:
        return JSONResponse({"accepted": False, "reason": "action not allowed"}, status_code=202)
    required = settings.github_required_label.lower()
    if (
        required
        and required not in _labels(payload)
        and not (
            action == "labeled"
            and str(payload.get("label", {}).get("name", "")).lower() == required
        )
    ):
        return JSONResponse(
            {"accepted": False, "reason": "required label missing"}, status_code=202
        )
    session.add(
        WebhookEvent(
            delivery_id=x_github_delivery,
            event_type=x_github_event,
            action=action,
            repository=repository,
            payload=payload,
            status=EventStatus.PENDING,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return JSONResponse({"accepted": True, "deduplicated": True}, status_code=202)
    return JSONResponse(
        {"accepted": True, "delivery_id": x_github_delivery, "deduplicated": False}, status_code=202
    )
