"""Phase 3: Slack approval + GitHub label dispatch, end-to-end against PostgreSQL.

Every scenario goes through the production endpoints (`/webhooks/slack/actions`,
`/webhooks/github`, `/operator/...`) with the fake Slack/GitHub adapters. No test talks to
Slack, GitHub or Devin.
"""

import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remediator.approvals import hash_action_token, retired_token_hash
from remediator.config import Settings
from remediator.devin.fake import FakeDevinClient, FakeScenario
from remediator.github.client import FakeGitHubClient, GitHubApiError
from remediator.lifecycle import CaseState, transition
from remediator.models import (
    OUTBOX_KIND_GITHUB_APPLY_LABEL,
    OUTBOX_KIND_GITHUB_REJECTION_COMMENT,
    OUTBOX_KIND_SLACK_APPROVAL_REQUEST,
    OUTBOX_RECORD_ONLY_KINDS,
    ApprovalDecision,
    ApprovalEvent,
    ApprovalRequest,
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    DeliveryStatus,
    EventStatus,
    NotificationOutbox,
    NotificationStatus,
    OutboxChannel,
    OutboxStatus,
    SlackAction,
    SlackFakeMessage,
    WebhookEvent,
)
from remediator.slack.blocks import ACTION_APPROVE, ACTION_REJECT
from remediator.slack.client import FakeSlackClient, SlackApiError
from remediator.worker.outbox import OutboxDispatcher
from remediator.worker.processor import process_case, process_event

ELIGIBLE_BODY = (
    "Steps to reproduce:\n1. Run.\nExpected behavior works. "
    "Actual behavior fails. Acceptance criteria: fixed. Similar existing pattern."
)
APPROVER = "U_APPROVER_ONE"
OUTSIDER = "U_NOT_ALLOWED"
SLACK_SECRET = "slack-signing-secret-for-tests-only"
GITHUB_SECRET = b"secret"


def issue_payload(number: int, action: str = "opened", label: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "repository": {"full_name": "apache/superset"},
        "issue": {
            "number": number,
            "title": "Fix chart rendering regression",
            "body": ELIGIBLE_BODY,
            "html_url": f"https://github.com/apache/superset/issues/{number}",
            "labels": [{"name": "bug"}, {"name": "devin-candidate"}],
        },
    }
    if label:
        payload["label"] = {"name": label}
        payload["issue"]["labels"].append({"name": label})
    return payload


def slack_headers(body: bytes, *, ts: str | None = None, secret: str = SLACK_SECRET) -> dict:
    ts = ts or str(int(time.time()))
    digest = hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": f"v0={digest}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def slack_body(
    token: str,
    user: str = APPROVER,
    action_id: str = ACTION_APPROVE,
    action_ts: str = "1.000",
    reason: str | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "type": "block_actions",
        "user": {"id": user, "username": "someone"},
        "channel": {"id": "C_SPOOFED"},
        # Anything here that names a case/issue/repository must be ignored by the server.
        "container": {"message_ts": "0.0"},
        "actions": [
            {
                "type": "button",
                "block_id": "remediation_decision",
                "action_id": action_id,
                "value": token,
                "action_ts": action_ts,
            }
        ],
    }
    if reason is not None:
        payload["state"] = {
            "values": {
                "rejection_reason": {
                    "rejection_reason": {
                        "type": "static_select",
                        "selected_option": {"value": reason},
                    }
                }
            }
        }
    return urlencode({"payload": json.dumps(payload)}).encode()


def github_headers(body: bytes, delivery: str) -> dict[str, str]:
    return {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": "sha256="
        + hmac.new(GITHUB_SECRET, body, hashlib.sha256).hexdigest(),
    }


