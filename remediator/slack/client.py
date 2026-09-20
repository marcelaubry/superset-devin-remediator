"""Slack chat adapters. The outbox dispatcher owns retries; adapters raise on failure."""

import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..metrics import instrument_http_client
from ..models import SlackFakeMessage

logger = logging.getLogger(__name__)

FAKE_POST_FAILURE = "fake Slack adapter configured to fail chat.postMessage"
# Fixture issue numbers whose first chat.postMessage attempts fail in the fake client; used by
# scripts/simulate.py to exercise notification failure + operator retry without restarting
# the worker. The fake recognises the issue from the "owner/repo#N" fallback text.
FAKE_POST_FAILURE_ISSUES: frozenset[int] = frozenset({4699})
_ISSUE_REF = re.compile(r"#(\d+)")


METADATA_EVENT_TYPE = "remediator_approval_request"


class SlackApiError(Exception):
    def __init__(
        self, method: str, error: str, *, retryable: bool, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(f"Slack {method} failed: {error}")
        self.method = method
        self.error = error
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class SlackMessageRef:
    channel: str
    ts: str


class SlackClient(Protocol):
    async def post_message(
        self,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SlackMessageRef: ...

    async def update_message(
        self, ref: SlackMessageRef, text: str, blocks: list[dict[str, Any]]
    ) -> SlackMessageRef: ...

    async def find_message(
        self, channel: str, approval_request_id: str, *, oldest: datetime
    ) -> SlackMessageRef | None:
        """Locate a message previously posted with `approval_request_id` metadata.

        Used to reconcile a chat.postMessage whose result was lost before commit so the
        same approval never produces two messages.
        """
        ...

    async def post_response(self, response_url: str, text: str) -> None:
        """Send an ephemeral follow-up to the user who clicked, via Slack's `response_url`."""
        ...

    async def aclose(self) -> None: ...


def approval_metadata(approval_request_id: str) -> dict[str, Any]:
    return {
        "event_type": METADATA_EVENT_TYPE,
        "event_payload": {"approval_request_id": approval_request_id},
    }


def _metadata_request_id(metadata: Any) -> str | None:
    if not isinstance(metadata, dict) or metadata.get("event_type") != METADATA_EVENT_TYPE:
        return None
    payload = metadata.get("event_payload")
    value = payload.get("approval_request_id") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


class FakeSlackClient:
    """Stores messages in PostgreSQL so api, worker and simulator observe the same channel."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        fail_posts: bool = False,
        failing_issue_attempts: int = 0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sessions = session_factory
        self.fail_posts = fail_posts
        self._failing_issue_attempts = failing_issue_attempts
        self._post_attempts: dict[int, int] = {}
        self._clock = clock

    def __repr__(self) -> str:
        return f"FakeSlackClient(fail_posts={self.fail_posts})"

    async def aclose(self) -> None:
        return None

    async def post_message(
        self,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SlackMessageRef:
        if self.fail_posts:
            raise SlackApiError("chat.postMessage", FAKE_POST_FAILURE, retryable=True)
        match = _ISSUE_REF.search(text)
        issue_number = int(match.group(1)) if match else None
        if issue_number in FAKE_POST_FAILURE_ISSUES:
            attempt = self._post_attempts.get(issue_number, 0) + 1
            self._post_attempts[issue_number] = attempt
            if attempt <= self._failing_issue_attempts:
                raise SlackApiError(
                    "chat.postMessage",
                    f"fake Slack post failure {attempt}/{self._failing_issue_attempts} "
                    f"for fixture issue #{issue_number}",
                    retryable=True,
                )
        ts = f"{self._clock():.6f}-{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC)
        async with self._sessions() as session:
            session.add(
                SlackFakeMessage(
                    channel=channel,
                    ts=ts,
                    text=text,
                    blocks=blocks,
                    message_metadata=metadata,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        return SlackMessageRef(channel=channel, ts=ts)

    async def find_message(
        self, channel: str, approval_request_id: str, *, oldest: datetime
    ) -> SlackMessageRef | None:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(SlackFakeMessage)
                .where(SlackFakeMessage.channel == channel, SlackFakeMessage.ephemeral.is_(False))
                .order_by(SlackFakeMessage.created_at)
            )
            for row in rows:
                if _metadata_request_id(row.message_metadata) == approval_request_id:
                    return SlackMessageRef(channel=row.channel, ts=row.ts)
        return None

    async def post_response(self, response_url: str, text: str) -> None:
        now = datetime.now(UTC)
        async with self._sessions() as session:
            session.add(
                SlackFakeMessage(
                    channel=response_url[:64],
                    ts=f"{self._clock():.6f}-{uuid.uuid4().hex[:8]}",
                    text=text,
                    blocks=[],
                    ephemeral=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

    async def update_message(
        self, ref: SlackMessageRef, text: str, blocks: list[dict[str, Any]]
    ) -> SlackMessageRef:
        if self.fail_posts:
            raise SlackApiError("chat.update", FAKE_POST_FAILURE, retryable=True)
        async with self._sessions() as session:
            existing = await session.scalar(
                select(SlackFakeMessage.id).where(
                    SlackFakeMessage.channel == ref.channel, SlackFakeMessage.ts == ref.ts
                )
            )
            if existing is None:
                raise SlackApiError("chat.update", "message_not_found", retryable=False)
            await session.execute(
                update(SlackFakeMessage)
                .where(SlackFakeMessage.id == existing)
                .values(
                    text=text,
                    blocks=blocks,
                    update_count=SlackFakeMessage.update_count + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
        return ref


_NON_RETRYABLE_ERRORS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "invalid_blocks",
        "invalid_blocks_format",
        "msg_too_long",
        "message_not_found",
        "cant_update_message",
        "missing_scope",
    }
)


def _retry_after(header: str | None) -> float | None:
    if header is None:
        return None
    try:
        return max(float(header), 0.0)
    except ValueError:
        return None


class LiveSlackClient:
    """Minimal Web API client for chat.postMessage / chat.update (https://api.slack.com/methods)."""

    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = "https://slack.com/api",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "superset-devin-remediator/phase3",
            },
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )
        instrument_http_client(self._http, "slack")
        # response_url posts are unauthenticated; never send the bot token there.
        self._hooks = httpx.AsyncClient(
            headers={"User-Agent": "superset-devin-remediator/phase3"},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    def __repr__(self) -> str:
        return "LiveSlackClient()"

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._hooks.aclose()

    async def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(f"/{method}", json=body)
        except httpx.HTTPError as exc:
            raise SlackApiError(method, exc.__class__.__name__, retryable=True) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise SlackApiError(
                method,
                f"http_{response.status_code}",
                retryable=True,
                retry_after_seconds=_retry_after(response.headers.get("retry-after")),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SlackApiError(method, "non_json_response", retryable=True) from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            error = str(payload.get("error", "unknown_error")) if isinstance(payload, dict) else "x"
            raise SlackApiError(method, error, retryable=error not in _NON_RETRYABLE_ERRORS)
        return payload

    async def auth_test(self) -> dict[str, Any]:
        """auth.test: bot identity and workspace (readiness only, read-only)."""
        return await self._call("auth.test", {})

    async def channel_info(self, channel: str) -> dict[str, Any]:
        """conversations.info: channel existence and bot membership (readiness only)."""
        payload = await self._call("conversations.info", {"channel": channel})
        info = payload.get("channel")
        return info if isinstance(info, dict) else {}

    async def user_exists(self, user_id: str) -> bool:
        """users.info: resolves an approver ID without exposing profile data (readiness only)."""
        try:
            payload = await self._call("users.info", {"user": user_id})
        except SlackApiError as exc:
            if exc.error == "user_not_found":
                return False
            raise
        user = payload.get("user")
        return isinstance(user, dict) and not bool(user.get("deleted"))

    async def post_message(
        self,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SlackMessageRef:
        body: dict[str, Any] = {
            "channel": channel,
            "text": text,
            "blocks": blocks,
            "unfurl_links": False,
        }
        if metadata is not None:
            body["metadata"] = metadata
        payload = await self._call("chat.postMessage", body)
        return SlackMessageRef(channel=str(payload["channel"]), ts=str(payload["ts"]))

    async def find_message(
        self, channel: str, approval_request_id: str, *, oldest: datetime
    ) -> SlackMessageRef | None:
        """conversations.history with include_all_metadata (needs the channels:history or
        groups:history scope); scans at most a few pages after `oldest`."""
        cursor: str | None = None
        for _ in range(5):
            body: dict[str, Any] = {
                "channel": channel,
                "oldest": f"{oldest.timestamp():.6f}",
                "limit": 200,
                "include_all_metadata": True,
            }
            if cursor:
                body["cursor"] = cursor
            payload = await self._call("conversations.history", body)
            for message in payload.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                if _metadata_request_id(message.get("metadata")) == approval_request_id:
                    return SlackMessageRef(channel=channel, ts=str(message["ts"]))
            cursor = (payload.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        return None

    async def post_response(self, response_url: str, text: str) -> None:
        if not response_url.startswith("https://hooks.slack.com/"):
            raise SlackApiError("response_url", "untrusted_response_url", retryable=False)
        try:
            response = await self._hooks.post(
                response_url,
                json={"response_type": "ephemeral", "replace_original": False, "text": text},
            )
        except httpx.HTTPError as exc:
            raise SlackApiError("response_url", exc.__class__.__name__, retryable=True) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise SlackApiError(
                "response_url",
                f"http_{response.status_code}",
                retryable=True,
                retry_after_seconds=_retry_after(response.headers.get("retry-after")),
            )
        if response.status_code >= 400:
            raise SlackApiError("response_url", f"http_{response.status_code}", retryable=False)

    async def update_message(
        self, ref: SlackMessageRef, text: str, blocks: list[dict[str, Any]]
    ) -> SlackMessageRef:
        payload = await self._call(
            "chat.update", {"channel": ref.channel, "ts": ref.ts, "text": text, "blocks": blocks}
        )
        return SlackMessageRef(channel=str(payload["channel"]), ts=str(payload["ts"]))
