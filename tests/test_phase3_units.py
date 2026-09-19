"""Phase 3 unit tests: Slack signing, Block Kit hygiene, adapter configuration, secret hygiene."""

import hashlib
import hmac
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from remediator.adapters import SettingsRedactingFilter, build_github_client, build_slack_client
from remediator.config import Settings
from remediator.devin.fake import sample_triage_output
from remediator.github.client import FakeGitHubClient, LiveGitHubClient, RepositoryNotAllowed
from remediator.slack.blocks import (
    ACTION_APPROVE,
    ACTION_REASON,
    ACTION_REJECT,
    ApprovalMessageInput,
    ApprovalMessageStatus,
    build_approval_blocks,
    escape_mrkdwn,
)
from remediator.slack.client import FakeSlackClient, LiveSlackClient
from remediator.slack.signature import (
    SlackVerificationFailure,
    compute_signature,
    verify_slack_request,
)

ROOT = Path(__file__).resolve().parents[1]
SECRET = "unit-test-slack-signing-secret"
NOW = 1_700_000_000.0


def _verify(body: bytes, ts: str, sig: str | None, *, now: float = NOW, skew: int = 300):
    return verify_slack_request(SECRET, body, ts, sig, max_skew_seconds=skew, now=now)


def test_signature_matches_slack_reference_algorithm() -> None:
    body = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    ts = "1700000000"
    expected = hmac.new(SECRET.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256)
    assert compute_signature(SECRET, ts, body) == "v0=" + expected.hexdigest()
    assert _verify(body, ts, compute_signature(SECRET, ts, body)).ok


@pytest.mark.parametrize(
    "ts,sig,failure",
    [
        (None, "v0=abc", SlackVerificationFailure.MISSING_TIMESTAMP),
        ("", "v0=abc", SlackVerificationFailure.MISSING_TIMESTAMP),
        ("yesterday", "v0=abc", SlackVerificationFailure.INVALID_TIMESTAMP),
        (str(int(NOW) - 301), None, SlackVerificationFailure.STALE_TIMESTAMP),
        (str(int(NOW) + 301), None, SlackVerificationFailure.STALE_TIMESTAMP),
        (str(int(NOW)), None, SlackVerificationFailure.MISSING_SIGNATURE),
        (str(int(NOW)), "", SlackVerificationFailure.MISSING_SIGNATURE),
        (str(int(NOW)), "sha256=deadbeef", SlackVerificationFailure.MISSING_SIGNATURE),
        (str(int(NOW)), "v0=deadbeef", SlackVerificationFailure.INVALID_SIGNATURE),
    ],
)
def test_every_signature_and_replay_failure(
    ts: str | None, sig: str | None, failure: SlackVerificationFailure
) -> None:
    result = _verify(b"payload=x", ts, sig)
    assert not result.ok and result.failure == failure


def test_signature_is_bound_to_timestamp_body_and_secret() -> None:
    body = b"payload=x"
    ts = str(int(NOW))
    good = compute_signature(SECRET, ts, body)
    assert _verify(body, ts, good).ok
    assert _verify(body + b"y", ts, good).failure == SlackVerificationFailure.INVALID_SIGNATURE
    other_ts = str(int(NOW) + 1)
    assert _verify(body, other_ts, good).failure == SlackVerificationFailure.INVALID_SIGNATURE
    replayed_later = _verify(body, ts, good, now=NOW + 300)
    assert replayed_later.ok  # boundary: exactly the window is still accepted
    assert _verify(body, ts, good, now=NOW + 301).failure == (
        SlackVerificationFailure.STALE_TIMESTAMP
    )
    other_secret = verify_slack_request("different", body, ts, good, max_skew_seconds=300, now=NOW)
    assert other_secret.failure == SlackVerificationFailure.INVALID_SIGNATURE


# ------------------------------------------------------------------------------- blocks


def _message(**overrides: Any) -> ApprovalMessageInput:
    base: dict[str, Any] = {
        "repository": "apache/superset",
        "issue_number": 4213,
        "issue_title": "Fix <script>alert(1)</script> & friends",
        "issue_url": "https://github.com/apache/superset/issues/4213",
        "devin_session_url": "https://app.devin.ai/sessions/abc",
        "dashboard_url": "http://localhost:8000/cases/1",
        "triage": sample_triage_output(4213, "apache/superset"),
        "action_token": "tok_secret_value",
        "status": ApprovalMessageStatus.AWAITING,
    }
    base.update(overrides)
    return ApprovalMessageInput(**base)