class Harness:
    def __init__(
        self,
        app: Any,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        fail_slack: bool = False,
        fail_labels: bool = False,
    ) -> None:
        self.factory = factory
        self.settings = settings
        self.devin = FakeDevinClient()
        self.slack = FakeSlackClient(factory, fail_posts=fail_slack)
        self.github = FakeGitHubClient(
            settings.allowed_repositories,
            fail_labels=fail_labels,
            failing_issue_attempts=settings.outbox_max_attempts,
        )
        self.dispatcher = OutboxDispatcher(
            settings, factory, self.slack, self.github, "test-worker"
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        self.operator = {"Authorization": "Bearer operator"}

    async def triage(self, number: int, *, scenario: FakeScenario | None = None) -> Case:
        """Run the Phase 2 triage pipeline for a fixture issue via the fake Devin client."""
        devin = FakeDevinClient(scenarios={number: scenario} if scenario else None)
        self.devin = devin
        async with self.factory() as session:
            event = WebhookEvent(
                delivery_id=f"triage-{number}",
                event_type="issues",
                action="opened",
                repository="apache/superset",
                payload=issue_payload(number),
                status=EventStatus.PENDING,
            )
            session.add(event)
            await session.commit()
            await process_event(session, event, devin, self.settings)
            case = await session.scalar(select(Case).where(Case.issue_number == number))
            assert case is not None
            return case

    async def drain(self, limit: int = 50) -> int:
        processed = 0
        for _ in range(limit):
            row = await self.dispatcher.claim()
            if row is None:
                break
            await self.dispatcher.dispatch(row.id)
            processed += 1
        return processed

    async def approval(self, case_id: Any) -> ApprovalRequest:
        async with self.factory() as session:
            request = await session.scalar(
                select(ApprovalRequest).where(ApprovalRequest.case_id == case_id)
            )
            assert request is not None
            return request

    async def case(self, case_id: Any) -> Case:
        async with self.factory() as session:
            case = await session.get(Case, case_id)
            assert case is not None
            return case

    async def outbox(self, case_id: Any, kind: str | None = None) -> list[NotificationOutbox]:
        async with self.factory() as session:
            stmt = select(NotificationOutbox).where(NotificationOutbox.case_id == case_id)
            if kind:
                stmt = stmt.where(NotificationOutbox.kind == kind)
            return list((await session.scalars(stmt.order_by(NotificationOutbox.created_at))).all())

    async def events(self, request_id: Any) -> list[str]:
        async with self.factory() as session:
            rows = await session.scalars(
                select(ApprovalEvent.kind)
                .where(ApprovalEvent.approval_request_id == request_id)
                .order_by(ApprovalEvent.seq)
            )
            return list(rows.all())

    async def fake_messages(self) -> list[SlackFakeMessage]:
        async with self.factory() as session:
            return list((await session.scalars(select(SlackFakeMessage))).all())

    async def token_from_slack(self) -> str:
        """Read the button value the way a human's Slack client would: from the message."""
        messages = await self.fake_messages()
        assert len(messages) == 1
        for block in messages[0].blocks:
            if block.get("type") != "actions":
                continue
            for element in block["elements"]:
                if element.get("action_id") == ACTION_APPROVE:
                    return str(element["value"])
        raise AssertionError("approval message has no approve button")

    async def click(
        self,
        token: str,
        *,
        user: str = APPROVER,
        action_id: str = ACTION_APPROVE,
        action_ts: str = "1.000",
        ts: str | None = None,
        secret: str = SLACK_SECRET,
        body: bytes | None = None,
        reason: str | None = None,
    ) -> httpx.Response:
        body = body if body is not None else slack_body(token, user, action_id, action_ts, reason)
        return await self.client.post(
            "/webhooks/slack/actions",
            content=body,
            headers=slack_headers(body, ts=ts, secret=secret),
        )

    async def label_webhook(self, number: int, delivery: str = "label-1") -> httpx.Response:
        body = json.dumps(issue_payload(number, "labeled", "devin:remediate")).encode()
        return await self.client.post(
            "/webhooks/github", content=body, headers=github_headers(body, delivery)
        )

    async def notify_and_approve(self, number: int = 4213) -> tuple[Case, ApprovalRequest, str]:
        case = await self.triage(number)
        assert case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
        assert await self.drain() == 1  # Slack approval request
        token = await self.token_from_slack()
        response = await self.click(token)
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "approved"
        return case, await self.approval(case.id), token


@pytest_asyncio.fixture
async def harness_factory(
    test_app: Any,
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_settings: Settings,
) -> AsyncIterator[Callable[..., Awaitable[Harness]]]:
    created: list[Harness] = []

    async def make(**kwargs: Any) -> Harness:
        harness = Harness(test_app, integration_session_factory, test_settings, **kwargs)
        created.append(harness)
        return harness

    yield make
    for harness in created:
        await harness.client.aclose()


@pytest_asyncio.fixture
async def harness(harness_factory: Callable[..., Awaitable[Harness]]) -> Harness:
    return await harness_factory()


# --------------------------------------------------------------------------- notification


@pytest.mark.asyncio
async def test_only_remediation_candidates_notify_slack(harness: Harness) -> None:
    candidate = await harness.triage(4213)
    assert candidate.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    not_candidate = await harness.triage(4214, scenario=FakeScenario.NEEDS_HUMAN)
    assert not_candidate.state != CaseState.AWAITING_REMEDIATION_APPROVAL

    slack_rows = await harness.outbox(candidate.id, OUTBOX_KIND_SLACK_APPROVAL_REQUEST)
    assert len(slack_rows) == 1
    assert await harness.outbox(not_candidate.id, OUTBOX_KIND_SLACK_APPROVAL_REQUEST) == []
    async with harness.factory() as session:
        requests = (await session.scalars(select(ApprovalRequest))).all()
    assert [r.case_id for r in requests] == [candidate.id]

    await harness.drain()
    messages = await harness.fake_messages()
    assert len(messages) == 1
    text = json.dumps(messages[0].blocks)
    assert "apache/superset#4213" in text
    assert "Approve remediation" in text and "Reject" in text
    assert ELIGIBLE_BODY not in text  # never the raw issue body
    request = await harness.approval(candidate.id)
    assert request.notification_status == NotificationStatus.SENT
    assert request.slack_channel == "C_TEST" and request.slack_message_ts == messages[0].ts
    assert request.action_token_hash is not None
    # Only the hash of the token is persisted; the token itself lives in the Slack message.
    token = await harness.token_from_slack()
    assert request.action_token_hash == hash_action_token(token)
    assert token not in json.dumps(request.__dict__, default=str)


@pytest.mark.asyncio
async def test_slack_notification_failure_never_fails_triage(
    harness_factory: Callable[..., Awaitable[Harness]], caplog: pytest.LogCaptureFixture
) -> None:
    harness = await harness_factory(fail_slack=True)
    case = await harness.triage(4213)
    assert case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    with caplog.at_level(logging.WARNING):
        attempts = await harness.drain()
    assert attempts == harness.settings.outbox_max_attempts
    rows = await harness.outbox(case.id, OUTBOX_KIND_SLACK_APPROVAL_REQUEST)
    assert rows[0].status == OutboxStatus.FAILED
    assert rows[0].attempts_count == harness.settings.outbox_max_attempts
    assert rows[0].last_error and "fail chat.postMessage" in rows[0].last_error
    request = await harness.approval(case.id)
    assert request.notification_status == NotificationStatus.FAILED
    assert request.decision == ApprovalDecision.PENDING
    # The validated triage result and the case state are untouched.
    assert (await harness.case(case.id)).state == CaseState.AWAITING_REMEDIATION_APPROVAL
    assert SLACK_SECRET not in caplog.text

    # An authenticated operator retry re-queues the row and the notification then lands.
    harness.slack._fail_posts = False
    retry = await harness.client.post(
        f"/operator/outbox/{rows[0].id}/retry", headers=harness.operator
    )
    assert retry.status_code == 200
    assert await harness.drain() == 1
    assert (await harness.approval(case.id)).notification_status == NotificationStatus.SENT


# ------------------------------------------------------------------ signature / replay


@pytest.mark.asyncio
async def test_slack_signature_and_replay_checks_happen_before_parsing(harness: Harness) -> None:
    body = slack_body("whatever")
    # Missing headers.
    response = await harness.client.post("/webhooks/slack/actions", content=body)
    assert response.status_code == 401
    # Wrong secret.
    response = await harness.client.post(
        "/webhooks/slack/actions", content=body, headers=slack_headers(body, secret="other")
    )
    assert response.status_code == 401
    # Tampered body (signature computed over a different body).
    headers = slack_headers(body)
    response = await harness.client.post(
        "/webhooks/slack/actions", content=body + b"&x=1", headers=headers
    )
    assert response.status_code == 401
    # Stale timestamp (outside the 300 s window) but otherwise valid signature.
    stale = str(int(time.time()) - 301)
    response = await harness.client.post(
        "/webhooks/slack/actions", content=body, headers=slack_headers(body, ts=stale)
    )
    assert response.status_code == 401
    assert "timestamp" in response.text
    # Future-dated timestamp is rejected too.
    future = str(int(time.time()) + 600)
    response = await harness.client.post(
        "/webhooks/slack/actions", content=body, headers=slack_headers(body, ts=future)
    )
    assert response.status_code == 401
    # Garbage timestamp.
    headers = slack_headers(body)
    headers["X-Slack-Request-Timestamp"] = "not-a-number"
    response = await harness.client.post("/webhooks/slack/actions", content=body, headers=headers)
    assert response.status_code == 401
    # Wrong signature version prefix.
    headers = slack_headers(body)
    headers["X-Slack-Signature"] = headers["X-Slack-Signature"].replace("v0=", "v1=")
    response = await harness.client.post("/webhooks/slack/actions", content=body, headers=headers)
    assert response.status_code == 401
    # Malformed body with a valid signature is only rejected *after* verification (400).
    junk = b"payload=%7Bnot-json"
    response = await harness.client.post(
        "/webhooks/slack/actions", content=junk, headers=slack_headers(junk)
    )
    assert response.status_code == 400
    # Nothing was recorded by any of the above.
    async with harness.factory() as session:
        assert (await session.scalars(select(SlackAction))).all() == []


@pytest.mark.asyncio
async def test_unknown_token_unauthorized_user_and_duplicates(harness: Harness) -> None:
    case = await harness.triage(4213)
    await harness.drain()
    token = await harness.token_from_slack()

    # Unknown token: acknowledged to Slack (200) but refused, nothing recorded or leaked.
    response = await harness.click("not-the-token")
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["outcome"] == "unknown_token"
    assert "case_state" not in response.json()

    # Unauthorized user: refused, recorded on the timeline, no decision.
    response = await harness.click(token, user=OUTSIDER)
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["outcome"] == "unauthorized"
    assert "case_state" not in response.json()
    request = await harness.approval(case.id)
    assert request.decision == ApprovalDecision.PENDING
    assert "unauthorized_action" in await harness.events(request.id)
    assert (await harness.case(case.id)).state == CaseState.AWAITING_REMEDIATION_APPROVAL

    # Real approval.
    first = await harness.click(token, action_ts="10.5")
    assert first.status_code == 200 and first.json()["outcome"] == "approved"
    # Same click replayed (same action_ts + user + token): idempotent no-op with current state.
    again = await harness.click(token, action_ts="10.5")
    assert again.status_code == 200
    assert again.json()["outcome"] == "duplicate"
    assert again.json()["decision"] == "APPROVED"
    # A different click after the decision reports the decision without changing anything.
    reject_late = await harness.click(token, action_id=ACTION_REJECT, action_ts="11.0")
    assert reject_late.status_code == 200
    assert reject_late.json()["outcome"] == "already_decided"
    request = await harness.approval(case.id)
    assert request.decision == ApprovalDecision.APPROVED
    assert request.decided_by_slack_user_id == APPROVER
    label_rows = await harness.outbox(case.id, OUTBOX_KIND_GITHUB_APPLY_LABEL)
    assert len(label_rows) == 1
    assert await harness.outbox(case.id, OUTBOX_KIND_GITHUB_REJECTION_COMMENT) == []


@pytest.mark.asyncio
async def test_expired_token_is_rejected(harness: Harness) -> None:
    case = await harness.triage(4213)
    await harness.drain()
    token = await harness.token_from_slack()
    request = await harness.approval(case.id)
    expire = await harness.client.post(
        f"/operator/approvals/{request.id}/expire", headers=harness.operator
    )
    assert expire.status_code == 200
    response = await harness.click(token)
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["outcome"] == "expired_token"
    request = await harness.approval(case.id)
    assert request.decision == ApprovalDecision.EXPIRED
    assert request.action_token_hash == retired_token_hash(hash_action_token(token))
    assert await harness.outbox(case.id, OUTBOX_KIND_GITHUB_APPLY_LABEL) == []
    # Operator expire needs auth and is idempotent.
    unauth = await harness.client.post(f"/operator/approvals/{request.id}/expire")
    assert unauth.status_code in {401, 303}
    twice = await harness.client.post(
        f"/operator/approvals/{request.id}/expire", headers=harness.operator
    )
    assert twice.status_code == 409


# ------------------------------------------------------------------------- approval


@pytest.mark.asyncio
async def test_full_approval_flow_reaches_remediation_approved_only_after_webhook(
    harness: Harness,
) -> None:
    case, request, token = await harness.notify_and_approve()
    assert request.decision == ApprovalDecision.APPROVED
    assert request.decided_at is not None
    # The live token hash is retired on decision; a repeat click still resolves to the
    # current state without ever being approvable again.
    assert request.action_token_hash == retired_token_hash(hash_action_token(token))
    assert request.action_token_hash != hash_action_token(token)
    assert request.decision_action_id.startswith(f"{ACTION_APPROVE}:")
    assert request.label_operation == "add_label:devin:remediate"
    assert request.delivery_status == DeliveryStatus.PENDING
    assert request.triage_result_hash
    # Approval alone does not move the case.
    assert (await harness.case(case.id)).state == CaseState.AWAITING_REMEDIATION_APPROVAL

    # Worker applies the label + comment (fake GitHub) and updates the Slack message.
    await harness.drain()
    assert harness.github.label_calls == [("apache/superset", 4213, "devin:remediate")]
    assert len(harness.github.comment_calls) == 1
    comment = harness.github.comment_calls[0][2]
    assert APPROVER in comment and "Triage session" in comment and "/cases/" in comment
    request = await harness.approval(case.id)
    assert request.delivery_status == DeliveryStatus.LABEL_APPLIED
    assert request.github_comment_id is not None
    # Still not approved: GitHub has not confirmed via webhook yet.
    assert (await harness.case(case.id)).state == CaseState.AWAITING_REMEDIATION_APPROVAL
    # Approval never created a Devin session (only the one triage session exists).
    assert harness.devin.create_calls == 1
    async with harness.factory() as session:
        kinds = (
            await session.scalars(select(Attempt.kind).where(Attempt.case_id == case.id))
        ).all()
    assert kinds == [AttemptKind.TRIAGE]

    # Re-running the dispatcher (e.g. after a restart) never re-applies the label.
    rows = await harness.outbox(case.id, OUTBOX_KIND_GITHUB_APPLY_LABEL)
    assert rows[0].status == OutboxStatus.SENT
    await harness.drain()
    assert len(harness.github.label_calls) == 1

    # Signed GitHub label webhook -> REMEDIATION_APPROVED.
    response = await harness.label_webhook(4213)
    assert response.status_code in {200, 202}
    async with harness.factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(WebhookEvent.delivery_id == "label-1")
        )
        assert event is not None
        await process_event(session, event, harness.devin, harness.settings)
    assert (await harness.case(case.id)).state == CaseState.REMEDIATION_APPROVED
    request = await harness.approval(case.id)
    assert request.delivery_status == DeliveryStatus.CONFIRMED
    assert request.label_confirmed_at is not None
    # A duplicate webhook delivery is a no-op.
    response = await harness.label_webhook(4213, delivery="label-1")
    assert response.status_code in {200, 202}
    assert (await harness.case(case.id)).state == CaseState.REMEDIATION_APPROVED
    assert harness.devin.create_calls == 1

    # Slack message was updated along the way and buttons are gone.
    await harness.drain()
    messages = await harness.fake_messages()
    assert len(messages) == 1 and messages[0].update_count >= 2
    text = json.dumps(messages[0].blocks)
    assert ACTION_APPROVE not in text and "`devin:remediate` applied" in text
    events = await harness.events(request.id)
    assert events[:2] == ["approval_requested", "slack_notified"]
    assert "approved" in events and "label_applied" in events and "label_confirmed" in events
    assert (
        events.index("approved") < events.index("label_applied") < events.index("label_confirmed")
    )


