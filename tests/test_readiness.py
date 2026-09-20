"""Readiness command: every default check is read-only and every line is redacted."""

import json
from collections.abc import Callable

import httpx
import pytest

from remediator import readiness
from remediator.config import Settings
from remediator.readiness import CheckResult, Probes, render, run_checks
from remediator.verifier.protocol import VERIFIER_PROTOCOL_VERSION, Capabilities

DEVIN_KEY = "apk_readiness_secret_key_never_printed_0001"
GH_TOKEN = "ghp_readiness_secret_token_never_printed_01"
SLACK_TOKEN = "xoxb-readiness-secret-token-never-printed"
SIGNING = "slack-signing-secret-for-readiness-tests"
VERIFIER_SECRET = "v" * 48


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def transport(self, handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
        def wrapped(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        return httpx.MockTransport(wrapped)


def _live_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://remediator:remediator@localhost:1/none",
        "github_webhook_secret": "webhook-secret-that-is-long-enough",
        "operator_token": "operator-token-that-is-long-enough",
        "devin_client_mode": "live",
        "devin_api_key": DEVIN_KEY,
        "devin_org_id": "org_42",
        "github_client_mode": "live",
        "github_token": GH_TOKEN,
        "github_repository": "acme/superset",
        "slack_client_mode": "live",
        "slack_bot_token": SLACK_TOKEN,
        "slack_signing_secret": SIGNING,
        "slack_channel_id": "C0123456789",
        "slack_approver_user_ids": "U0123456789,U0000000BAD",
        "probe_runner_mode": "remote",
        "probe_verifier_url": "http://verifier:8100",
        "probe_verifier_shared_secret": VERIFIER_SECRET,
        "public_base_url": "https://remediator.example.com",
        "devin_triage_timeout_seconds": 1800,
        "devin_remediation_timeout_seconds": 5400,
        "devin_poll_interval_seconds": 15,
        "ci_poll_interval_seconds": 60,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _capabilities() -> dict[str, object]:
    return {
        "protocol_version": VERIFIER_PROTOCOL_VERSION,
        "isolation": {
            "uid": 65534,
            "root_writable": False,
            "credential_exposure": [],
            "no_new_privs": True,
            "effective_capabilities": "0000000000000000",
            "pids_limit": 512,
            "memory_limit_bytes": 3_221_225_472,
            "cpu_quota": "200000 100000",
            "docker_socket_present": False,
            "direct_egress": False,
            "egress_proxy_configured": True,
        },
        "tools": {"git": True, "bash": True, "python3": True, "node": True, "npm": True},
        "tool_versions": {"git": "2.43", "bash": "5.2", "node": "v24.16.0", "npm": "11.13.0"},
        "repository_allowlist": ["acme/superset"],
        "registry_present": True,
        "max_concurrent": 1,
        "max_timeout_seconds": 3600,
        "cache_enabled": True,
        "in_flight": 0,
        "execution": "runner",
    }


def _happy_handler(request: httpx.Request) -> httpx.Response:
    path, host = request.url.path, request.url.host
    if host == "api.devin.ai":
        if path.endswith("/self"):
            return httpx.Response(200, json={"email": "svc@acme.test", "org_id": "org_42"})
        if "/sessions" in path:
            return httpx.Response(200, json={"items": [{"session_id": "x"}]})
    if host == "api.github.com":
        if path == "/repos/acme/superset":
            return httpx.Response(
                200,
                json={
                    "full_name": "acme/superset",
                    "default_branch": "master",
                    "permissions": {"pull": True, "push": True},
                },
            )
        if path.startswith("/repos/acme/superset/labels/"):
            return httpx.Response(200, json={"name": path.rsplit("/", 1)[1]})
    if host == "slack.com":
        if path.endswith("auth.test"):
            return httpx.Response(200, json={"ok": True, "user": "remediator", "team": "acme"})
        if path.endswith("conversations.info"):
            return httpx.Response(
                200, json={"ok": True, "channel": {"name": "approvals", "is_member": True}}
            )
        if path.endswith("users.info"):
            body = json.loads(request.content)
            if body["user"] == "U0000000BAD":
                return httpx.Response(200, json={"ok": False, "error": "user_not_found"})
            return httpx.Response(200, json={"ok": True, "user": {"id": body["user"]}})
        if path.endswith("chat.postMessage"):
            return httpx.Response(200, json={"ok": True, "channel": "C0123456789", "ts": "1.2"})
    if host == "verifier":
        assert "X-Verifier-Signature" in request.headers
        return httpx.Response(200, json=_capabilities())
    if host == "remediator.example.com" and path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    return httpx.Response(500, json={"unexpected": str(request.url)})


def _by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {r.name: r for r in results}


@pytest.mark.asyncio
async def test_default_run_is_read_only_and_redacted() -> None:
    recorder = Recorder()
    transport = recorder.transport(_happy_handler)
    probes = Probes(
        devin=transport, github=transport, slack=transport, verifier=transport, public=transport
    )
    results = await run_checks(_live_settings(), probes=probes)
    by_name = _by_name(results)

    # Read-only by construction: only GET requests except Slack's POST-only Web API reads.
    for request in recorder.requests:
        if request.url.host == "slack.com":
            assert request.url.path.rsplit("/", 1)[1] in {
                "auth.test",
                "conversations.info",
                "users.info",
            }
        else:
            assert request.method == "GET", request
    assert not any(r.mutating for r in results)

    assert by_name["devin.identity"].status == "pass"
    assert by_name["devin.list_sessions"].status == "pass"
    assert by_name["github.repository[acme/superset]"].status == "pass"
    assert by_name["github.default_branch[acme/superset]"].status == "pass"
    assert by_name["github.label[acme/superset:devin:remediate]"].status == "pass"
    assert by_name["slack.identity"].status == "pass"
    assert by_name["slack.channel"].status == "pass"
    assert by_name["slack.approver[U0123456789]"].status == "pass"
    assert by_name["slack.approver[U0000000BAD]"].status == "fail"
    assert by_name["verifier.health"].status == "pass"
    assert by_name["verifier.node"].status == "pass"
    assert by_name["verifier.isolation"].status == "pass"
    assert by_name["verifier.allowlist"].status == "pass"
    assert by_name["webhook.base_url"].status == "pass"
    assert by_name["webhook.health"].status == "pass"
    assert by_name["database.connect"].status == "fail"  # port 1: unreachable by design

    rendered = render(results, as_json=False) + render(results, as_json=True)
    for secret in (DEVIN_KEY, GH_TOKEN, SLACK_TOKEN, SIGNING, VERIFIER_SECRET):
        assert secret not in rendered
    assert "NOT READY" in rendered


@pytest.mark.asyncio
async def test_mutating_check_requires_explicit_flag() -> None:
    recorder = Recorder()
    transport = recorder.transport(_happy_handler)
    probes = Probes(slack=transport, devin=transport, github=transport, verifier=transport)
    settings = _live_settings(
        probe_runner_mode="fake", devin_client_mode="fake", devin_api_key=None
    )
    settings = _live_settings(
        probe_runner_mode="fake",
        probe_verifier_url=None,
        probe_verifier_shared_secret=None,
        devin_client_mode="fake",
        devin_api_key=None,
        devin_org_id=None,
        github_client_mode="fake",
        github_token=None,
        public_base_url="",
    )
    await run_checks(settings, probes=probes)
    assert not any(r.url.path.endswith("chat.postMessage") for r in recorder.requests)

    results = await run_checks(
        settings, allow_mutations=True, confirm_channel="C0123456789", probes=probes
    )
    posts = [r for r in recorder.requests if r.url.path.endswith("chat.postMessage")]
    assert len(posts) == 1
    assert _by_name(results)["slack.test_message"].mutating is True


@pytest.mark.asyncio
async def test_provider_failures_are_reported_not_raised_and_stay_redacted() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.host == "verifier":
            return httpx.Response(401)
        if request.url.host == "api.github.com" and request.url.path == "/repos/acme/superset":
            return httpx.Response(
                200, json={"full_name": "acme/superset-renamed", "default_branch": "main"}
            )
        return httpx.Response(403, json={"ok": False, "error": "invalid_auth", "token": GH_TOKEN})

    transport = httpx.MockTransport(failing)
    probes = Probes(
        devin=transport, github=transport, slack=transport, verifier=transport, public=transport
    )
    results = await run_checks(_live_settings(), probes=probes)
    by_name = _by_name(results)
    assert by_name["devin.identity"].status == "fail"
    assert by_name["github.repository[acme/superset]"].status == "fail"
    assert "renamed" in by_name["github.repository[acme/superset]"].detail
    assert by_name["github.default_branch[acme/superset]"].status == "fail"
    assert by_name["slack.identity"].status == "fail"
    assert by_name["slack.identity"].detail == "invalid_auth"
    assert by_name["verifier.health"].status == "fail"
    assert "signature" in by_name["verifier.health"].detail
    assert GH_TOKEN not in render(results, as_json=True)


@pytest.mark.asyncio
async def test_verifier_isolation_gaps_and_allowlist_mismatch_fail() -> None:
    caps = _capabilities()
    caps["isolation"] = {**caps["isolation"], "no_new_privs": False, "pids_limit": None}  # type: ignore[dict-item]
    caps["repository_allowlist"] = ["other/repo"]
    caps["tool_versions"] = {**caps["tool_versions"], "node": "v20.1.0"}  # type: ignore[dict-item]

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=caps))
    # Live mode always requires isolation, so the detailed report is only reachable when the
    # runner accepts the verifier; exercise the report directly for the gap case.
    results = readiness._verifier_results(_live_settings(), Capabilities.model_validate(caps))  # noqa: SLF001
    by_name = _by_name(results)
    assert by_name["verifier.isolation"].status == "fail"
    assert by_name["verifier.allowlist"].status == "fail"
    assert by_name["verifier.node"].status == "warn"
    assert by_name["verifier.key_separation"].status == "pass"
    assert by_name["verifier.egress"].status == "pass"

    strict = await readiness.check_verifier(_live_settings(), Probes(verifier=transport))
    assert strict[0].name == "verifier.health" and strict[0].status == "fail"
    assert "isolation not enforced" in strict[0].detail


