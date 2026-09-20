"""Devin v3 REST client used in ``DEVIN_CLIENT_MODE=live``.

Endpoints (https://docs.devin.ai/api-reference/v3/overview):

- ``POST   /organizations/{org_id}/sessions``
- ``GET    /organizations/{org_id}/sessions``            (filter by tag, exact match applied here)
- ``GET    /organizations/{org_id}/sessions/{devin_id}``
- ``DELETE /organizations/{org_id}/sessions/{devin_id}``

Only idempotent reads are retried. A create that fails at the transport layer is
reported as uncertain (``DevinTransportError``) and never re-sent by this class.
"""

import asyncio
import logging
import random
from datetime import UTC, datetime
from typing import Any

import httpx

from .client import (
    ConsumptionReport,
    CreateSessionRequest,
    DevinApiError,
    DevinSessionNotFound,
    DevinTransportError,
    SessionSnapshot,
    parse_pull_requests,
)

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_LIST_PAGE_SIZE = 200
_MAX_LIST_PAGES = 5
MAX_RETRY_AFTER_SECONDS = 60.0


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Honour an integer `Retry-After` header (seconds), capped so a hostile or broken
    value can never park the worker for hours."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


class SecretRedactingFilter(logging.Filter):
    """Replaces the API key in any log record emitted while it is installed."""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self._secret = secret

    def redact(self, text: str) -> str:
        return text.replace(self._secret, "[REDACTED]") if self._secret else text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secret:
            return True
        message = record.getMessage()
        if self._secret in message:
            record.msg = self.redact(message)
            record.args = ()
        return True


def _parse_snapshot(payload: dict[str, Any]) -> SessionSnapshot:
    updated = payload.get("updated_at")
    updated_at = (
        datetime.fromtimestamp(int(updated), tz=UTC) if isinstance(updated, int | float) else None
    )
    output = payload.get("structured_output")
    acus = payload.get("acus_consumed")
    return SessionSnapshot(
        session_id=str(payload["session_id"]),
        url=str(payload.get("url", "")),
        status=str(payload.get("status", "")),
        status_detail=(
            str(payload["status_detail"]) if payload.get("status_detail") is not None else None
        ),
        tags=tuple(str(tag) for tag in payload.get("tags") or ()),
        structured_output=output if isinstance(output, dict) else None,
        acus_consumed=float(acus) if isinstance(acus, int | float) else None,
        updated_at=updated_at,
        pull_requests=parse_pull_requests(payload.get("pull_requests")),
        extra={k: v for k, v in payload.items() if k in {"title", "status_detail", "is_archived"}},
    )


def _problem_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        for key in ("detail", "title", "message", "error"):
            value = body.get(key)
            if isinstance(value, str):
                return value[:500]
        return str(body)[:500]
    return str(body)[:500]