@pytest.mark.asyncio
async def test_label_webhook_without_approval_never_advances(harness: Harness) -> None:
    case = await harness.triage(4213)
    await harness.drain()
    response = await harness.label_webhook(4213, delivery="rogue-label")
    assert response.status_code in {200, 202}
    async with harness.factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(WebhookEvent.delivery_id == "rogue-label")
        )
        assert event is not None
        await process_event(session, event, harness.devin, harness.settings)
        assert event.status == EventStatus.PROCESSED
    assert (await harness.case(case.id)).state == CaseState.AWAITING_REMEDIATION_APPROVAL
    assert (await harness.approval(case.id)).decision == ApprovalDecision.PENDING
    # Unsigned label webhook is rejected outright.
    body = json.dumps(issue_payload(4213, "labeled", "devin:remediate")).encode()
    bad = await harness.client.post(
        "/webhooks/github",
        content=body,
        headers={**github_headers(body, "rogue-2"), "X-Hub-Signature-256": "sha256=00"},
    )
    assert bad.status_code == 401


# ------------------------------------------------------------------------- rejection


@pytest.mark.asyncio
async def test_rejection_comments_and_never_labels(harness: Harness) -> None:
    case = await harness.triage(4213)
    await harness.drain()
    token = await harness.token_from_slack()
    response = await harness.click(token, action_id=ACTION_REJECT, reason="too_risky<script>")
    assert response.status_code == 200
    assert response.json()["outcome"] == "rejected"
    assert response.json()["case_state"] == "REMEDIATION_REJECTED"
    request = await harness.approval(case.id)
    assert request.decision == ApprovalDecision.REJECTED
    assert request.decision_reason == "too_risky<script>"
    assert request.decided_by_slack_user_id == APPROVER and request.decided_at is not None
    assert (await harness.case(case.id)).state == CaseState.REMEDIATION_REJECTED
    await harness.drain()
    assert harness.github.label_calls == []
    assert len(harness.github.comment_calls) == 1
    comment = harness.github.comment_calls[0][2]
    assert "Remediation rejected" in comment and "too_risky" in comment
    assert "<script>" not in comment
    assert harness.devin.create_calls == 1
    messages = await harness.fake_messages()
    text = json.dumps(messages[0].blocks)
    assert ACTION_APPROVE not in text and "rejected" in text.lower()
    # A late approve click cannot resurrect the case.
    late = await harness.click(token, action_ts="99")
    assert late.status_code == 200 and late.json()["outcome"] == "already_decided"
    assert await harness.outbox(case.id, OUTBOX_KIND_GITHUB_APPLY_LABEL) == []


