"""Readiness Slack checks against a mock that behaves like the real Web API: the read
methods (`conversations.info`, `users.info`) are GET methods that read their arguments
from the query string / form body and ignore a JSON body."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from remediator.config import Settings
from remediator.readiness import CheckResult, Probes, check_slack

CHANNEL = "C0C2WSL7ELB"
APPROVER = "U0C2WSFK1V1"
SLACK_TOKEN = "xoxb-readiness-secret-token-never-printed"

Handler = Callable[[httpx.Request], httpx.Response]


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://remediator:remediator@localhost:1/none",
        "github_webhook_secret": "webhook-secret-that-is-long-enough",
        "public_base_url": "https://remediator.example.com",
        "slack_client_mode": "live",
        "slack_bot_token": SLACK_TOKEN,
        "slack_signing_secret": "slack-signing-secret-for-readiness-tests",
        "slack_channel_id": CHANNEL,
        "slack_approver_user_ids": APPROVER,
        "cookie_secure": True,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _ok(**payload: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, **payload})


def _err(error: str, **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": False, "error": error, **extra})


PRIVATE_CHANNEL = {
    "id": CHANNEL,
    "name": "devin-remediation-demo",
    "is_archived": False,
    "is_member": True,
    "is_private": True,
}
ACTIVE_USER = {
    "id": APPROVER,
    "name": "aubryma",
    "deleted": False,
    "is_bot": False,
    "team_id": "T0C2Y6DSYCD",
}


def _slack_like(
    channel: dict[str, Any] | None = None,
    channel_error: str | None = None,
    user: dict[str, Any] | None = None,
    user_error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[Handler, list[httpx.Request]]:
    """Slack read methods take documented arguments from the query string only."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == f"Bearer {SLACK_TOKEN}"
        method = request.url.path.rsplit("/", 1)[1]
        args = dict(request.url.params)
        if method == "auth.test":
            return _ok(user="remediator", team="acme", user_id="U0BOT", team_id="T0C2Y6DSYCD")
        if method == "conversations.info":
            # Real Slack: a JSON body is ignored on this GET method, so a missing
            # `channel` surfaces as invalid_arguments.
            if "channel" not in args or set(args) - {
                "channel",
                "include_locale",
                "include_num_members",
            }:
                return _err("invalid_arguments")
            if channel_error:
                return _err(channel_error, **(extra or {}))
            if args.get("channel") != CHANNEL:
                return _err("channel_not_found")
            return _ok(channel=channel or PRIVATE_CHANNEL)
        if method == "users.info":
            if set(args) - {"user", "include_locale"}:
                return _err("invalid_arguments")
            if "user" not in args:
                return _err("user_not_found")
            if user_error:
                return _err(user_error, **(extra or {}))
            if args.get("user") != APPROVER:
                return _err("user_not_found")
            return _ok(user=user or ACTIVE_USER)
        return httpx.Response(404, json={"ok": False, "error": "unknown_method"})

    return handler, seen


async def _run(handler: Handler, settings: Settings | None = None) -> dict[str, CheckResult]:
    transport = httpx.MockTransport(handler)
    probes = Probes(devin=None, github=None, slack=transport, verifier=None, public=None)
    results = await check_slack(settings or _settings(), probes)
    return {r.name: r for r in results}


@pytest.mark.asyncio
async def test_private_channel_with_bot_membership_and_active_approver_pass() -> None:
    handler, seen = _slack_like()
    by_name = await _run(handler)

    assert by_name["slack.identity"].status == "pass"
    assert by_name["slack.channel"].status == "pass", by_name["slack.channel"]
    assert "is_private=True" in by_name["slack.channel"].detail
    assert by_name[f"slack.approver[{APPROVER}]"].status == "pass"
    assert by_name[f"slack.approver[{APPROVER}]"].detail == "active"
    # Documented arguments only, as query parameters, and no JSON body on the reads.
    reads = [r for r in seen if r.url.path.endswith((".info",))]
    assert reads and all(r.method == "GET" and not r.content for r in reads)
    assert {dict(r.url.params).get("channel") for r in reads if "conversations" in r.url.path} == {
        CHANNEL
    }
    assert {dict(r.url.params).get("user") for r in reads if "users" in r.url.path} == {APPROVER}


