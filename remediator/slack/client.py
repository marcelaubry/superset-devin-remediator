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

from ..models import SlackFakeMessage

logger = logging.getLogger(__name__)

FAKE_POST_FAILURE = "fake Slack adapter configured to fail chat.postMessage"
# Fixture issue numbers whose first chat.postMessage attempts fail in the fake client; used by
# scripts/simulate.py to exercise notification failure + operator retry without restarting
# the worker. The fake recognises the issue from the "owner/repo#N" fallback text.
FAKE_POST_FAILURE_ISSUES: frozenset[int] = frozenset({4699})
_ISSUE_REF = re.compile(r"#(\d+)")


class SlackApiError(Exception):
    def __init__(self, method: str, error: str, *, retryable: bool) -> None:
        super().__init__(f"Slack {method} failed: {error}")
        self.method = method
        self.error = error
        self.retryable = retryable


@dataclass(frozen=True)
class SlackMessageRef:
    channel: str
    ts: str


class SlackClient(Protocol):
    async def post_message(
        self, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> SlackMessageRef: ...

    async def update_message(
        self, ref: SlackMessageRef, text: str, blocks: list[dict[str, Any]]
    ) -> SlackMessageRef: ...

    async def aclose(self) -> None: ...


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
        self._fail_posts = fail_posts
        self._failing_issue_attempts = failing_issue_attempts
        self._post_attempts: dict[int, int] = {}
        self._clock = clock

    def __repr__(self) -> str:
        return f"FakeSlackClient(fail_posts={self._fail_posts})"

    async def aclose(self) -> None:
        return None

    async def post_message(
        self, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> SlackMessageRef:
        if self._fail_posts:
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
                    channel=channel, ts=ts, text=text, blocks=blocks, created_at=now, updated_at=now
                )
            )
            await session.commit()
        return SlackMessageRef(channel=channel, ts=ts)

    async def update_message(
        self, ref: SlackMessageRef, text: str, blocks: list[dict[str, Any]]
    ) -> SlackMessageRef:
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

    def __repr__(self) -> str:
        return "LiveSlackClient()"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(f"/{method}", json=body)
        except httpx.HTTPError as exc:
            raise SlackApiError(method, exc.__class__.__name__, retryable=True) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise SlackApiError(method, f"http_{response.status_code}", retryable=True)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SlackApiError(method, "non_json_response", retryable=True) from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            error = str(payload.get("error", "unknown_error")) if isinstance(payload, dict) else "x"
            raise SlackApiError(method, error, retryable=error not in _NON_RETRYABLE_ERRORS)
        return payload

    async def post_message(
        self, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> SlackMessageRef:
        payload = await self._call(
            "chat.postMessage",
            {"channel": channel, "text": text, "blocks": blocks, "unfurl_links": False},
        )
        return SlackMessageRef(channel=str(payload["channel"]), ts=str(payload["ts"]))

    async def update_message(
        self, ref: SlackMessageRef, text: str, blocks: list[dict[str, Any]]
    ) -> SlackMessageRef:
        payload = await self._call(
            "chat.update", {"channel": ref.channel, "ts": ref.ts, "text": text, "blocks": blocks}
        )
        return SlackMessageRef(channel=str(payload["channel"]), ts=str(payload["ts"]))