class LiveDevinClient:
    mode = "live"

    def __init__(
        self,
        *,
        api_key: str,
        org_id: str,
        base_url: str,
        repos_format: str = "https://github.com/{repository}",
        request_timeout_seconds: float = 30.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        if not api_key or not org_id:
            raise ValueError("live Devin client requires api_key and org_id")
        self._org_id = org_id
        self._repos_format = repos_format
        self._max_retries = max(0, max_retries)
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "superset-devin-remediator/phase2",
            },
            timeout=httpx.Timeout(request_timeout_seconds),
            transport=transport,
        )
        self._redactor = SecretRedactingFilter(api_key)
        for handler in logging.getLogger().handlers:
            handler.addFilter(self._redactor)

    def __repr__(self) -> str:
        return f"LiveDevinClient(org_id={self._org_id!r})"

    def _sessions_path(self, session_id: str | None = None) -> str:
        base = f"/organizations/{self._org_id}/sessions"
        return f"{base}/{session_id}" if session_id else base

    def _api_error(self, response: httpx.Response) -> DevinApiError:
        return DevinApiError(
            response.status_code, self._redactor.redact(_problem_message(response))
        )

    def _backoff(self, attempt: int) -> float:
        cap = min(self._backoff_max, self._backoff_base * (2**attempt))
        return random.uniform(0, cap)

    async def _get_with_retries(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        server_delay: float | None = None
        for attempt in range(self._max_retries + 1):
            server_delay = None
            try:
                response = await self._http.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = DevinTransportError(f"GET {path}: {exc.__class__.__name__}")
            else:
                if response.status_code in RETRYABLE_STATUS_CODES:
                    last_error = self._api_error(response)
                    server_delay = retry_after_seconds(response)
                elif response.status_code >= 400:
                    raise self._api_error(response)
                else:
                    return response.json()
            if attempt < self._max_retries:
                delay = self._backoff(attempt) if server_delay is None else server_delay
                logger.warning(
                    "Devin GET %s failed (%s); retrying in %.2fs", path, last_error, delay
                )
                await self._sleep(delay)
        assert last_error is not None
        raise last_error

    async def create_session(self, request: CreateSessionRequest) -> SessionSnapshot:
        body: dict[str, Any] = {
            "prompt": request.prompt,
            "title": request.title
            or f"triage {request.repository}@{request.base_sha[:12]} [{request.operation_key}]",
            "tags": request.all_tags(),
            "max_acu_limit": request.max_acu_limit,
            "repos": [self._repos_format.format(repository=request.repository)],
            "structured_output_schema": request.structured_output_schema,
            "structured_output_required": True,
            "resumable": False,
        }
        path = self._sessions_path()
        try:
            response = await self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise DevinTransportError(
                f"POST {path} outcome unknown: {exc.__class__.__name__}"
            ) from exc
        if response.status_code >= 400:
            raise self._api_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DevinTransportError("POST sessions returned a non-JSON body") from exc
        if not isinstance(payload, dict) or "session_id" not in payload:
            raise DevinTransportError("POST sessions returned an unexpected body")
        return _parse_snapshot(payload)

    async def find_sessions_by_tag(self, tag: str) -> list[SessionSnapshot]:
        matches: list[SessionSnapshot] = []
        after: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            params: dict[str, Any] = {"tags": [tag], "first": _LIST_PAGE_SIZE}
            if after:
                params["after"] = after
            payload = await self._get_with_retries(self._sessions_path(), params=params)
            items = payload.get("items", []) if isinstance(payload, dict) else []
            for item in items:
                if isinstance(item, dict) and tag in (item.get("tags") or []):
                    matches.append(_parse_snapshot(item))
            if not isinstance(payload, dict) or not payload.get("has_next_page"):
                break
            after = payload.get("end_cursor")
            if not after:
                break
        return matches

    async def get_session(self, session_id: str) -> SessionSnapshot:
        try:
            payload = await self._get_with_retries(self._sessions_path(session_id))
        except DevinApiError as exc:
            if exc.status_code == 404:
                raise DevinSessionNotFound(session_id) from exc
            raise
        return _parse_snapshot(payload)

    async def terminate_session(self, session_id: str) -> SessionSnapshot | None:
        path = self._sessions_path(session_id)
        try:
            response = await self._http.delete(path)
        except httpx.HTTPError as exc:
            raise DevinTransportError(f"DELETE {path}: {exc.__class__.__name__}") from exc
        if response.status_code == 404:
            raise DevinSessionNotFound(session_id)
        if response.status_code >= 400:
            raise self._api_error(response)
        try:
            payload = response.json()
        except ValueError:
            return None
        return (
            _parse_snapshot(payload)
            if isinstance(payload, dict) and "session_id" in payload
            else None
        )

    async def session_consumption(self, session_id: str) -> ConsumptionReport:
        """GET /organizations/{org}/consumption/daily/sessions/{id} (Enterprise plans, service
        user with `ViewOrgConsumption`). Any failure is reported as `unavailable`; nothing is
        estimated and nothing propagates to the caller."""
        path = f"/organizations/{self._org_id}/consumption/daily/sessions/{session_id}"
        try:
            payload = await self._get_with_retries(path)
        except DevinApiError as exc:
            reason = {
                401: "credentials rejected",
                403: "service user lacks ViewOrgConsumption or plan does not include it",
                404: "consumption endpoint or session not found",
            }.get(exc.status_code, f"HTTP {exc.status_code}")
            return ConsumptionReport("unavailable", detail=reason)
        except DevinTransportError as exc:
            return ConsumptionReport("unavailable", detail=f"transport: {exc}")
        total = payload.get("total_acus") if isinstance(payload, dict) else None
        if isinstance(total, bool) or not isinstance(total, int | float):
            return ConsumptionReport("unavailable", detail="response carried no total_acus")
        return ConsumptionReport("available", acus=float(total))

    async def whoami(self) -> dict[str, Any]:
        """GET /self: service-user identity (readiness only, read-only)."""
        payload = await self._get_with_retries("/self")
        return payload if isinstance(payload, dict) else {}

    async def list_sessions_probe(self) -> int:
        """GET one page of sessions to prove the read permission (readiness only)."""
        payload = await self._get_with_retries(self._sessions_path(), params={"first": 1})
        items = payload.get("items", []) if isinstance(payload, dict) else []
        return len(items) if isinstance(items, list) else 0

    async def aclose(self) -> None:
        for handler in logging.getLogger().handlers:
            handler.removeFilter(self._redactor)
        await self._http.aclose()