# -------------------------------------------------------------- GitHub failure / retry


@pytest.mark.asyncio
async def test_github_label_failure_preserves_approval_and_operator_retry(
    harness: Harness,
) -> None:
    case, request, _ = await harness.notify_and_approve(4688)  # fixture: label writes fail
    await harness.drain()
    rows = await harness.outbox(case.id, OUTBOX_KIND_GITHUB_APPLY_LABEL)
    assert rows[0].status == OutboxStatus.FAILED
    assert rows[0].attempts_count == harness.settings.outbox_max_attempts
    assert rows[0].last_error and "fake GitHub label failure" in rows[0].last_error
    assert len(harness.github.label_calls) == harness.settings.outbox_max_attempts
    request = await harness.approval(case.id)
    assert request.decision == ApprovalDecision.APPROVED  # approval retained
    assert request.delivery_status == DeliveryStatus.FAILED
    assert (await harness.case(case.id)).state == CaseState.APPROVAL_DELIVERY_FAILED
    await harness.drain()
    assert "delivery failed" in json.dumps((await harness.fake_messages())[0].blocks).lower()

    # Dashboard shows the failure and exposes the retry.
    page = await harness.client.get(f"/cases/{case.id}", headers=harness.operator)
    assert page.status_code == 200
    assert "APPROVAL_DELIVERY_FAILED" in page.text and "fake GitHub label failure" in page.text
    assert f"/operator/outbox/{rows[0].id}/retry" in page.text

    # Retry needs auth, must not require a new approval, and eventually succeeds.
    unauth = await harness.client.post(f"/operator/outbox/{rows[0].id}/retry")
    assert unauth.status_code in {401, 303}
    retry = await harness.client.post(
        f"/operator/outbox/{rows[0].id}/retry", headers=harness.operator
    )
    assert retry.status_code == 200
    await harness.drain()
    request = await harness.approval(case.id)
    assert request.delivery_status == DeliveryStatus.LABEL_APPLIED
    assert request.decided_by_slack_user_id == APPROVER
    assert harness.github.issues[("apache/superset", 4688)].labels == ["devin:remediate"]
    assert (await harness.case(case.id)).state == CaseState.APPROVAL_DELIVERY_FAILED
    # Webhook confirmation still works from the failed state.
    await harness.label_webhook(4688, delivery="label-4688")
    async with harness.factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(WebhookEvent.delivery_id == "label-4688")
        )
        assert event is not None
        await process_event(session, event, harness.devin, harness.settings)
    assert (await harness.case(case.id)).state == CaseState.REMEDIATION_APPROVED
    assert harness.devin.create_calls == 1
    # Retrying a non-failed row is refused.
    assert (
        await harness.client.post(f"/operator/outbox/{rows[0].id}/retry", headers=harness.operator)
    ).status_code == 409