def _buttons(blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for block in blocks:
        if block["type"] == "actions":
            for element in block["elements"]:
                out[element["action_id"]] = element
    return out


def test_blocks_contain_required_fields_and_escape_untrusted_text() -> None:
    blocks = build_approval_blocks(_message())
    text = json.dumps(blocks)
    for needle in (
        "apache/superset#4213",
        "medium",  # severity
        "p2",  # priority
        "82%",  # confidence
        "low",  # risk
        "reproduced with focused test",  # evidence
        "superset/views/core.py",  # affected files
        "single module",  # scope
        "pytest tests/unit_tests/views/test_core.py -q",  # probe
        "tests/unit_tests/views/test_core.py",  # focused tests
        "https://app.devin.ai/sessions/abc",
    ):
        assert needle in text, needle
    assert "<script>" not in text and "&lt;script&gt;" in text
    buttons = _buttons(blocks)
    assert set(buttons) == {
        ACTION_APPROVE,
        ACTION_REJECT,
        ACTION_REASON,
        "view_issue",
        "view_dashboard",
    }
    assert buttons[ACTION_REASON]["type"] == "static_select"
    assert buttons[ACTION_APPROVE]["value"] == "tok_secret_value"
    assert buttons[ACTION_REJECT]["value"] == "tok_secret_value"
    assert buttons[ACTION_REJECT]["style"] == "danger" and buttons[ACTION_APPROVE]["style"] == (
        "primary"
    )
    assert buttons["view_issue"]["url"] == "https://github.com/apache/superset/issues/4213"
    assert buttons["view_dashboard"]["url"] == "http://localhost:8000/cases/1"
    assert len(blocks) <= 50


@pytest.mark.parametrize(
    "status",
    [
        ApprovalMessageStatus.APPROVED_PENDING_GITHUB,
        ApprovalMessageStatus.LABEL_APPLIED,
        ApprovalMessageStatus.REJECTED,
        ApprovalMessageStatus.DELIVERY_FAILED,
        ApprovalMessageStatus.EXPIRED,
    ],
)
def test_decision_buttons_removed_after_terminal_decision(status: ApprovalMessageStatus) -> None:
    blocks = build_approval_blocks(_message(status=status, action_token=None))
    buttons = _buttons(blocks)
    assert ACTION_APPROVE not in buttons and ACTION_REJECT not in buttons
    assert ACTION_REASON not in buttons
    assert {"view_issue", "view_dashboard"} <= set(buttons)
    assert "tok_secret_value" not in json.dumps(blocks)


def test_blocks_enforce_slack_limits_and_drop_raw_body() -> None:
    huge = sample_triage_output(4213, "apache/superset")
    huge["summary"] = "x" * 20_000
    huge["evidence"] = [f"evidence line {i} " + "y" * 500 for i in range(100)]
    huge["affected_files"] = [f"file_{i}.py" for i in range(100)]
    huge["raw_issue_body"] = "SHOULD NEVER APPEAR " * 50
    blocks = build_approval_blocks(_message(triage=huge, issue_title="t" * 5000))
    text = json.dumps(blocks)
    assert "SHOULD NEVER APPEAR" not in text
    for block in blocks:
        if block["type"] == "section":
            if "text" in block:
                assert len(block["text"]["text"]) <= 3000
            for field in block.get("fields", []):
                assert len(field["text"]) <= 2000
        if block["type"] == "header":
            assert len(block["text"]["text"]) <= 150
        if block["type"] == "actions":
            for element in block["elements"]:
                if element["type"] == "static_select":
                    assert len(element["options"]) <= 100
                    continue
                assert len(element["text"]["text"]) <= 75
                if "value" in element:
                    assert len(element["value"]) <= 2000
    assert len(text) < 40_000


def test_escape_mrkdwn() -> None:
    assert escape_mrkdwn("<@U123> & <http://x|y>") == "&lt;@U123&gt; &amp; &lt;http://x|y&gt;"


# ------------------------------------------------------------------------------ config


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_fake_adapters_are_default_and_live_modes_fail_closed() -> None:
    settings = _settings()
    assert settings.slack_client_mode == "fake" and settings.github_client_mode == "fake"
    assert isinstance(build_github_client(settings), FakeGitHubClient)

    with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
        _settings(slack_client_mode="live")
    with pytest.raises(ValueError, match="SLACK_SIGNING_SECRET"):
        _settings(slack_client_mode="live", slack_bot_token="xoxb-" + "a" * 40)
    with pytest.raises(ValueError, match="SLACK_APPROVER_USER_IDS"):
        _settings(
            slack_client_mode="live",
            slack_bot_token="xoxb-" + "a" * 40,
            slack_signing_secret="s" * 32,
            slack_approver_user_ids="",
        )
    with pytest.raises(ValueError, match="https"):
        _settings(
            slack_client_mode="live",
            slack_bot_token="xoxb-" + "a" * 40,
            slack_signing_secret="s" * 32,
            slack_approver_user_ids="U1",
            slack_api_base_url="http://slack.com/api",
        )
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        _settings(github_client_mode="live")
    with pytest.raises(ValueError, match="https"):
        _settings(
            github_client_mode="live",
            github_token="ghp_" + "b" * 40,
            github_api_base_url="http://x",
        )
    with pytest.raises(ValueError, match="fake-mode-only"):
        _settings(
            github_client_mode="live", github_token="ghp_" + "b" * 40, github_fake_fail_labels=True
        )
    with pytest.raises(ValueError, match="GITHUB_REPOSITORY"):
        _settings(github_repository=" , ")


def test_live_adapters_never_expose_credentials(caplog: pytest.LogCaptureFixture) -> None:
    slack_token = "xoxb-" + "q" * 40
    github_token = "github_pat_" + "z" * 40
    signing = "signing-" + "w" * 32
    settings = _settings(
        slack_client_mode="live",
        slack_bot_token=slack_token,
        slack_signing_secret=signing,
        slack_approver_user_ids="U1",
        github_client_mode="live",
        github_token=github_token,
    )
    assert {slack_token, github_token, signing} <= set(settings.secret_values)
    dumped = settings.model_dump_json() + repr(settings) + str(settings)
    for secret in (slack_token, github_token, signing):
        assert secret not in dumped
    slack = build_slack_client(settings, session_factory=None)  # type: ignore[arg-type]
    github = build_github_client(settings)
    assert isinstance(slack, LiveSlackClient) and isinstance(github, LiveGitHubClient)
    for secret in (slack_token, github_token):
        assert secret not in repr(slack) + repr(github) + str(slack) + str(github)

    redactor = SettingsRedactingFilter(settings.secret_values)
    logger = logging.getLogger("phase3.redaction")
    logger.addFilter(redactor)
    with caplog.at_level(logging.INFO, logger="phase3.redaction"):
        logger.info("token=%s secret=%s", slack_token, signing)
        logger.info("plain %s", github_token)
    assert slack_token not in caplog.text and signing not in caplog.text
    assert github_token not in caplog.text and "[REDACTED]" in caplog.text


@pytest.mark.asyncio
async def test_fake_github_client_enforces_allowlist_and_idempotent_labels() -> None:
    client = FakeGitHubClient(["apache/superset"])
    with pytest.raises(RepositoryNotAllowed):
        await client.get_issue("evil/superset", 1)
    with pytest.raises(RepositoryNotAllowed):
        await client.add_label("Apache/Other", 1, "devin:remediate")
    with pytest.raises(RepositoryNotAllowed):
        await client.create_comment("evil/superset", 1, "hi")
    first = await client.add_label("apache/superset", 7, "devin:remediate")
    second = await client.add_label("apache/superset", 7, "devin:remediate")
    assert first.applied and not second.applied
    assert client.issues[("apache/superset", 7)].labels == ["devin:remediate"]


@pytest.mark.asyncio
async def test_live_github_client_checks_allowlist_before_any_http() -> None:
    client = LiveGitHubClient(
        "github_pat_" + "z" * 40, ["apache/superset"], base_url="https://127.0.0.1:9"
    )
    try:
        with pytest.raises(RepositoryNotAllowed):
            await client.add_label("someone/else", 1, "devin:remediate")
        with pytest.raises(RepositoryNotAllowed):
            await client.get_issue("someone/else", 1)
        with pytest.raises(RepositoryNotAllowed):
            await client.create_comment("someone/else", 1, "x")
    finally:
        await client.aclose()


# ------------------------------------------------------------------------- secret hygiene

SECRET_PATTERNS = (
    re.compile(r"xox[bpa]-[A-Za-z0-9-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"apk_[A-Za-z0-9]{24,}"),
    re.compile(r"BEGIN (RSA |OPENSSH )?PRIVATE KEY"),
)


def test_no_credentials_in_tracked_files_env_example_or_fixtures() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split()
    hits: list[str] = []
    for rel in tracked:
        path = ROOT / rel
        if not path.is_file() or path.suffix in {".png", ".jpg", ".gif", ".ico"}:
            continue
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue
        if rel.startswith("tests/"):
            continue
        for pattern in SECRET_PATTERNS:
            for line in content.splitlines():
                if pattern.search(line):
                    hits.append(f"{rel}: {line.strip()[:80]}")
    assert hits == []
    env_example = (ROOT / ".env.example").read_text()
    for key in ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "GITHUB_TOKEN"):
        lines = [
            line for line in env_example.splitlines() if line.lstrip("# ").startswith(f"{key}=")
        ]
        assert lines, key
        for line in lines:
            value = line.split("=", 1)[1].strip()
            assert value == "" or value.startswith("change-me"), line
    dockerignore = (ROOT / ".dockerignore").read_text()
    assert ".env" in dockerignore.split()
    assert ".env" in (ROOT / ".gitignore").read_text().split()
    assert ".env" not in tracked


@pytest.mark.asyncio
async def test_fake_slack_client_repr_has_no_state_and_live_repr_has_no_token() -> None:
    fake = FakeSlackClient(session_factory=None)  # type: ignore[arg-type]
    assert "token" not in repr(fake).lower()
    live = LiveSlackClient("xoxb-" + "k" * 40, base_url="https://slack.com/api")
    try:
        assert "xoxb" not in repr(live)
    finally:
        await live.aclose()