@pytest.mark.asyncio
async def test_verifier_key_colocation_and_open_egress_fail_readiness() -> None:
    colocated = _capabilities()
    colocated["execution"] = "in-process"
    results = readiness._verifier_results(_live_settings(), Capabilities.model_validate(colocated))  # noqa: SLF001
    key = _by_name(results)["verifier.key_separation"]
    assert key.status == "fail" and "HMAC key" in key.detail
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=colocated))
    strict = await readiness.check_verifier(_live_settings(), Probes(verifier=transport))
    assert strict[0].status == "fail" and "HMAC key" in strict[0].detail

    for direct, phrase in ((True, "reach the internet directly"), (None, "not verified")):
        caps = _capabilities()
        caps["isolation"] = {**caps["isolation"], "direct_egress": direct}  # type: ignore[dict-item]
        results = readiness._verifier_results(_live_settings(), Capabilities.model_validate(caps))  # noqa: SLF001
        egress = _by_name(results)["verifier.egress"]
        assert egress.status == "fail" and phrase in egress.detail
        transport = httpx.MockTransport(lambda request, caps=caps: httpx.Response(200, json=caps))
        strict = await readiness.check_verifier(_live_settings(), Probes(verifier=transport))
        assert strict[0].status == "fail" and "egress" in strict[0].detail


