"""Live-readiness report: `python -m remediator.readiness [--json] [--verifier-smoke]
[--allow-mutations --confirm-channel <SLACK_CHANNEL_ID>]`.

Every default check is read-only: no Devin session is created, no GitHub or Slack resource
is written, no database row is modified. The only mutating check (a Slack test message)
runs solely behind `--allow-mutations` *and* `--confirm-channel` naming the exact channel
it will post to. `--verifier-smoke` executes the registry smoke probe (a real Superset
Jest test at a pinned SHA) inside the verifier runner: it downloads dependencies through
the egress proxy but mutates nothing and spends no ACUs. All output passes through the
settings redactor so tokens and signing secrets can never appear, even inside provider
error messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from pydantic import SecretStr, ValidationError
from sqlalchemy import text

from .adapters import SettingsRedactingFilter
from .config import (
    CANARY_CONCURRENCY_LIMIT,
    DEFAULT_DEVIN_REPOS_FORMAT,
    MIN_LIVE_SECRET_LENGTH,
    PLACEHOLDER_SECRET,
    Settings,
    format_devin_repo,
    repos_format_problem,
    unsafe_service_url,
)
from .db import build_engine
from .devin.live import LiveDevinClient
from .github.client import GitHubApiError, LiveGitHubClient
from .models import ProbeTarget
from .probes.registry import (
    ProbeRegistryError,
    load_approved_probe,
    manifest_cache_inputs,
    manifest_setup_steps,
    manifest_setup_timeout,
)
from .probes.remote import RemoteProbeRunner
from .probes.runner import ProbeRunSpec
from .slack.client import LiveSlackClient, SlackApiError
from .verifier.protocol import Capabilities

Status = Literal["pass", "fail", "warn", "skip"]
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SLACK_USER_RE = re.compile(r"^[UW][A-Z0-9]{8,}$")
SLACK_CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]{8,}$")
REQUIRED_VERIFIER_TOOLS = ("git", "bash", "node", "npm")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    mutating: bool = False


@dataclass(frozen=True)
class Probes:
    """Optional httpx transports per provider so tests never touch the network."""

    devin: httpx.AsyncBaseTransport | None = None
    github: httpx.AsyncBaseTransport | None = None
    slack: httpx.AsyncBaseTransport | None = None
    verifier: httpx.AsyncBaseTransport | None = None
    public: httpx.AsyncBaseTransport | None = None


CheckFn = Callable[[Settings, Probes], Awaitable[list[CheckResult]]]


def _mask(value: SecretStr | str | None) -> str:
    raw = value.get_secret_value() if isinstance(value, SecretStr) else (value or "")
    return f"set ({len(raw)} chars)" if raw else "unset"


def _secret_ok(value: SecretStr | str | None) -> bool:
    raw = value.get_secret_value() if isinstance(value, SecretStr) else (value or "")
    return bool(raw.strip()) and raw != PLACEHOLDER_SECRET and len(raw) >= MIN_LIVE_SECRET_LENGTH


# --------------------------------------------------------------------------- offline checks


async def check_configuration(settings: Settings, probes: Probes) -> list[CheckResult]:
    results: list[CheckResult] = []
    modes = (
        f"devin={settings.devin_client_mode} github={settings.github_client_mode} "
        f"slack={settings.slack_client_mode} probes={settings.probe_runner_mode} "
        f"metrics_mode={settings.metrics_mode}"
    )
    results.append(CheckResult("config.modes", "pass", modes))

    required_always: list[tuple[str, SecretStr | str | None]] = [
        ("GITHUB_WEBHOOK_SECRET", settings.github_webhook_secret),
        ("OPERATOR_TOKEN", settings.operator_token),
    ]
    conditional: list[tuple[str, SecretStr | str | None, bool]] = [
        ("DEVIN_API_KEY", settings.devin_api_key, settings.live_mode),
        ("GITHUB_TOKEN", settings.github_token, settings.github_live),
        ("SLACK_BOT_TOKEN", settings.slack_bot_token, settings.slack_live),
        ("SLACK_SIGNING_SECRET", settings.slack_signing_secret, settings.slack_live),
        (
            "PROBE_VERIFIER_SHARED_SECRET",
            settings.probe_verifier_shared_secret,
            settings.probe_runner_mode == "remote",
        ),
    ]
    any_live = settings.live_mode or settings.github_live or settings.slack_live
    for name, value in required_always:
        if _secret_ok(value):
            results.append(CheckResult(f"config.secret.{name}", "pass", _mask(value)))
        else:
            results.append(
                CheckResult(
                    f"config.secret.{name}",
                    "fail" if any_live else "warn",
                    f"{_mask(value)}; placeholder or shorter than {MIN_LIVE_SECRET_LENGTH} chars",
                )
            )
    for name, value, needed in conditional:
        if not needed:
            results.append(
                CheckResult(f"config.secret.{name}", "skip", "not required in this mode")
            )
        elif _secret_ok(value):
            results.append(CheckResult(f"config.secret.{name}", "pass", _mask(value)))
        else:
            results.append(
                CheckResult(
                    f"config.secret.{name}", "fail", f"{_mask(value)}; required but unusable"
                )
            )
    if settings.probe_runner_mode == "remote":
        secret = settings.probe_verifier_secret_value or ""
        if len(secret) < 32:
            results.append(
                CheckResult(
                    "config.verifier_secret_length",
                    "fail",
                    "PROBE_VERIFIER_SHARED_SECRET must hold at least 32 characters",
                )
            )
    if settings.live_mode and not settings.devin_org_id:
        results.append(CheckResult("config.devin_org_id", "fail", "DEVIN_ORG_ID is unset"))
    if settings.devin_acu_reporting_enabled and not settings.devin_org_id:
        results.append(
            CheckResult(
                "config.acu_reporting", "fail", "DEVIN_ACU_REPORTING_ENABLED needs DEVIN_ORG_ID"
            )
        )
    env_file = Path(".env")
    if env_file.exists() and (env_file.stat().st_mode & 0o077):
        results.append(
            CheckResult("config.env_permissions", "warn", ".env is readable by other users")
        )
    return results


async def check_repository_allowlist(settings: Settings, probes: Probes) -> list[CheckResult]:
    repos = sorted(settings.allowed_repositories)
    if not repos:
        return [CheckResult("allowlist.repositories", "fail", "GITHUB_REPOSITORY is empty")]
    bad = [r for r in repos if not REPOSITORY_RE.match(r)]
    if bad:
        return [CheckResult("allowlist.repositories", "fail", f"not owner/name: {bad}")]
    results = [CheckResult("allowlist.repositories", "pass", ", ".join(repos))]
    label = settings.github_required_label.strip()
    if label:
        results.append(
            CheckResult(
                "allowlist.required_label",
                "pass",
                f"intake label {label!r}; remediation label {settings.github_remediation_label!r}",
            )
        )
    elif settings.live_mode:
        results.append(
            CheckResult(
                "allowlist.required_label",
                "fail",
                "GITHUB_REQUIRED_LABEL is empty while Devin is live: every opened issue would "
                "spend ACUs on triage",
            )
        )
    elif settings.github_live:
        results.append(
            CheckResult(
                "allowlist.required_label",
                "warn",
                "GITHUB_REQUIRED_LABEL is empty: every opened issue is eligible for triage",
            )
        )
    return results


async def check_canary(settings: Settings, probes: Probes) -> list[CheckResult]:
    """The controlled-canary envelope (`LIVE_CANARY=true`): reported here so an operator sees
    every deviation at once; `Settings` refuses to start on the same list."""
    violations = settings.canary_violations
    if not settings.live_canary:
        if settings.live_mode:
            return [
                CheckResult(
                    "canary.envelope",
                    "warn",
                    "LIVE_CANARY is off while Devin is live"
                    + (f"; would fail: {'; '.join(violations)}" if violations else ""),
                )
            ]
        return [CheckResult("canary.envelope", "skip", "LIVE_CANARY=false")]
    if violations:
        return [CheckResult("canary.envelope", "fail", "; ".join(violations))]
    (repository,) = settings.allowed_repositories
    return [
        CheckResult(
            "canary.envelope",
            "pass",
            f"repository={repository} intake={settings.github_required_label} "
            f"remediation={settings.github_remediation_label} "
            f"concurrency={CANARY_CONCURRENCY_LIMIT} modes="
            f"devin:{settings.devin_client_mode}/github:{settings.github_client_mode}/"
            f"slack:{settings.slack_client_mode}/probes:{settings.probe_runner_mode}",
        )
    ]


async def check_limits(settings: Settings, probes: Probes) -> list[CheckResult]:
    results: list[CheckResult] = []
    limits = {
        "MAX_CONCURRENT_TRIAGE": settings.max_concurrent_triage,
        "MAX_CONCURRENT_REMEDIATION": settings.max_concurrent_remediation,
        "MAX_CONCURRENT_PROBES": settings.max_concurrent_probes,
        "MAX_CONCURRENT_REMEDIATION_PER_REPOSITORY": (
            settings.max_concurrent_remediation_per_repository
        ),
    }
    bad = {k: v for k, v in limits.items() if v < 1}
    if bad:
        results.append(CheckResult("limits.concurrency", "fail", f"must be >= 1: {bad}"))
    else:
        results.append(
            CheckResult(
                "limits.concurrency", "pass", " ".join(f"{k}={v}" for k, v in limits.items())
            )
        )
    if settings.max_concurrent_remediation_per_repository > settings.max_concurrent_remediation:
        results.append(
            CheckResult(
                "limits.per_repository",
                "warn",
                "per-repository remediation limit exceeds the global limit; global wins",
            )
        )
    acu = (
        f"triage={settings.devin_triage_max_acu} remediation={settings.devin_remediation_max_acu}"
        f" reporting={'on' if settings.devin_acu_reporting_enabled else 'off'}"
    )
    status: Status = "pass"
    if settings.live_mode and settings.devin_remediation_max_acu > 50:
        status = "warn"
        acu += "; remediation ACU cap above 50"
    results.append(CheckResult("limits.acu", status, acu))
    if settings.live_mode and any(v > CANARY_CONCURRENCY_LIMIT for v in limits.values()):
        results.append(
            CheckResult(
                "limits.canary",
                "warn",
                f"a first live run should keep every limit at {CANARY_CONCURRENCY_LIMIT}",
            )
        )
    return results


async def check_public_url(settings: Settings, probes: Probes) -> list[CheckResult]:
    url = settings.public_base_url.strip() or settings.dashboard_base_url.strip()
    source = "PUBLIC_BASE_URL" if settings.public_base_url.strip() else "DASHBOARD_BASE_URL"
    if not url:
        return [CheckResult("webhook.base_url", "fail", "no public base URL configured")]
    problem = unsafe_service_url(url)
    if problem:
        return [CheckResult("webhook.base_url", "fail", f"{source} {problem}")]
    parsed = urlsplit(url)
    any_live = settings.github_live or settings.slack_live
    if any_live and parsed.scheme != "https":
        return [
            CheckResult("webhook.base_url", "fail", f"{source} must be https for live providers")
        ]
    if any_live and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return [CheckResult("webhook.base_url", "fail", f"{source} points at loopback")]
    results = [CheckResult("webhook.base_url", "pass", f"{source}={url}")]
    if any_live and not settings.cookie_secure:
        results.append(
            CheckResult(
                "dashboard.cookie_secure",
                "fail",
                "COOKIE_SECURE=false with live providers: operator cookies would travel "
                "without the Secure flag",
            )
        )
    elif any_live:
        results.append(CheckResult("dashboard.cookie_secure", "pass", "COOKIE_SECURE=true"))
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=probes.public) as client:
            response = await client.get(f"{url.rstrip('/')}/health")
    except httpx.HTTPError as exc:
        results.append(
            CheckResult("webhook.health", "warn", f"GET /health failed: {exc.__class__.__name__}")
        )
    else:
        ok = response.status_code == 200
        results.append(
            CheckResult("webhook.health", "pass" if ok else "fail", f"HTTP {response.status_code}")
        )
    return results


# --------------------------------------------------------------------------- database


def _alembic_head() -> str | None:
    root = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    return heads[0] if len(heads) == 1 else None


async def check_database(settings: Settings, probes: Probes) -> list[CheckResult]:
    engine = build_engine(settings)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            has_table = await conn.scalar(text("SELECT to_regclass('alembic_version') IS NOT NULL"))
            current = []
            if has_table:
                rows = await conn.execute(text("SELECT version_num FROM alembic_version"))
                current = [str(row[0]) for row in rows]
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        first_line = str(exc).splitlines()[0] if str(exc) else ""
        return [CheckResult("database.connect", "fail", f"{exc.__class__.__name__}: {first_line}")]
    finally:
        await engine.dispose()
    head = _alembic_head()
    results = [CheckResult("database.connect", "pass", "SELECT 1 ok")]
    if head is None:
        results.append(CheckResult("database.migrations", "fail", "alembic has multiple heads"))
    elif current == [head]:
        results.append(CheckResult("database.migrations", "pass", f"at head {head}"))
    else:
        results.append(
            CheckResult(
                "database.migrations", "fail", f"database at {current or 'none'}, head {head}"
            )
        )
    return results


# --------------------------------------------------------------------------- providers


async def check_devin(settings: Settings, probes: Probes) -> list[CheckResult]:
    if not settings.live_mode:
        return [
            CheckResult("devin.identity", "skip", "DEVIN_CLIENT_MODE=fake"),
            _repos_format_result(settings),
        ]
    if settings.devin_api_key is None or not settings.devin_org_id:
        return [CheckResult("devin.identity", "fail", "DEVIN_API_KEY / DEVIN_ORG_ID missing")]
    problem = unsafe_service_url(settings.devin_api_base_url)
    if problem:
        return [CheckResult("devin.api_base_url", "fail", f"DEVIN_API_BASE_URL {problem}")]
    client = LiveDevinClient(
        api_key=settings.devin_api_key.get_secret_value(),
        org_id=settings.devin_org_id,
        base_url=settings.devin_api_base_url,
        request_timeout_seconds=settings.devin_http_timeout_seconds,
        max_retries=0,
        transport=probes.devin,
    )
    results: list[CheckResult] = []
    try:
        try:
            me = await client.whoami()
        except Exception as exc:  # noqa: BLE001
            return [CheckResult("devin.identity", "fail", f"GET /self: {exc.__class__.__name__}")]
        org = me.get("org_id") or me.get("organization_id") or me.get("organization")
        ident = me.get("email") or me.get("name") or me.get("id") or "service user"
        if org and str(org) != settings.devin_org_id:
            results.append(
                CheckResult(
                    "devin.identity", "fail", f"{ident} belongs to org {org}, not configured"
                )
            )
        else:
            results.append(
                CheckResult("devin.identity", "pass", f"{ident} org={settings.devin_org_id}")
            )
        try:
            count = await client.list_sessions_probe()
        except Exception as exc:  # noqa: BLE001
            results.append(
                CheckResult(
                    "devin.list_sessions", "fail", f"read-only list: {exc.__class__.__name__}"
                )
            )
        else:
            results.append(CheckResult("devin.list_sessions", "pass", f"listed {count} session(s)"))
        results.extend(await _devin_repository_results(settings, client))
        if settings.devin_acu_reporting_enabled:
            results.append(
                CheckResult(
                    "devin.acu_reporting",
                    "warn",
                    "enabled; per-session consumption is fetched lazily and shows Unavailable "
                    "when the plan or service user lacks ViewOrgConsumption",
                )
            )
    finally:
        await client.aclose()
    return results


def _repos_format_result(settings: Settings) -> CheckResult:
    fmt = settings.devin_repos_format
    if problem := repos_format_problem(fmt):
        return CheckResult("devin.repos_format", "fail", f"DEVIN_REPOS_FORMAT {problem}")
    if fmt != DEFAULT_DEVIN_REPOS_FORMAT:
        return CheckResult(
            "devin.repos_format",
            "warn",
            f"{fmt!r} differs from the owner/repo path default {DEFAULT_DEVIN_REPOS_FORMAT!r}",
        )
    return CheckResult("devin.repos_format", "pass", f"{fmt!r} (owner/repo path)")


def _repo_path_matches(candidate: str, repository: str) -> bool:
    tail = candidate.strip().lower().removesuffix(".git")
    return tail == repository or tail.endswith("/" + repository)


async def _devin_repository_results(
    settings: Settings, client: LiveDevinClient
) -> list[CheckResult]:
    """Read-only contract evidence for `repos[]`: the organization's repository listing must
    know every allowlisted repository under the exact path the worker will send."""
    results = [_repos_format_result(settings)]
    for repository in sorted(settings.allowed_repositories):
        sent = format_devin_repo(settings.devin_repos_format, repository)
        name = f"devin.repository_access[{repository}]"
        try:
            paths = await client.list_repositories_probe(repository)
        except Exception as exc:  # noqa: BLE001 - beta endpoint, permission-dependent
            results.append(
                CheckResult(
                    name,
                    "warn",
                    f"repository listing unavailable ({exc.__class__.__name__}); "
                    f"repos[] will carry {sent!r} unverified",
                )
            )
            continue
        matches = sorted({p for p in paths if _repo_path_matches(p, repository)})
        if matches:
            results.append(
                CheckResult(name, "pass", f"listed as {', '.join(matches)}; repos[] sends {sent!r}")
            )
        else:
            results.append(
                CheckResult(
                    name,
                    "fail",
                    f"not in the organization's repository listing; a session with "
                    f"repos=[{sent!r}] would not have the fork attached",
                )
            )
    return results


async def check_github(settings: Settings, probes: Probes) -> list[CheckResult]:
    if not settings.github_live:
        return [CheckResult("github.repository", "skip", "GITHUB_CLIENT_MODE=fake")]
    if settings.github_token is None:
        return [CheckResult("github.repository", "fail", "GITHUB_TOKEN missing")]
    problem = unsafe_service_url(settings.github_api_base_url)
    if problem:
        return [CheckResult("github.api_base_url", "fail", f"GITHUB_API_BASE_URL {problem}")]
    client = LiveGitHubClient(
        settings.github_token.get_secret_value(),
        settings.allowed_repositories,
        base_url=settings.github_api_base_url,
        transport=probes.github,
    )
    results: list[CheckResult] = []
    try:
        for repo in sorted(settings.allowed_repositories):
            try:
                meta = await client.repository_metadata(repo)
            except GitHubApiError as exc:
                results.append(CheckResult(f"github.repository[{repo}]", "fail", str(exc)))
                continue
            full_name = str(meta.get("full_name", "")).lower()
            default_branch = str(meta.get("default_branch", ""))
            perms = meta.get("permissions") if isinstance(meta.get("permissions"), dict) else {}
            if full_name != repo:
                results.append(
                    CheckResult(
                        f"github.repository[{repo}]",
                        "fail",
                        f"resolved to {full_name or 'unknown'} (renamed or redirected)",
                    )
                )
            else:
                results.append(CheckResult(f"github.repository[{repo}]", "pass", full_name))
            results.append(
                CheckResult(
                    f"github.default_branch[{repo}]",
                    "pass" if default_branch == settings.github_base_ref else "fail",
                    f"default={default_branch or '?'} configured={settings.github_base_ref}",
                )
            )
            results.append(await _base_sha_result(settings, client, repo))
            if perms:
                can_read = bool(perms.get("pull"))
                can_write = bool(perms.get("push"))
                results.append(
                    CheckResult(
                        f"github.permissions[{repo}]",
                        "pass" if can_read else "fail",
                        f"pull={can_read} push={can_write} (labels/comments need issues:write)",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        f"github.permissions[{repo}]",
                        "warn",
                        "token permissions not reported (fine-grained token?)",
                    )
                )
            for label in filter(
                None, {settings.github_remediation_label, settings.github_required_label}
            ):
                try:
                    exists = await client.label_exists(repo, label)
                except GitHubApiError as exc:
                    results.append(CheckResult(f"github.label[{repo}:{label}]", "fail", str(exc)))
                else:
                    results.append(
                        CheckResult(
                            f"github.label[{repo}:{label}]",
                            "pass" if exists else "fail",
                            "exists" if exists else "missing; create it before going live",
                        )
                    )
    finally:
        await client.aclose()
    return results


async def _base_sha_result(settings: Settings, client: LiveGitHubClient, repo: str) -> CheckResult:
    """Resolve the live tip of GITHUB_BASE_REF (what a session would pin right now) and, when
    GITHUB_BASE_SHA_REFERENCE is set, say whether it still equals the last verified SHA."""
    name = f"github.base_sha[{repo}]"
    try:
        sha = await client.resolve_ref(repo, settings.github_base_ref)
    except GitHubApiError as exc:
        return CheckResult(name, "fail", f"cannot resolve {settings.github_base_ref}: {exc}")
    reference = settings.github_base_sha_reference.strip().lower()
    detail = f"{settings.github_base_ref}={sha}"
    if not reference:
        return CheckResult(name, "pass", detail + " (no GITHUB_BASE_SHA_REFERENCE to compare)")
    if sha == reference:
        return CheckResult(name, "pass", detail + " equals GITHUB_BASE_SHA_REFERENCE")
    return CheckResult(
        name,
        "warn",
        detail + f" differs from verified {reference[:12]}: re-check probe manifests and "
        "the smoke result before approving",
    )


async def check_slack(settings: Settings, probes: Probes) -> list[CheckResult]:
    if not settings.slack_live:
        return [CheckResult("slack.identity", "skip", "SLACK_CLIENT_MODE=fake")]
    if settings.slack_bot_token is None:
        return [CheckResult("slack.identity", "fail", "SLACK_BOT_TOKEN missing")]
    problem = unsafe_service_url(settings.slack_api_base_url)
    if problem:
        return [CheckResult("slack.api_base_url", "fail", f"SLACK_API_BASE_URL {problem}")]
    results: list[CheckResult] = []
    if not SLACK_CHANNEL_RE.match(settings.slack_channel_id):
        results.append(
            CheckResult("slack.channel", "fail", "SLACK_CHANNEL_ID is not a channel ID (C…/G…)")
        )
    approvers = sorted(settings.approver_user_ids)
    if not approvers:
        results.append(CheckResult("slack.approvers", "fail", "SLACK_APPROVER_USER_IDS is empty"))
    malformed = [a for a in approvers if not SLACK_USER_RE.match(a)]
    if malformed:
        results.append(CheckResult("slack.approvers", "fail", f"not user IDs: {malformed}"))
    client = LiveSlackClient(
        settings.slack_bot_token.get_secret_value(),
        base_url=settings.slack_api_base_url,
        transport=probes.slack,
    )
    try:
        try:
            me = await client.auth_test()
        except SlackApiError as exc:
            results.append(CheckResult("slack.identity", "fail", exc.describe()))
            return results
        results.append(
            CheckResult(
                "slack.identity", "pass", f"bot={me.get('user', '?')} team={me.get('team', '?')}"
            )
        )
        try:
            channel = await client.channel_info(settings.slack_channel_id)
        except SlackApiError as exc:
            results.append(CheckResult("slack.channel", "fail", exc.describe()))
        else:
            member = bool(channel.get("is_member"))
            archived = bool(channel.get("is_archived"))
            private = bool(channel.get("is_private"))
            results.append(
                CheckResult(
                    "slack.channel",
                    "pass" if member and not archived else "fail",
                    f"#{channel.get('name', settings.slack_channel_id)} is_member={member} "
                    f"is_archived={archived} is_private={private}",
                )
            )
        for approver in approvers:
            if approver in malformed:
                continue
            try:
                user = await client.user_info(approver)
            except SlackApiError as exc:
                detail = exc.error if exc.error == "user_not_found" else exc.describe()
                results.append(CheckResult(f"slack.approver[{approver}]", "fail", detail))
            else:
                verdict: Status
                if user.deleted:
                    verdict, detail = "fail", "deactivated"
                elif user.is_bot:
                    verdict, detail = "fail", "is a bot user"
                else:
                    verdict, detail = "pass", "active"
                results.append(CheckResult(f"slack.approver[{approver}]", verdict, detail))
    finally:
        await client.aclose()
    return results


async def check_slack_test_message(settings: Settings, probes: Probes) -> list[CheckResult]:
    """MUTATING: posts one message to the approval channel. Only runs with --allow-mutations."""
    if not settings.slack_live or settings.slack_bot_token is None:
        return [CheckResult("slack.test_message", "skip", "Slack is not live", mutating=True)]
    client = LiveSlackClient(
        settings.slack_bot_token.get_secret_value(),
        base_url=settings.slack_api_base_url,
        transport=probes.slack,
    )
    try:
        ref = await client.post_message(
            settings.slack_channel_id,
            "superset-devin-remediator readiness check: this message can be deleted.",
            [],
        )
    except SlackApiError as exc:
        return [CheckResult("slack.test_message", "fail", exc.error, mutating=True)]
    finally:
        await client.aclose()
    return [CheckResult("slack.test_message", "pass", f"posted ts={ref.ts}", mutating=True)]


async def check_verifier(settings: Settings, probes: Probes) -> list[CheckResult]:
    if settings.probe_runner_mode != "remote":
        return [CheckResult("verifier.health", "skip", "PROBE_RUNNER_MODE=fake")]
    runner = _remote_runner(settings, probes)
    if isinstance(runner, str):
        return [CheckResult("verifier.health", "fail", runner)]
    try:
        caps = await runner.health()
    finally:
        await runner.aclose()
    if isinstance(caps, str):
        return [CheckResult("verifier.health", "fail", caps)]
    return _verifier_results(settings, caps)


def _remote_runner(settings: Settings, probes: Probes) -> RemoteProbeRunner | str:
    if settings.probe_runner_mode != "remote":
        return "PROBE_RUNNER_MODE=fake"
    secret = settings.probe_verifier_secret_value
    if not settings.probe_verifier_url or not secret or len(secret) < 32:
        return "PROBE_VERIFIER_URL / shared secret unusable"
    problem = unsafe_service_url(settings.probe_verifier_url)
    if problem:
        return f"PROBE_VERIFIER_URL {problem}"
    return RemoteProbeRunner(
        settings.probe_verifier_url,
        secret,
        client=httpx.AsyncClient(
            base_url=settings.probe_verifier_url.rstrip("/"), transport=probes.verifier
        ),
        require_isolation=settings.probe_verifier_isolation_required,
        request_timeout_seconds=settings.probe_verifier_request_timeout_seconds,
    )


async def check_verifier_smoke(settings: Settings, probes: Probes) -> list[CheckResult]:
    """OPT-IN (`--verifier-smoke`): run the registry smoke probe against its BASE SHA in the
    verifier runner. Installs the real frontend dependencies and runs a real Jest test, so
    it takes minutes and needs the egress proxy; it writes nothing outside the runner."""
    repository, sep, issue_text = settings.probe_smoke_probe.partition("#")
    if not sep or not REPOSITORY_RE.match(repository) or not issue_text.isdigit():
        return [CheckResult("verifier.smoke", "fail", "PROBE_SMOKE_PROBE must be owner/name#issue")]
    try:
        probe = await load_approved_probe(
            Path(settings.probe_root), repository, int(issue_text), allow_smoke=True
        )
    except ProbeRegistryError as exc:
        return [CheckResult("verifier.smoke", "fail", f"smoke probe not loadable: {exc}")]
    runner = _remote_runner(settings, probes)
    if isinstance(runner, str):
        return [CheckResult("verifier.smoke", "skip", runner)]
    request_id = uuid.uuid4().hex
    spec = ProbeRunSpec(
        repository=probe.repository,
        issue_number=probe.issue_number,
        commit_sha=probe.base_sha,
        target=ProbeTarget.BASE,
        probe_identifier=probe.identifier,
        script_hash=probe.script_hash,
        script_content=probe.script_content,
        timeout_seconds=probe.timeout_seconds,
        max_output_bytes=settings.probe_max_output_bytes,
        required_tools=probe.required_tools,
        setup_steps=manifest_setup_steps(probe.manifest),
        setup_timeout_seconds=manifest_setup_timeout(probe.manifest),
        cache_inputs=manifest_cache_inputs(probe.manifest),
        manifest_hash=probe.manifest_hash,
        request_id=request_id,
    )
    try:
        result = await runner.run(spec)
    finally:
        await runner.aclose()
    summary = (
        f"{probe.identifier} @ {probe.base_sha[:12]} exit={result.exit_code} "
        f"expected={probe.expected_base_exit_code} {result.duration_ms / 1000:.0f}s "
        f"stage={result.failure_stage or 'run'} "
        + " ".join(f"{k}={v}" for k, v in sorted(result.tool_versions.items()))
    )
    if result.infrastructure_failed:
        return [
            CheckResult(
                "verifier.smoke",
                "fail",
                f"infrastructure: {result.infrastructure_error} | {summary}",
            )
        ]
    if result.timed_out or result.exit_code != probe.expected_base_exit_code:
        tail = result.stderr.strip().splitlines()[-3:] or result.stdout.strip().splitlines()[-3:]
        return [CheckResult("verifier.smoke", "fail", summary + " | " + " / ".join(tail))]
    return [CheckResult("verifier.smoke", "pass", summary)]


def _verifier_results(settings: Settings, caps: Capabilities) -> list[CheckResult]:
    iso = caps.isolation
    results = [
        CheckResult(
            "verifier.health",
            "pass",
            f"{caps.protocol_version} uid={iso.uid} "
            f"in_flight={caps.in_flight}/{caps.max_concurrent}",
        )
    ]
    missing = [tool for tool in REQUIRED_VERIFIER_TOOLS if not caps.tools.get(tool)]
    versions = " ".join(
        f"{tool}={caps.tool_versions.get(tool, '?')}" for tool in REQUIRED_VERIFIER_TOOLS
    )
    results.append(
        CheckResult(
            "verifier.tools",
            "fail" if missing else "pass",
            f"missing {missing}" if missing else versions,
        )
    )
    node = caps.tool_versions.get("node", "")
    if caps.tools.get("node"):
        results.append(
            CheckResult(
                "verifier.node",
                "pass" if node.lstrip("v").startswith("24.") else "warn",
                f"node {node or '?'} (Superset master pins ^24.16.0)",
            )
        )
    unenforced = iso.unenforced()
    results.append(
        CheckResult(
            "verifier.isolation",
            "fail" if unenforced else "pass",
            "; ".join(unenforced) if unenforced else "non-root, read-only root, limits enforced",
        )
    )
    results.append(
        CheckResult(
            "verifier.key_separation",
            "pass" if caps.key_isolated_from_probes else "fail",
            f"probes execute {caps.execution}"
            + (
                ""
                if caps.key_isolated_from_probes
                else " - repository code could read the verifier HMAC key"
            ),
        )
    )
    egress_ok = iso.direct_egress is False
    results.append(
        CheckResult(
            "verifier.egress",
            "pass" if egress_ok else "fail",
            (
                "no direct egress from the probe executor"
                if egress_ok
                else (
                    "probe executor can reach the internet directly"
                    if iso.direct_egress
                    else "egress restriction not verified (VERIFIER_EGRESS_CHECK=off?)"
                )
            )
            + ("; proxy configured" if iso.egress_proxy_configured else "; no egress proxy"),
        )
    )
    verifier_allow = {r.lower() for r in caps.repository_allowlist}
    not_allowed = sorted(settings.allowed_repositories - verifier_allow)
    results.append(
        CheckResult(
            "verifier.allowlist",
            "fail" if not_allowed else "pass",
            f"verifier does not allow {not_allowed}"
            if not_allowed
            else ", ".join(sorted(verifier_allow)),
        )
    )
    results.append(
        CheckResult(
            "verifier.registry",
            "pass" if caps.registry_present else "fail",
            "probe registry mounted" if caps.registry_present else "no /probes registry",
        )
    )
    if settings.probe_timeout_seconds > caps.max_timeout_seconds:
        results.append(
            CheckResult(
                "verifier.timeout",
                "fail",
                f"PROBE_TIMEOUT_SECONDS={settings.probe_timeout_seconds} exceeds verifier "
                f"max {caps.max_timeout_seconds}",
            )
        )
    results.append(
        CheckResult("verifier.cache", "pass", "enabled" if caps.cache_enabled else "disabled")
    )
    return results


# --------------------------------------------------------------------------- driver

READ_ONLY_CHECKS: tuple[CheckFn, ...] = (
    check_configuration,
    check_repository_allowlist,
    check_limits,
    check_canary,
    check_database,
    check_devin,
    check_github,
    check_slack,
    check_verifier,
    check_public_url,
)
MUTATING_CHECKS: tuple[CheckFn, ...] = (check_slack_test_message,)
OPT_IN_CHECKS: tuple[CheckFn, ...] = (check_verifier_smoke,)


def mutation_confirmed(settings: Settings, confirm_channel: str | None) -> str | None:
    """Second gate for mutating checks: the operator must re-type the exact Slack channel
    id the test message will land in. Returns the reason when the gate is not passed."""
    if confirm_channel is None:
        return "--allow-mutations also requires --confirm-channel <SLACK_CHANNEL_ID>"
    if confirm_channel != settings.slack_channel_id:
        return "--confirm-channel does not match SLACK_CHANNEL_ID; refusing to post"
    return None


async def run_checks(
    settings: Settings,
    *,
    allow_mutations: bool = False,
    confirm_channel: str | None = None,
    verifier_smoke: bool = False,
    probes: Probes | None = None,
) -> list[CheckResult]:
    probes = probes or Probes()
    redactor = SettingsRedactingFilter(settings.secret_values)
    results: list[CheckResult] = []
    checks = READ_ONLY_CHECKS + (OPT_IN_CHECKS if verifier_smoke else ())
    if allow_mutations:
        refusal = mutation_confirmed(settings, confirm_channel)
        if refusal is None:
            checks += MUTATING_CHECKS
        else:
            results.append(CheckResult("mutations.confirmation", "fail", refusal, mutating=True))
    for check in checks:
        try:
            batch = await check(settings, probes)
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the rest
            batch = [
                CheckResult(
                    check.__name__.removeprefix("check_"),
                    "fail",
                    f"unexpected {exc.__class__.__name__}: {exc}",
                )
            ]
        results.extend(
            CheckResult(r.name, r.status, redactor.redact(r.detail), r.mutating) for r in batch
        )
    return results


def render(results: list[CheckResult], *, as_json: bool) -> str:
    if as_json:
        return json.dumps([asdict(r) for r in results], indent=2)
    width = max(len(r.name) for r in results) if results else 10
    lines = [
        f"[{r.status.upper():4}] {r.name:<{width}}  {r.detail}"
        + ("  (mutating)" if r.mutating else "")
        for r in results
    ]
    fails = sum(r.status == "fail" for r in results)
    warns = sum(r.status == "warn" for r in results)
    lines.append("")
    lines.append(
        f"{'READY' if not fails else 'NOT READY'}: {fails} failed, {warns} warnings, "
        f"{sum(r.status == 'pass' for r in results)} passed, "
        f"{sum(r.status == 'skip' for r in results)} skipped"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--allow-mutations",
        action="store_true",
        help="also run checks that write to external systems (posts a Slack test message); "
        "requires --confirm-channel",
    )
    parser.add_argument(
        "--confirm-channel",
        metavar="SLACK_CHANNEL_ID",
        help="second confirmation for --allow-mutations: the exact channel id to post into",
    )
    parser.add_argument(
        "--verifier-smoke",
        action="store_true",
        help="run the registry smoke probe (PROBE_SMOKE_PROBE) in the verifier runner: real "
        "dependency install + Jest test at a pinned SHA; minutes, no mutations, no ACUs",
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        # Never print the raw error: pydantic echoes the offending input values.
        for error in exc.errors(include_input=False, include_url=False):
            print(f"[FAIL] config.load  {error['msg']}")
        return 2
    results = asyncio.run(
        run_checks(
            settings,
            allow_mutations=args.allow_mutations,
            confirm_channel=args.confirm_channel,
            verifier_smoke=args.verifier_smoke,
        )
    )
    print(render(results, as_json=args.json))
    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