@pytest.mark.asyncio
async def test_pending_outbox_survives_restart(harness: Harness) -> None:
    case, _, _ = await harness.notify_and_approve()
    rows = await harness.outbox(case.id, OUTBOX_KIND_GITHUB_APPLY_LABEL)
    # Simulate a worker that claimed the row then died: lease is still held.
    claimed = await harness.dispatcher.claim()
    assert claimed is not None and claimed.kind == OUTBOX_KIND_GITHUB_APPLY_LABEL
    async with harness.factory() as session:
        row = await session.get(NotificationOutbox, rows[0].id)
        assert row is not None
        assert row.claimed_by == "test-worker" and row.status == OutboxStatus.PENDING
    # Another worker cannot steal it while the lease is valid...
    other = OutboxDispatcher(
        harness.settings, harness.factory, harness.slack, harness.github, "worker-2"
    )
    while (pending := await other.claim()) is not None:
        assert pending.kind != OUTBOX_KIND_GITHUB_APPLY_LABEL
        await other.dispatch(pending.id)
    assert harness.github.label_calls == []
    # ...but once the lease expires the work is picked up and completed.
    async with harness.factory() as session:
        row = await session.get(NotificationOutbox, rows[0].id)
        assert row is not None
        row.lease_expires_at = row.last_attempt_at
        await session.commit()
    while (row := await other.claim()) is not None:
        await other.dispatch(row.id)
    assert harness.github.label_calls == [("apache/superset", 4213, "devin:remediate")]
    assert (await harness.approval(case.id)).delivery_status == DeliveryStatus.LABEL_APPLIED


# -------------------------------------------------------------------------- dashboard


@pytest.mark.asyncio
async def test_dashboard_and_api_visibility_without_leaking_ids(harness: Harness) -> None:
    case, request, token = await harness.notify_and_approve()
    await harness.drain()
    # Unauthenticated endpoints expose neither Slack user ids nor the action token.
    for path in ("/", "/healthz", "/metrics"):
        response = await harness.client.get(path)
        assert APPROVER not in response.text and token not in response.text
    anonymous = await harness.client.get(f"/cases/{case.id}")
    assert anonymous.status_code in {401, 302, 303, 307}
    anonymous_json = await harness.client.get("/api/cases/apache/superset/4213")
    assert anonymous_json.status_code == 401
    # Authenticated detail shows the Phase 3 status but never the raw token.
    page = await harness.client.get(f"/cases/{case.id}", headers=harness.operator)
    assert page.status_code == 200
    assert "Remediation approval" in page.text
    assert (
        APPROVER in page.text and "LABEL_APPLIED" in page.text and "Approval timeline" in page.text
    )
    assert token not in page.text and SLACK_SECRET not in page.text
    body = (
        await harness.client.get("/api/cases/apache/superset/4213", headers=harness.operator)
    ).json()
    assert body["state"] == "AWAITING_REMEDIATION_APPROVAL"
    assert body["approval"]["decision"] == "APPROVED"
    assert body["approval"]["delivery_status"] == "LABEL_APPLIED"
    assert body["approval"]["decided_by_slack_user_id"] == APPROVER
    assert {row["kind"] for row in body["outbox"]} >= {
        OUTBOX_KIND_SLACK_APPROVAL_REQUEST,
        OUTBOX_KIND_GITHUB_APPLY_LABEL,
    }
    assert token not in json.dumps(body) and "action_token" not in json.dumps(body)
    # Fake Slack channel viewer is operator-only.
    assert (await harness.client.get("/api/slack/fake/messages")).status_code == 401
    messages = await harness.client.get("/api/slack/fake/messages", headers=harness.operator)
    assert messages.status_code == 200 and len(messages.json()) == 1


# ------------------------------------------------------------------ review regressions


async def _requests_for(harness: Harness, case_id: Any) -> list[ApprovalRequest]:
    async with harness.factory() as session:
        rows = await session.scalars(
            select(ApprovalRequest)
            .where(ApprovalRequest.case_id == case_id)
            .order_by(ApprovalRequest.created_at)
        )
        return list(rows.all())


async def _retriage(harness: Harness, case: Case) -> None:
    """Legitimate lifecycle: AWAITING → FAILED → operator retry → RECEIVED → re-triage."""
    async with harness.factory() as session:
        fresh = await session.get(Case, case.id)
        assert fresh is not None
        await transition(session, fresh, CaseState.FAILED, "simulated failure", "test")
        await session.commit()
    retry = await harness.client.post(f"/operator/cases/{case.id}/retry", headers=harness.operator)
    assert retry.status_code == 200 and retry.json()["state"] == CaseState.RECEIVED
    async with harness.factory() as session:
        fresh = await session.get(Case, case.id)
        assert fresh is not None
        await process_case(session, fresh, FakeDevinClient(), harness.settings)
        await session.commit()
        assert fresh.state == CaseState.AWAITING_REMEDIATION_APPROVAL