@pytest.mark.asyncio
async def test_bot_token_is_never_in_query_or_check_detail() -> None:
    handler, seen = _slack_like()
    by_name = await _run(handler)
    assert all("token" not in r.url.params for r in seen)
    assert all(SLACK_TOKEN not in r.detail for r in by_name.values())


@pytest.mark.asyncio
async def test_invalid_arguments_is_reported_as_an_api_error_not_missing_resource() -> None:
    handler, _ = _slack_like(channel_error="invalid_arguments", user_error="invalid_arguments")
    by_name = await _run(handler)

    channel = by_name["slack.channel"]
    assert channel.status == "fail"
    assert channel.detail.startswith("conversations.info returned invalid_arguments")
    approver = by_name[f"slack.approver[{APPROVER}]"]
    assert approver.status == "fail"
    assert approver.detail.startswith("users.info returned invalid_arguments")
    assert "deactivated" not in approver.detail and "unknown" not in approver.detail


@pytest.mark.asyncio
async def test_missing_scope_names_the_needed_scope() -> None:
    handler, _ = _slack_like(
        channel_error="missing_scope",
        user_error="missing_scope",
        extra={"needed": "groups:read,channels:read", "provided": "chat:write"},
    )
    by_name = await _run(handler)

    channel = by_name["slack.channel"]
    assert channel.status == "fail"
    assert "missing_scope" in channel.detail
    assert "needed=groups:read,channels:read" in channel.detail
    assert "provided=chat:write" in channel.detail
    approver = by_name[f"slack.approver[{APPROVER}]"]
    assert approver.status == "fail" and "missing_scope" in approver.detail


@pytest.mark.asyncio
async def test_archived_channel_fails_even_when_bot_is_member() -> None:
    handler, _ = _slack_like(channel={**PRIVATE_CHANNEL, "is_archived": True})
    by_name = await _run(handler)
    channel = by_name["slack.channel"]
    assert channel.status == "fail"
    assert "is_archived=True" in channel.detail


@pytest.mark.asyncio
async def test_channel_without_bot_membership_fails() -> None:
    handler, _ = _slack_like(channel={**PRIVATE_CHANNEL, "is_member": False})
    by_name = await _run(handler)
    channel = by_name["slack.channel"]
    assert channel.status == "fail" and "is_member=False" in channel.detail


@pytest.mark.asyncio
async def test_deactivated_approver_is_labelled_deactivated() -> None:
    handler, _ = _slack_like(user={**ACTIVE_USER, "deleted": True})
    by_name = await _run(handler)
    approver = by_name[f"slack.approver[{APPROVER}]"]
    assert approver.status == "fail" and approver.detail == "deactivated"


@pytest.mark.asyncio
async def test_bot_approver_is_rejected() -> None:
    handler, _ = _slack_like(user={**ACTIVE_USER, "is_bot": True})
    by_name = await _run(handler)
    approver = by_name[f"slack.approver[{APPROVER}]"]
    assert approver.status == "fail" and approver.detail == "is a bot user"


@pytest.mark.asyncio
async def test_unknown_approver_is_labelled_not_found() -> None:
    handler, _ = _slack_like(user_error="user_not_found")
    by_name = await _run(handler)
    approver = by_name[f"slack.approver[{APPROVER}]"]
    assert approver.status == "fail" and approver.detail == "user_not_found"


@pytest.mark.asyncio
async def test_approver_ids_are_trimmed_before_lookup() -> None:
    handler, seen = _slack_like()
    by_name = await _run(handler, _settings(slack_approver_user_ids=f" {APPROVER} , "))
    assert by_name[f"slack.approver[{APPROVER}]"].status == "pass"
    users = [dict(r.url.params)["user"] for r in seen if r.url.path.endswith("users.info")]
    assert users == [APPROVER]
