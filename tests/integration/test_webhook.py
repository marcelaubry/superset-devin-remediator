import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remediator.config import Settings
from remediator.models import EventStatus, WebhookEvent


def payload(
    number: int = 4213,
    *,
    repository: str = "apache/superset",
    labels: list[str] | None = None,
    action: str = "opened",
    added_label: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "action": action,
        "repository": {"full_name": repository},
        "issue": {
            "number": number,
            "title": "Fix chart rendering regression",
            "body": "Steps to reproduce:\n1. Run.\nExpected behavior works. Actual behavior fails.",
            "html_url": f"https://github.com/apache/superset/issues/{number}",
            "labels": [{"name": label} for label in labels or ["devin-candidate"]],
        },
    }
    if added_label:
        result["label"] = {"name": added_label}
    return result


def signature(body: bytes) -> str:
    return "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()


async def send(
    client: httpx.AsyncClient,
    body: bytes,
    delivery: str | None = "delivery-1",
    event: str = "issues",
    signature_header: str | None = None,
) -> httpx.Response:
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": signature_header or signature(body),
    }
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return await client.post("/webhooks/github", content=body, headers=headers)


@pytest.mark.asyncio
async def test_bad_signature_before_json(test_app) -> None:
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await send(client, b"{not-json", signature_header="sha256=bad")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_filtered_events_are_not_persisted(
    test_app, integration_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = json.dumps(payload(repository="other/repo")).encode()
        response = await send(client, body)
    assert response.status_code == 202
    assert response.json()["accepted"] is False
    async with integration_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 0


@pytest.mark.asyncio
async def test_unlabeled_opened_issue_is_accepted_by_default(
    test_app, test_settings, integration_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """No intake label is required by default: every `issues/opened` delivery is persisted
    so the zero-ACU eligibility filter evaluates it."""
    assert Settings(_env_file=None).github_required_label == ""
    test_settings.github_required_label = Settings(_env_file=None).github_required_label
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = json.dumps(payload(labels=["bug"])).encode()
        response = await send(client, body, delivery="unlabeled-opened")
    assert response.status_code == 202
    assert response.json()["accepted"] is True
    async with integration_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 1


@pytest.mark.asyncio
async def test_optional_intake_label_filters_unlabeled_issues(
    test_app, test_settings, integration_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Deployments may opt in to label gating; then unlabeled issues are filtered and an
    `labeled` event adding the intake or remediation label is accepted."""
    test_settings.github_required_label = "devin-candidate"
    try:
        transport = httpx.ASGITransport(app=test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = json.dumps(payload(labels=["bug"])).encode()
            response = await send(client, body, delivery="missing-label")
            assert response.status_code == 202
            assert response.json()["accepted"] is False
            async with integration_session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 0

            body = json.dumps(
                payload(labels=["bug"], action="labeled", added_label="devin-candidate")
            ).encode()
            response = await send(client, body, delivery="labeled-delivery")
            assert response.status_code == 202
            assert response.json()["accepted"] is True

            body = json.dumps(
                payload(labels=["bug"], action="labeled", added_label="devin:remediate")
            ).encode()
            response = await send(client, body, delivery="remediate-labeled-delivery")
            assert response.status_code == 202
            assert response.json()["accepted"] is True
    finally:
        test_settings.github_required_label = ""


@pytest.mark.asyncio
async def test_accepted_and_duplicate_delivery(
    test_app, integration_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    body = json.dumps(payload()).encode()
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await send(client, body, delivery="duplicate-delivery")
        second = await send(client, body, delivery="duplicate-delivery")
    assert first.status_code == 202
    assert first.json()["deduplicated"] is False
    assert second.status_code == 202
    assert second.json()["deduplicated"] is True
    async with integration_session_factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(WebhookEvent.delivery_id == "duplicate-delivery")
        )
        assert event and event.status == EventStatus.PENDING
        assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 1


@pytest.mark.asyncio
async def test_missing_delivery_id(test_app) -> None:
    body = json.dumps(payload()).encode()
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await send(client, body, delivery=None)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_signed_non_object_payload_is_bad_request(test_app) -> None:
    body = b"[]"
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await send(client, body, delivery="array-payload")
    assert response.status_code == 400
    assert response.json()["detail"] == "payload must be a JSON object"