@pytest.mark.asyncio
async def test_retriage_supersedes_old_round_and_old_token_never_approves(
    harness: Harness,
) -> None:
    case = await harness.triage(4213)
    await harness.drain()
    old_token = await harness.token_from_slack()
    old_hash = hash_action_token(old_token)

    await _retriage(harness, case)
    rounds = await _requests_for(harness, case.id)
    assert len(rounds) == 2
    old, new = rounds
    assert old.decision == ApprovalDecision.SUPERSEDED and old.action_token_hash is None
    assert new.decision == ApprovalDecision.PENDING and new.attempt_id != old.attempt_id
    assert "superseded" in await harness.events(old.id)

    await harness.drain()  # new Slack notification + old-message update
    messages = await harness.fake_messages()
    assert len(messages) == 2
    old_message = next(m for m in messages if m.ts == old.slack_message_ts)
    assert ACTION_APPROVE not in json.dumps(old_message.blocks)
    assert ACTION_REJECT not in json.dumps(old_message.blocks)
    assert "superseded" in json.dumps(old_message.blocks).lower()

    # The old click is verified, acknowledged, and refused: no decision, no label work.
    response = await harness.click(old_token)
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["outcome"] in {"unknown_token", "stale_token"}
    assert "case_state" not in response.json()
    rounds = await _requests_for(harness, case.id)
    assert rounds[0].decision == ApprovalDecision.SUPERSEDED
    assert rounds[1].decision == ApprovalDecision.PENDING
    assert await harness.outbox(case.id, OUTBOX_KIND_GITHUB_APPLY_LABEL) == []
    assert harness.github.label_calls == []
    async with harness.factory() as session:
        assert (
            await session.scalar(
                select(ApprovalRequest).where(ApprovalRequest.action_token_hash == old_hash)
            )
        ) is None

    # The new round works normally.
    new_message = next(m for m in messages if m.ts != old.slack_message_ts)
    new_token = next(
        str(el["value"])
        for block in new_message.blocks
        if block.get("type") == "actions"
        for el in block["elements"]
        if el.get("action_id") == ACTION_APPROVE
    )
    approved = await harness.click(new_token, action_ts="2.000")
    assert approved.status_code == 200 and approved.json()["outcome"] == "approved"
    await harness.drain()
    assert harness.github.label_calls == [("apache/superset", 4213, "devin:remediate")]
    assert (await _requests_for(harness, case.id))[1].delivery_status == (
        DeliveryStatus.LABEL_APPLIED
    )


@pytest.mark.asyncio
async def test_stale_token_with_hash_still_present_is_refused(harness: Harness) -> None:
    """Defence in depth: even if an old round kept its hash, the current-attempt check holds."""
    case = await harness.triage(4213)
    await harness.drain()
    old_token = await harness.token_from_slack()
    await _retriage(harness, case)
    old, _new = await _requests_for(harness, case.id)
    async with harness.factory() as session:
        row = await session.get(ApprovalRequest, old.id)
        assert row is not None
        row.action_token_hash = hash_action_token(old_token)
        row.decision = ApprovalDecision.PENDING
        await session.commit()
    response = await harness.click(old_token)
    assert response.status_code == 200 and response.json()["outcome"] == "stale_token"
    rounds = await _requests_for(harness, case.id)
    assert rounds[0].decision == ApprovalDecision.PENDING  # untouched, not approved
    assert "stale_action" in await harness.events(old.id)
    assert await harness.outbox(case.id, OUTBOX_KIND_GITHUB_APPLY_LABEL) == []
    assert harness.github.label_calls == []


@pytest.mark.asyncio
async def test_label_outbox_refuses_when_approved_attempt_is_no_longer_current(
    harness: Harness,
) -> None:
    case, request, _ = await harness.notify_and_approve()
    rows = await harness.outbox(case.id, OUTBOX_KIND_GITHUB_APPLY_LABEL)
    assert len(rows) == 1
    # A newer triage attempt appears before the worker delivers the label.
    async with harness.factory() as session:
        session.add(
            Attempt(
                case_id=case.id,
                kind=AttemptKind.TRIAGE,
                idempotency_key=f"triage-{case.id}-2",
                operation_key=f"triage-{case.id}-2",
                status=AttemptStatus.SUCCEEDED,
                started_at=datetime.now(UTC),
            )
        )
        await session.commit()
    await harness.drain()
    async with harness.factory() as session:
        row = await session.get(NotificationOutbox, rows[0].id)
        assert row is not None
        assert row.status == OutboxStatus.FAILED
        assert row.last_error and "no longer the case's current attempt" in row.last_error
    assert harness.github.label_calls == []


@pytest.mark.asyncio
async def test_early_signed_label_webhook_does_not_advance_before_our_delivery(
    harness: Harness,
) -> None:
    case, request, _ = await harness.notify_and_approve()
    assert request.delivery_status == DeliveryStatus.PENDING
    # Someone applies the label by hand and GitHub tells us before the worker has run.
    response = await harness.label_webhook(4213, delivery="early-label")
    assert response.status_code in {200, 202}
    async with harness.factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(WebhookEvent.delivery_id == "early-label")
        )
        assert event is not None
        await process_event(session, event, harness.devin, harness.settings)
    assert (await harness.case(case.id)).state == CaseState.AWAITING_REMEDIATION_APPROVAL
    request = await harness.approval(case.id)
    assert request.delivery_status == DeliveryStatus.PENDING
    assert request.label_confirmed_at is None
    assert "label_webhook_unexpected" in await harness.events(request.id)

    # The outbox still owns delivery: label (already present → no second write) + comment.
    await harness.github.add_label("apache/superset", 4213, "devin:remediate")
    harness.github.label_calls.clear()
    await harness.drain()
    request = await harness.approval(case.id)
    assert request.delivery_status == DeliveryStatus.LABEL_APPLIED
    assert request.github_comment_id is not None
    assert harness.github.issues[("apache/superset", 4213)].labels == ["devin:remediate"]
    assert len(harness.github.comment_calls) == 1

    # Now a signed webhook confirms.
    await harness.label_webhook(4213, delivery="late-label")
    async with harness.factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(WebhookEvent.delivery_id == "late-label")
        )
        assert event is not None
        await process_event(session, event, harness.devin, harness.settings)
    assert (await harness.case(case.id)).state == CaseState.REMEDIATION_APPROVED
    assert harness.devin.create_calls == 1  # triage only; approval never creates a session