@pytest.mark.asyncio
async def test_offline_checks_flag_placeholders_loopback_and_bad_limits() -> None:
    settings = Settings(
        github_webhook_secret="change-me",
        operator_token="short",
        github_client_mode="fake",
        slack_client_mode="fake",
        devin_client_mode="fake",
        public_base_url="http://localhost:8000",
        max_concurrent_remediation=1,
        max_concurrent_remediation_per_repository=3,
    )
    probes = Probes()
    config = _by_name(await readiness.check_configuration(settings, probes))
    assert config["config.secret.GITHUB_WEBHOOK_SECRET"].status == "warn"
    assert config["config.secret.OPERATOR_TOKEN"].status == "warn"
    assert config["config.secret.DEVIN_API_KEY"].status == "skip"

    limits = _by_name(await readiness.check_limits(settings, probes))
    assert limits["limits.per_repository"].status == "warn"
    assert limits["limits.concurrency"].status == "pass"

    url = await readiness.check_public_url(
        settings, Probes(public=httpx.MockTransport(lambda request: httpx.Response(200)))
    )
    assert _by_name(url)["webhook.base_url"].status == "pass"  # loopback fine in fake mode

    live_url = await readiness.check_public_url(
        Settings(
            github_client_mode="live",
            github_token=GH_TOKEN,
            github_webhook_secret="webhook-secret-that-is-long-enough",
            operator_token="operator-token-that-is-long-enough",
            public_base_url="http://localhost:8000",
        ),
        probes,
    )
    assert _by_name(live_url)["webhook.base_url"].status == "fail"

    allow = await readiness.check_repository_allowlist(
        Settings(github_repository="not-a-repo"), probes
    )
    assert allow[0].status == "fail"


