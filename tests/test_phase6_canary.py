"""Phase 6: repos[] identifier default, LIVE_CANARY envelope, readiness canary checks and the
multi-architecture verifier image. Everything runs against MockTransport / static files."""

import json
import re
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from remediator.config import (
    DEFAULT_DEVIN_REPOS_FORMAT,
    Settings,
    format_devin_repo,
    repos_format_problem,
)
from remediator.devin.client import DevinTransportError
from remediator.devin.live import LiveDevinClient
from remediator.github.client import GitHubApiError, LiveGitHubClient
from remediator.probes.registry import ProbeRegistryError, load_approved_probe
from remediator.readiness import Probes, run_checks
from tests.test_live_devin_client import API_KEY, ORG, _client, _request, _session_payload
from tests.test_readiness import _by_name, _happy_handler, _live_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def _settings(**overrides: object) -> Settings:
    """Settings from code + process env only: a developer's local .env must not leak in."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type,call-arg]


VERIFIER_DOCKERFILE = REPO_ROOT / "docker" / "verifier" / "Dockerfile"
PREVIOUS_SHA = "7b6dd7597c4af49f7f0571b7b33dd247f382218b"


# --------------------------------------------------------------------------- repos[] format


def test_default_repos_format_is_the_repository_path() -> None:
    assert DEFAULT_DEVIN_REPOS_FORMAT == "{repository}"
    assert _settings().devin_repos_format == "{repository}"
    assert format_devin_repo("{repository}", "marcelaubry/superset") == "marcelaubry/superset"
    assert (
        format_devin_repo("https://github.com/{repository}", "marcelaubry/superset")
        == "https://github.com/marcelaubry/superset"
    )


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ("https://github.com/owner/repo", "literal {repository}"),
        ("{repository}/{repository}", "exactly once"),
        ("{repository} ", "whitespace"),
    ],
)
def test_bad_repos_format_is_rejected_by_settings_and_client(value: str, fragment: str) -> None:
    assert fragment in (repos_format_problem(value) or "")
    with pytest.raises(ValidationError, match="DEVIN_REPOS_FORMAT"):
        _settings(devin_repos_format=value)
    with pytest.raises(ValueError, match="repos_format"):
        LiveDevinClient(
            api_key=API_KEY, org_id=ORG, base_url="https://api.devin.ai/v3", repos_format=value
        )


@pytest.mark.asyncio
async def test_custom_repos_format_stays_configurable() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json=_session_payload())

    client = _client(handler, repos_format="https://github.com/{repository}")
    await client.create_session(_request())
    await client.aclose()
    assert seen[0]["repos"] == ["https://github.com/apache/superset"]


@pytest.mark.asyncio
async def test_repository_listing_probe_is_a_single_get_beside_v3() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {"repo_path": "acme/superset", "repo_name": "superset"},
                    {"repo_path": "acme/other"},
                    "garbage",
                ]
            },
        )

    client = _client(handler)
    paths = await client.list_repositories_probe("acme/superset")
    await client.aclose()
    assert paths == ["acme/superset", "superset", "acme/other"]
    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].url.path == f"/v3beta1/organizations/{ORG}/repositories"
    assert seen[0].url.params["only_repo_paths"] == "acme/superset"


@pytest.mark.asyncio
async def test_repository_listing_probe_refuses_non_v3_base() -> None:
    client = LiveDevinClient(
        api_key=API_KEY,
        org_id=ORG,
        base_url="https://api.devin.ai/v2",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    with pytest.raises(DevinTransportError):
        await client.list_repositories_probe("acme/superset")
    await client.aclose()


# --------------------------------------------------------------------------- settings envelope


def test_base_sha_reference_must_be_a_full_sha() -> None:
    assert _settings(github_base_sha_reference=PREVIOUS_SHA).github_base_sha_reference == (
        PREVIOUS_SHA
    )
    assert _settings(github_base_sha_reference="").github_base_sha_reference == ""
    with pytest.raises(ValidationError, match="GITHUB_BASE_SHA_REFERENCE"):
        _settings(github_base_sha_reference="7b6dd75")
    with pytest.raises(ValidationError, match="GITHUB_BASE_SHA_REFERENCE"):
        _settings(github_base_sha_reference="Z" * 40)


def test_empty_repository_allowlist_is_rejected() -> None:
    with pytest.raises(ValidationError, match="GITHUB_REPOSITORY"):
        _settings(github_repository=" , ")


def _canary_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "live_canary": True,
        "github_repository": "marcelaubry/superset",
        "github_required_label": "devin:triage",
        "max_concurrent_triage": 1,
        "max_concurrent_remediation": 1,
        "max_concurrent_probes": 1,
        "max_concurrent_remediation_per_repository": 1,
        "probe_runner_mode": "remote",
        "probe_verifier_url": "http://verifier:8100",
        "probe_verifier_shared_secret": "v" * 48,
        "cookie_secure": True,
    }
    base.update(overrides)
    return base


def test_live_canary_envelope_accepts_the_strict_fake_configuration() -> None:
    settings = _settings(**_canary_kwargs())
    assert settings.canary_violations == []
    assert settings.allowed_repositories == frozenset({"marcelaubry/superset"})


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"github_repository": "marcelaubry/superset,apache/superset"}, "exactly one"),
        ({"github_required_label": ""}, "GITHUB_REQUIRED_LABEL must name"),
        ({"github_required_label": "devin:remediate"}, "must differ"),
        ({"max_concurrent_triage": 2}, "MAX_CONCURRENT_TRIAGE must be 1"),
        ({"max_concurrent_remediation": 2}, "MAX_CONCURRENT_REMEDIATION must be 1"),
        ({"max_concurrent_probes": 2}, "MAX_CONCURRENT_PROBES must be 1"),
        ({"probe_runner_mode": "fake"}, "PROBE_RUNNER_MODE must be remote"),
    ],
)
def test_live_canary_envelope_fails_closed(overrides: dict[str, object], fragment: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(fragment)):
        _settings(**_canary_kwargs(**overrides))
    relaxed = _settings(**_canary_kwargs(live_canary=False, **overrides))
    assert any(fragment in problem for problem in relaxed.canary_violations)


def test_live_canary_requires_live_github_slack_and_secure_cookies_once_devin_is_live() -> None:
    live = _live_settings(**_canary_kwargs(live_canary=False, cookie_secure=False))
    problems = "; ".join(live.canary_violations)
    assert "COOKIE_SECURE" in problems
    assert "GITHUB_CLIENT_MODE" not in problems  # both already live
    # Devin live + GitHub fake is already refused by the base validator (fake PR/CI evidence).
    with pytest.raises(ValidationError, match="requires GITHUB_CLIENT_MODE=live"):
        _live_settings(github_client_mode="fake", github_token=None)
    with pytest.raises(ValidationError, match="LIVE_CANARY=true: COOKIE_SECURE"):
        _live_settings(**_canary_kwargs(cookie_secure=False))


# --------------------------------------------------------------------------- readiness checks


def _probes(handler: object) -> Probes:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return Probes(
        devin=transport, github=transport, slack=transport, verifier=transport, public=transport
    )


@pytest.mark.asyncio
async def test_readiness_reports_canary_envelope_label_cookie_and_sha_comparison() -> None:
    settings = _live_settings(
        github_repository="acme/superset",
        github_required_label="devin:triage",
        github_base_sha_reference=PREVIOUS_SHA,
        cookie_secure=True,
        max_concurrent_triage=1,
        max_concurrent_remediation=1,
        max_concurrent_probes=1,
        max_concurrent_remediation_per_repository=1,
        live_canary=True,
    )
    by_name = _by_name(await run_checks(settings, probes=_probes(_happy_handler)))
    assert by_name["canary.envelope"].status == "pass"
    assert "repository=acme/superset" in by_name["canary.envelope"].detail
    assert by_name["allowlist.required_label"].status == "pass"
    assert by_name["dashboard.cookie_secure"].status == "pass"
    sha = by_name["github.base_sha[acme/superset]"]
    assert sha.status == "warn"
    assert "7" * 40 in sha.detail and PREVIOUS_SHA[:12] in sha.detail
    assert "limits.canary" not in by_name


@pytest.mark.asyncio
async def test_readiness_flags_missing_intake_label_and_insecure_cookies_when_live() -> None:
    settings = _live_settings(github_base_sha_reference="7" * 40)
    by_name = _by_name(await run_checks(settings, probes=_probes(_happy_handler)))
    assert by_name["allowlist.required_label"].status == "fail"
    assert by_name["dashboard.cookie_secure"].status == "fail"
    assert by_name["canary.envelope"].status == "warn"
    assert "COOKIE_SECURE" in by_name["canary.envelope"].detail
    assert by_name["limits.canary"].status == "warn"
    assert by_name["github.base_sha[acme/superset]"].status == "pass"
    assert "equals" in by_name["github.base_sha[acme/superset]"].detail


@pytest.mark.asyncio
async def test_readiness_repository_access_fail_and_warn_paths() -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repositories"):
            return httpx.Response(200, json={"items": [{"repo_path": "acme/other"}]})
        if request.url.path.endswith("/commits/master"):
            return httpx.Response(404, json={"message": "Not Found"})
        return _happy_handler(request)

    by_name = _by_name(await run_checks(_live_settings(), probes=_probes(missing)))
    assert by_name["devin.repository_access[acme/superset]"].status == "fail"
    assert by_name["github.base_sha[acme/superset]"].status == "fail"

    def forbidden(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repositories"):
            return httpx.Response(403, json={"detail": "nope"})
        return _happy_handler(request)

    by_name = _by_name(await run_checks(_live_settings(), probes=_probes(forbidden)))
    assert by_name["devin.repository_access[acme/superset]"].status == "warn"
    assert "'acme/superset'" in by_name["devin.repository_access[acme/superset]"].detail


@pytest.mark.asyncio
async def test_readiness_in_fake_mode_still_reports_repos_format() -> None:
    by_name = _by_name(await run_checks(_settings(), probes=_probes(_happy_handler)))
    assert by_name["devin.repos_format"].status == "pass"
    assert by_name["canary.envelope"].status == "skip"
    assert "allowlist.required_label" not in by_name


@pytest.mark.asyncio
async def test_github_resolve_ref_requires_a_full_sha() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/master"):
            return httpx.Response(200, json={"sha": "7" * 40})
        return httpx.Response(200, json={"sha": "short"})

    client = LiveGitHubClient(
        "ghp_x" * 8, ["acme/superset"], transport=httpx.MockTransport(handler)
    )
    assert await client.resolve_ref("acme/superset", "master") == "7" * 40
    with pytest.raises(GitHubApiError):
        await client.resolve_ref("acme/superset", "main")
    with pytest.raises(GitHubApiError):
        await client.resolve_ref("acme/elsewhere", "master")
    await client.aclose()


# --------------------------------------------------------------------------- multi-arch image


def _dockerfile_args(text: str) -> dict[str, str]:
    return dict(re.findall(r"^ARG ([A-Z0-9_]+)=(\S+)$", text, flags=re.MULTILINE))


def test_verifier_dockerfile_selects_node_by_targetarch_with_pinned_digests() -> None:
    text = VERIFIER_DOCKERFILE.read_text()
    args = _dockerfile_args(text)
    assert re.fullmatch(r"\d+\.\d+\.\d+", args["NODE_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", args["NODE_SHA256_X64"])
    assert re.fullmatch(r"[0-9a-f]{64}", args["NODE_SHA256_ARM64"])
    assert args["NODE_SHA256_X64"] != args["NODE_SHA256_ARM64"]
    assert "SHASUMS256.txt" in text and f"v{args['NODE_VERSION']}/SHASUMS256" in text
    assert "ARG TARGETARCH" in text
    assert re.search(r"amd64\)\s+node_arch=x64;\s+node_sha256=\"\$\{NODE_SHA256_X64\}\"", text)
    assert re.search(r"arm64\)\s+node_arch=arm64;\s+node_sha256=\"\$\{NODE_SHA256_ARM64\}\"", text)
    assert re.search(r"\*\)\s+echo \"unsupported architecture", text) and "exit 1" in text
    assert "sha256sum -c -" in text
    assert "linux-${node_arch}.tar.xz" in text


@pytest.mark.parametrize(
    "path",
    sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in [REPO_ROOT / "Dockerfile", *REPO_ROOT.glob("docker/*/Dockerfile")]
    ),
)
def test_no_dockerfile_hardcodes_a_single_cpu_architecture(path: str) -> None:
    text = (REPO_ROOT / path).read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"linux-x64\b", code), f"{path}: hardcoded Node x64 artifact"
    assert not re.search(r"\b(x86_64|aarch64)\b", code), f"{path}: hardcoded CPU architecture"
    assert not re.search(r"--platform=\S+", code), f"{path}: FROM --platform pin"
    for hit in re.finditer(r"\b(amd64|arm64)\b", code):
        line = code[code.rfind("\n", 0, hit.start()) + 1 : code.find("\n", hit.end())]
        assert "node_arch=" in line or "unsupported architecture" in line, f"{path}: {line}"


def test_compose_does_not_force_a_platform() -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "platform:" not in compose
    assert "DOCKER_DEFAULT_PLATFORM" not in compose


# --------------------------------------------------------------------------- canary artefacts


async def test_fork_smoke_probe_is_registered_at_the_verified_base() -> None:
    probe = await load_approved_probe(
        REPO_ROOT / "probes", "marcelaubry/superset", 0, allow_smoke=True
    )
    upstream = await load_approved_probe(
        REPO_ROOT / "probes", "apache/superset", 0, allow_smoke=True
    )
    assert probe.base_sha == PREVIOUS_SHA
    assert probe.script_hash == upstream.script_hash
    assert (probe.expected_base_exit_code, probe.expected_head_exit_code) == (0, 1)
    with pytest.raises(ProbeRegistryError):
        await load_approved_probe(REPO_ROOT / "probes", "marcelaubry/superset", 0)


def test_env_example_documents_the_canary_without_enabling_it() -> None:
    text = (REPO_ROOT / ".env.example").read_text()
    active = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert active["LIVE_CANARY"] == "false"
    assert active["DEVIN_REPOS_FORMAT"] == "{repository}"
    assert active["DEVIN_CLIENT_MODE"] == "fake"
    assert "UNVERIFIED" not in text
    for canary_line in (
        "# GITHUB_REPOSITORY=marcelaubry/superset",
        "# GITHUB_REQUIRED_LABEL=devin:triage",
        "# MAX_CONCURRENT_TRIAGE=1",
        "# PROBE_RUNNER_MODE=remote",
        "# COOKIE_SECURE=true",
    ):
        assert canary_line in text
    assert "GITHUB_BASE_SHA_REFERENCE=" in text and PREVIOUS_SHA not in text


def test_runbook_covers_activation_order_rollback_and_checklist() -> None:
    text = (REPO_ROOT / "docs" / "canary-runbook.md").read_text()
    for needle in (
        "LIVE_CANARY=true",
        "6. **Live Devin last.**",
        "13. **Stop at `CI_PASSED`.**",
        "Never blindly retry an uncertain create",
        "confirm_no_session=true",
        "Current master SHA",
        "triage_result_hash",
        "exact head SHA",
        "ACUs consumed",
        "PR not merged by the service",
    ):
        assert needle in text, needle
    assert "ghp_" not in text and "xoxb-" not in text