@pytest.mark.asyncio
async def test_label_webhook_inside_post_to_commit_window_still_confirms(
    harness: Harness,
) -> None:
    """GitHub fires `labeled` as soon as our POST lands, possibly before the worker commits
    `label_applied_at`. That delivery is never resent, so it must still confirm the case."""
    case, request, _ = await harness.notify_and_approve()
    original_label = harness.github.add_label

    async def label_then_webhook(*args: Any, **kwargs: Any) -> Any:
        result = await original_label(*args, **kwargs)
        response = await harness.label_webhook(4213, delivery="in-window")
        assert response.status_code in {200, 202}
        async with harness.factory() as session:
            event = await session.scalar(
                select(WebhookEvent).where(WebhookEvent.delivery_id == "in-window")
            )
            assert event is not None
            await process_event(session, event, harness.devin, harness.settings)
        # Processed while our delivery was in flight: recorded, not confirmed.
        assert (await harness.case(case.id)).state == CaseState.AWAITING_REMEDIATION_APPROVAL
        assert "label_webhook_unexpected" in await harness.events(request.id)
        return result

    harness.github.add_label = label_then_webhook  # type: ignore[method-assign]
    await harness.drain()
    harness.github.add_label = original_label  # type: ignore[method-assign]

    assert (await harness.case(case.id)).state == CaseState.REMEDIATION_APPROVED
    request = await harness.approval(case.id)
    assert request.delivery_status == DeliveryStatus.CONFIRMED
    assert request.label_confirmed_at is not None
    assert request.github_comment_id is not None
    events = await harness.events(request.id)
    assert "label_webhook_replayed" in events and events.count("label_confirmed") == 1
    assert harness.github.label_calls == [("apache/superset", 4213, "devin:remediate")]
    async with harness.factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(WebhookEvent.delivery_id == "in-window")
        )
        assert event is not None
        assert event.status == EventStatus.PROCESSED and event.last_error is None
        assert event.case_id == case.id

    # GitHub redelivery after confirmation is a no-op.
    replay = await harness.label_webhook(4213, delivery="in-window")
    assert replay.json().get("deduplicated") is True
    await harness.label_webhook(4213, delivery="after-confirm")
    async with harness.factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(WebhookEvent.delivery_id == "after-confirm")
        )
        assert event is not None
        await process_event(session, event, harness.devin, harness.settings)
    assert (await harness.case(case.id)).state == CaseState.REMEDIATION_APPROVED
    assert "label_webhook_duplicate" in await harness.events(request.id)
    assert harness.devin.create_calls == 1


@pytest.mark.asyncio
async def test_slack_post_accepted_but_commit_lost_does_not_duplicate_message(
    harness: Harness,
) -> None:
    case = await harness.triage(4213)
    rows = await harness.outbox(case.id, OUTBOX_KIND_SLACK_APPROVAL_REQUEST)
    assert len(rows) == 1

    original_post = harness.slack.post_message

    async def post_then_crash(*args: Any, **kwargs: Any) -> Any:
        await original_post(*args, **kwargs)
        raise SlackApiError("chat.postMessage", "ReadTimeout", retryable=True)

    harness.slack.post_message = post_then_crash  # type: ignore[method-assign]
    claimed = await harness.dispatcher.claim()
    assert claimed is not None
    await harness.dispatcher.dispatch(claimed.id)
    request = await harness.approval(case.id)
    assert request.notification_status == NotificationStatus.SENDING
    assert request.action_token_hash is not None
    assert len(await harness.fake_messages()) == 1
    async with harness.factory() as session:
        row = await session.get(NotificationOutbox, rows[0].id)
        assert row is not None
        assert row.status == OutboxStatus.PENDING and row.attempts_count == 1
        row.next_attempt_at = datetime.now(UTC)
        await session.commit()

    harness.slack.post_message = original_post  # type: ignore[method-assign]
    await harness.drain()
    messages = await harness.fake_messages()
    assert len(messages) == 1  # reconciled, not re-posted
    request = await harness.approval(case.id)
    assert request.notification_status == NotificationStatus.SENT
    assert request.slack_message_ts == messages[0].ts
    assert "slack_notified" in await harness.events(request.id)
    # The token on the (single) message is the one that is live.
    token = await harness.token_from_slack()
    assert hash_action_token(token) == request.action_token_hash
    approved = await harness.click(token)
    assert approved.status_code == 200 and approved.json()["outcome"] == "approved"


@pytest.mark.asyncio
async def test_slack_reconciliation_without_history_scope_fails_loudly_without_repost(
    harness: Harness,
) -> None:
    case = await harness.triage(4213)
    rows = await harness.outbox(case.id, OUTBOX_KIND_SLACK_APPROVAL_REQUEST)
    original_post = harness.slack.post_message
    original_find = harness.slack.find_message

    async def post_then_crash(*args: Any, **kwargs: Any) -> Any:
        await original_post(*args, **kwargs)
        raise SlackApiError("chat.postMessage", "ReadTimeout", retryable=True)

    async def find_without_scope(*args: Any, **kwargs: Any) -> Any:
        raise SlackApiError("conversations.history", "missing_scope", retryable=False)

    harness.slack.post_message = post_then_crash  # type: ignore[method-assign]
    claimed = await harness.dispatcher.claim()
    assert claimed is not None
    await harness.dispatcher.dispatch(claimed.id)
    assert (await harness.approval(case.id)).notification_status == NotificationStatus.SENDING
    async with harness.factory() as session:
        row = await session.get(NotificationOutbox, rows[0].id)
        assert row is not None
        row.next_attempt_at = datetime.now(UTC)
        await session.commit()

    harness.slack.post_message = original_post  # type: ignore[method-assign]
    harness.slack.find_message = find_without_scope  # type: ignore[method-assign]
    posts_before = len(await harness.fake_messages())
    await harness.drain()
    harness.slack.find_message = original_find  # type: ignore[method-assign]

    assert len(await harness.fake_messages()) == posts_before  # never reposted blindly
    request = await harness.approval(case.id)
    assert request.notification_status == NotificationStatus.FAILED
    assert request.slack_message_ts is None
    assert "slack_notification_failed" in await harness.events(request.id)
    async with harness.factory() as session:
        row = await session.get(NotificationOutbox, rows[0].id)
        assert row is not None and row.status == OutboxStatus.FAILED
        assert row.last_error is not None
        assert "missing_scope" in row.last_error and "channels:history" in row.last_error
    # Triage itself is untouched and the case still awaits approval.
    assert (await harness.case(case.id)).state == CaseState.AWAITING_REMEDIATION_APPROVAL

    # Once the scope is granted, an operator retry reconciles the original message.
    retry = await harness.client.post(
        f"/operator/outbox/{rows[0].id}/retry", headers=harness.operator
    )
    assert retry.status_code == 200
    await harness.drain()
    request = await harness.approval(case.id)
    assert request.notification_status == NotificationStatus.SENT
    messages = await harness.fake_messages()
    assert len(messages) == posts_before and request.slack_message_ts == messages[0].ts


