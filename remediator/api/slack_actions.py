"""`POST /webhooks/slack/actions` — Slack interactivity endpoint.

Order of operations is security-relevant: the raw body is read first, the timestamp is
checked against the replay window, then the `v0:{timestamp}:{body}` HMAC is compared in
constant time. Only after all three succeed is the form body decoded and the JSON payload
parsed. The decision itself is a short database transaction (no network calls); every
side effect (GitHub label/comment, Slack message update) is queued on the outbox and
performed asynchronously by the worker.
"""

import json
import logging
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..approvals import (
    MAX_REASON_LENGTH,
    ActionOutcome,
    SlackActionInput,
    process_slack_action,
)
from ..config import Settings, get_settings
from ..db import get_session
from ..metrics import slack_action_requests_total
from ..slack.blocks import ACTION_REASON, BLOCK_ID_REASON
from ..slack.signature import verify_slack_request

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 256 * 1024


def _reject(result: str, status: int, detail: str) -> JSONResponse:
    slack_action_requests_total.labels(result=result).inc()
    return JSONResponse({"ok": False, "detail": detail}, status_code=status)


def _extract_action(payload: dict[str, Any]) -> SlackActionInput | None:
    if payload.get("type") != "block_actions":
        return None
    user = payload.get("user")
    actions = payload.get("actions")
    if not isinstance(user, dict) or not isinstance(actions, list) or not actions:
        return None
    action = actions[0]
    if not isinstance(action, dict):
        return None
    user_id = str(user.get("id", "")).strip()
    token = action.get("value")
    action_id = str(action.get("action_id", ""))
    action_ts = str(action.get("action_ts", "")).strip()
    if not user_id or not isinstance(token, str) or not token or not action_ts:
        return None
    return SlackActionInput(
        token=token,
        slack_user_id=user_id,
        action_id=action_id,
        action_ts=action_ts,
        reason=_extract_reason(payload),
        response_url=_extract_response_url(payload),
    )


def _extract_response_url(payload: dict[str, Any]) -> str | None:
    """Slack's one-shot URL for ephemeral feedback to the clicking user; only the Slack
    hooks host is accepted so the outbox never posts to an attacker-chosen URL."""
    raw = payload.get("response_url")
    if not isinstance(raw, str) or not raw.startswith("https://hooks.slack.com/"):
        return None
    return raw[:2000]


def _extract_reason(payload: dict[str, Any]) -> str | None:
    """Optional rejection reason from the message's select, echoed by Slack in state.values."""
    state = payload.get("state")
    values = state.get("values") if isinstance(state, dict) else None
    block = values.get(BLOCK_ID_REASON) if isinstance(values, dict) else None
    element = block.get(ACTION_REASON) if isinstance(block, dict) else None
    if not isinstance(element, dict):
        return None
    selected = element.get("selected_option")
    raw = selected.get("value") if isinstance(selected, dict) else element.get("value")
    if not isinstance(raw, str):
        return None
    reason = raw.strip()[:MAX_REASON_LENGTH]
    return reason or None


@router.post("/webhooks/slack/actions")
async def slack_actions(
    request: Request,
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _reject("bad_request", 413, "payload too large")
    if settings.slack_signing_secret is None:
        return _reject("not_configured", 503, "Slack signing secret is not configured")
    verification = verify_slack_request(
        settings.slack_signing_secret.get_secret_value(),
        body,
        x_slack_request_timestamp,
        x_slack_signature,
        max_skew_seconds=settings.slack_max_timestamp_skew_seconds,
    )
    if not verification.ok:
        assert verification.failure is not None
        return _reject(verification.failure.value, 401, verification.failure.value)

    content_type = request.headers.get("content-type", "")
    raw_payload: str | None = None
    if content_type.startswith("application/x-www-form-urlencoded"):
        try:
            form = parse_qs(body.decode("utf-8"), strict_parsing=True, max_num_fields=10)
        except (UnicodeDecodeError, ValueError):
            return _reject("bad_request", 400, "malformed form body")
        values = form.get("payload") or []
        raw_payload = values[0] if values else None
    elif content_type.startswith("application/json"):
        raw_payload = body.decode("utf-8", errors="replace")
    if raw_payload is None:
        return _reject("bad_request", 400, "missing payload")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return _reject("bad_request", 400, "invalid payload JSON")
    if not isinstance(payload, dict):
        return _reject("bad_request", 400, "payload must be an object")
    action = _extract_action(payload)
    if action is None:
        return _reject("bad_request", 400, "unsupported interaction payload")

    try:
        result = await process_slack_action(session, settings, action)
        await session.commit()
    except IntegrityError:
        # A concurrent duplicate click won the dedupe race; report the current state.
        await session.rollback()
        result = await process_slack_action(session, settings, action)
        await session.rollback()
        if result.outcome not in {ActionOutcome.DUPLICATE, ActionOutcome.ALREADY_DECIDED}:
            # Verified request that lost a race: still a 200 ack for Slack.
            slack_action_requests_total.labels(result="conflict").inc()
            return JSONResponse(
                {"ok": False, "outcome": "conflict", "detail": "concurrent action; retry"},
                status_code=200,
            )
    slack_action_requests_total.labels(result=result.outcome.value).inc()
    logger.info(
        "slack action %s -> %s (case state %s)",
        action.action_id,
        result.outcome.value,
        result.case_state.value if result.case_state else None,
    )
    body_out: dict[str, Any] = {
        "ok": result.outcome in {ActionOutcome.APPROVED, ActionOutcome.REJECTED},
        "outcome": result.outcome.value,
        "detail": result.detail,
    }
    if result.request is not None and result.outcome not in {
        ActionOutcome.UNAUTHORIZED,
        ActionOutcome.UNKNOWN_TOKEN,
        ActionOutcome.STALE_TOKEN,
    }:
        body_out["decision"] = result.request.decision.value
        body_out["case_state"] = result.case_state.value if result.case_state else None
    return JSONResponse(body_out, status_code=result.http_status)