def test_main_reports_settings_errors_without_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEVIN_CLIENT_MODE", "live")
    monkeypatch.setenv("DEVIN_API_KEY", "change-me")
    monkeypatch.setenv("DEVIN_ORG_ID", "org")
    monkeypatch.setenv("DEVIN_TRIAGE_TIMEOUT_SECONDS", "1800")
    monkeypatch.setenv("DEVIN_POLL_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "hook-secret-that-must-not-print")
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-token-that-must-not-print")
    assert readiness.main([]) == 2
    out = capsys.readouterr().out
    assert "config.load" in out and "DEVIN_CLIENT_MODE=live" in out
    assert "must-not-print" not in out and "input_value" not in out


@pytest.mark.asyncio
async def test_mutations_need_the_channel_confirmation_too() -> None:
    recorder = Recorder()
    transport = recorder.transport(_happy_handler)
    probes = Probes(slack=transport, devin=transport, github=transport, verifier=transport)
    settings = _live_settings(
        probe_runner_mode="fake",
        probe_verifier_url=None,
        probe_verifier_shared_secret=None,
        devin_client_mode="fake",
        devin_api_key=None,
        devin_org_id=None,
        github_client_mode="fake",
        github_token=None,
        public_base_url="",
    )
    for confirm in (None, "C0000000000"):
        results = await run_checks(
            settings, allow_mutations=True, confirm_channel=confirm, probes=probes
        )
        gate = _by_name(results)["mutations.confirmation"]
        assert gate.status == "fail" and gate.mutating is True
        assert "slack.test_message" not in _by_name(results)
    assert not any(r.url.path.endswith("chat.postMessage") for r in recorder.requests)

    results = await run_checks(
        settings, allow_mutations=True, confirm_channel="C0123456789", probes=probes
    )
    assert _by_name(results)["slack.test_message"].status == "pass"
    assert "mutations.confirmation" not in _by_name(results)


def _smoke_handler(
    exit_code: int | None, infra: str | None = None
) -> Callable[..., httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capabilities":
            caps = _capabilities()
            caps["repository_allowlist"] = ["apache/superset"]
            return httpx.Response(200, json=caps)
        assert request.url.path == "/probe"
        body = json.loads(request.content)
        assert body["issue_number"] == 0 and body["target"] == "BASE"
        assert "script_content" not in body
        from remediator.models import ProbeTarget
        from remediator.probes.runner import ProbeRunSpec, command_identity

        identity = command_identity(
            ProbeRunSpec(
                repository=body["repository"],
                issue_number=0,
                commit_sha=body["commit_sha"],
                target=ProbeTarget.BASE,
                probe_identifier=body["probe_identifier"],
                script_hash=body["script_hash"],
                script_content="",
                timeout_seconds=body["timeout_seconds"],
                max_output_bytes=body["max_output_bytes"],
                required_tools=(),
            )
        )
        return httpx.Response(
            200,
            json={
                "request_id": body["request_id"],
                "repository": body["repository"],
                "commit_sha": body["commit_sha"],
                "probe_identifier": body["probe_identifier"],
                "script_hash": body["script_hash"],
                "runner_mode": "local",
                "command_identity": identity,
                "exit_code": exit_code,
                "stdout": "Tests: 3 passed",
                "stderr": "" if exit_code == 0 else f"jest failed token={DEVIN_KEY}",
                "output_truncated": False,
                "timed_out": False,
                "duration_ms": 61_000,
                "infrastructure_error": infra,
                "failure_stage": "setup" if infra else None,
                "tool_versions": {"node": "v24.16.0", "npm": "11.13.0"},
            },
        )

    return handler


@pytest.mark.asyncio
async def test_verifier_smoke_is_opt_in_and_classifies_results() -> None:
    settings = _live_settings(probe_root="probes", probe_smoke_probe="apache/superset#0")
    recorder = Recorder()
    probes = Probes(verifier=recorder.transport(_smoke_handler(0)))

    results = await readiness.check_verifier_smoke(settings, probes)
    assert [r.status for r in results] == ["pass"]
    assert (
        "apache/superset#0@sha256:" in results[0].detail and "@ 4511c1381930" in results[0].detail
    )
    assert any(r.url.path == "/probe" for r in recorder.requests)

    failed = await readiness.check_verifier_smoke(
        settings, Probes(verifier=httpx.MockTransport(_smoke_handler(1)))
    )
    assert failed[0].status == "fail" and "exit=1 expected=0" in failed[0].detail

    infra = await readiness.check_verifier_smoke(
        settings, Probes(verifier=httpx.MockTransport(_smoke_handler(None, "npm ci failed")))
    )
    assert infra[0].status == "fail" and infra[0].detail.startswith("infrastructure: npm ci")

    # Not part of the default run; opt-in adds it and redaction still applies to its output.
    default = await run_checks(
        settings, probes=Probes(verifier=httpx.MockTransport(_happy_handler))
    )
    assert "verifier.smoke" not in _by_name(default)
    opted = await run_checks(
        settings,
        verifier_smoke=True,
        probes=Probes(verifier=httpx.MockTransport(_smoke_handler(1))),
    )
    smoke = _by_name(opted)["verifier.smoke"]
    assert smoke.status == "fail" and DEVIN_KEY not in smoke.detail


@pytest.mark.asyncio
async def test_verifier_smoke_skips_without_a_remote_verifier_and_rejects_bad_probe() -> None:
    fake = _live_settings(
        probe_runner_mode="fake",
        probe_verifier_url=None,
        probe_verifier_shared_secret=None,
        devin_client_mode="fake",
        devin_api_key=None,
        devin_org_id=None,
    )
    assert (await readiness.check_verifier_smoke(fake, Probes()))[0].status == "skip"
    bad = _live_settings(probe_smoke_probe="apache/superset#999999")
    result = (await readiness.check_verifier_smoke(bad, Probes()))[0]
    assert result.status == "fail" and "not loadable" in result.detail