@pytest.mark.asyncio
async def test_github_label_and_comment_posts_survive_lost_commits(harness: Harness) -> None:
    case, request, _ = await harness.notify_and_approve()
    rows = await harness.outbox(case.id, OUTBOX_KIND_GITHUB_APPLY_LABEL)

    original_label = harness.github.add_label
    original_comment = harness.github.create_comment

    async def label_then_crash(*args: Any, **kwargs: Any) -> Any:
        await original_label(*args, **kwargs)
        raise GitHubApiError("connection dropped after POST", retryable=True)

    async def comment_then_crash(*args: Any, **kwargs: Any) -> Any:
        await original_comment(*args, **kwargs)
        raise GitHubApiError("connection dropped after POST", retryable=True)

    async def reschedule() -> None:
        async with harness.factory() as session:
            row = await session.get(NotificationOutbox, rows[0].id)
            assert row is not None and row.status == OutboxStatus.PENDING
            row.next_attempt_at = datetime.now(UTC)
            await session.commit()

    async def dispatch_once() -> None:
        """Dispatch rows until the label row has had exactly one more attempt."""
        while (claimed := await harness.dispatcher.claim()) is not None:
            await harness.dispatcher.dispatch(claimed.id)
            if claimed.id == rows[0].id:
                return
        raise AssertionError("label row was not claimable")

    harness.github.add_label = label_then_crash  # type: ignore[method-assign]
    await dispatch_once()
    request = await harness.approval(case.id)
    assert request.label_requested_at is not None and request.label_applied_at is None
    assert harness.github.issues[("apache/superset", 4213)].labels == ["devin:remediate"]
    harness.github.add_label = original_label  # type: ignore[method-assign]

    harness.github.create_comment = comment_then_crash  # type: ignore[method-assign]
    await reschedule()
    await dispatch_once()
    request = await harness.approval(case.id)
    assert request.label_applied_at is not None
    assert request.delivery_status == DeliveryStatus.LABEL_APPLIED
    assert request.comment_requested_at is not None and request.github_comment_id is None
    assert len(harness.github.comment_calls) == 1
    harness.github.create_comment = original_comment  # type: ignore[method-assign]

    await reschedule()
    await harness.drain()
    request = await harness.approval(case.id)
    assert request.github_comment_id is not None
    # Exactly one label write and one comment across three attempts.
    assert harness.github.label_calls == [("apache/superset", 4213, "devin:remediate")]
    assert len(harness.github.comment_calls) == 1
    assert f"<!-- remediator:approval:{request.id} -->" in harness.github.comment_calls[0][2]
    async with harness.factory() as session:
        row = await session.get(NotificationOutbox, rows[0].id)
        assert row is not None and row.status == OutboxStatus.SENT


@pytest.mark.asyncio
async def test_legacy_phase2_outbox_kinds_are_record_only(harness: Harness) -> None:
    case = await harness.triage(4213)
    async with harness.factory() as session:
        for kind in ("eligibility_rejected", "case_failed", "human_blocked", "case_completed"):
            session.add(
                NotificationOutbox(
                    case_id=case.id,
                    channel=OutboxChannel.GITHUB,
                    kind=kind,
                    payload={"reason": "legacy"},
                )
            )
        await session.commit()
    await harness.drain()
    rows = await harness.outbox(case.id)
    legacy = [row for row in rows if row.kind in OUTBOX_RECORD_ONLY_KINDS]
    assert len(legacy) == 4
    assert all(row.status == OutboxStatus.SENT and row.last_error is None for row in legacy)
    assert harness.github.comment_calls == [] and harness.github.label_calls == []


@pytest.mark.asyncio
async def test_ephemeral_feedback_uses_response_url_only(harness: Harness) -> None:
    case = await harness.triage(4213)
    await harness.drain()
    token = await harness.token_from_slack()
    body_payload = json.loads(dict(parse_qsl(slack_body(token, user=OUTSIDER).decode()))["payload"])
    body_payload["response_url"] = "https://hooks.slack.com/actions/T1/B1/xyz"
    body = urlencode({"payload": json.dumps(body_payload)}).encode()
    response = await harness.click(token, body=body)
    assert response.status_code == 200 and response.json()["outcome"] == "unauthorized"
    await harness.drain()
    ephemeral = [m for m in await harness.fake_messages() if m.ephemeral]
    assert len(ephemeral) == 1 and "not an authorized approver" in ephemeral[0].text
    assert token not in ephemeral[0].text
    # An untrusted response_url is ignored entirely.
    body_payload["response_url"] = "https://evil.example/collect"
    body_payload["actions"][0]["action_ts"] = "3.000"
    body = urlencode({"payload": json.dumps(body_payload)}).encode()
    await harness.click(token, body=body)
    await harness.drain()
    assert len([m for m in await harness.fake_messages() if m.ephemeral]) == 1
    request = await harness.approval(case.id)
    assert request.decision == ApprovalDecision.PENDING
