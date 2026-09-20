import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_LIVE_POLL_INTERVAL_SECONDS = 10.0
MAX_RECOMMENDED_POLL_INTERVAL_SECONDS = 30.0
MIN_LIVE_TRIAGE_TIMEOUT_SECONDS = 300.0
MIN_LIVE_REMEDIATION_TIMEOUT_SECONDS = 600.0
MIN_LIVE_CI_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_PR_AUTHOR_LOGINS = "devin-ai-integration[bot]"
MIN_LIVE_SECRET_LENGTH = 16
PLACEHOLDER_SECRET = "change-me"


def unsafe_service_url(url: str) -> str | None:
    """Why `url` may not be used as an outbound service base (SSRF surface), or None.

    Service bases are operator configuration, never request input, but a misconfigured
    value must still fail closed: only http(s), no credentials, no query/fragment, no
    empty host. Loopback/private hosts are allowed because the verifier and dashboard are
    internal by design; the readiness command reports them.
    """
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return "must be an http(s) URL"
    if not parsed.hostname:
        return "must include a host"
    if parsed.username is not None or parsed.password is not None:
        return "must not embed credentials"
    if parsed.query or parsed.fragment:
        return "must not carry a query string or fragment"
    if any(ch.isspace() for ch in url.strip()):
        return "must not contain whitespace"
    return None


_METADATA_HOSTS = frozenset(
    {"metadata.google.internal", "metadata", "instance-data", "169.254.169.254", "fd00:ec2::254"}
)


def non_public_service_host(url: str) -> str | None:
    """Why `url` cannot be a *provider* base (GitHub/Devin/Slack) in live mode, or None.

    Provider APIs are public SaaS endpoints; a loopback, private, link-local or cloud
    metadata host there means either a typo or an SSRF pivot through configuration.
    """
    host = (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    if not host:
        return "must include a host"
    if host in _METADATA_HOSTS or host == "localhost" or host.endswith(".localhost"):
        return f"host {host!r} is loopback or cloud metadata"
    if host.endswith((".internal", ".local", ".localdomain")) or "." not in host:
        return f"host {host!r} is not a public DNS name"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_global:
        return f"host {host!r} is a non-public IP address"
    return None


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://remediator:remediator@localhost:5432/remediator"
    github_webhook_secret: str = "change-me"
    operator_token: str = "change-me"
    github_repository: str = "apache/superset"
    github_base_ref: str = "master"
    github_api_base_url: str = "https://api.github.com"
    github_token: SecretStr | None = None
    github_client_mode: Literal["fake", "live"] = "fake"
    github_remediation_label: str = "devin:remediate"
    github_fake_fail_labels: bool = False
    github_allowed_events: str = "issues"
    github_allowed_actions: str = "opened,labeled"
    # Empty by default: every newly opened issue passes through the zero-ACU eligibility
    # filter. Set a label to make intake opt-in for a deployment.
    github_required_label: str = ""
    worker_poll_interval_seconds: float = 1.0
    worker_concurrency: int = 2
    worker_lease_seconds: int = 300
    worker_shutdown_timeout_seconds: int = 30
    # Worker-process counters/histograms are served here (operator token); 0 disables.
    worker_metrics_port: int = 8001
    worker_metrics_host: str = "0.0.0.0"
    event_max_attempts: int = 3
    reconcile_retry_delay_seconds: float = 1.0
    reconcile_max_attempts: int = 3
    devin_client_mode: Literal["fake", "live"] = "fake"
    devin_api_key: SecretStr | None = None
    devin_org_id: str | None = None
    devin_api_base_url: str = "https://api.devin.ai/v3"
    devin_repos_format: str = "https://github.com/{repository}"
    devin_triage_max_acu: int = 5
    devin_triage_timeout_seconds: float = 1800.0
    devin_poll_interval_seconds: float = 15.0
    devin_http_timeout_seconds: float = 30.0
    devin_http_max_retries: int = 3
    devin_remediation_max_acu: int = 15
    devin_remediation_timeout_seconds: float = 5400.0
    devin_remediation_branch_prefix: str = "devin/"
    # The worker never runs repository code itself: "fake" is deterministic simulation data,
    # "remote" delegates to the credential-free verifier container (remediator.verifier) and
    # refuses to trust a verifier that can see any credential. There is no "local" mode here
    # because this process always holds at least the database credential.
    probe_runner_mode: Literal["fake", "remote"] = "fake"
    probe_verifier_url: str | None = None
    probe_root: str = "probes"
    probe_timeout_seconds: int = 900
    probe_max_output_bytes: int = 65536
    ci_poll_interval_seconds: float = 60.0
    ci_timeout_seconds: float = 4 * 3600.0
    github_pr_author_logins: str = DEFAULT_PR_AUTHOR_LOGINS
    github_required_checks: str = ""
    remediation_max_changed_files: int = 25
    max_attempts_per_kind: int = 3
    log_level: str = "INFO"
    cookie_secure: bool = False
    dashboard_base_url: str = "http://localhost:8000"
    slack_client_mode: Literal["fake", "live"] = "fake"
    slack_bot_token: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None
    slack_channel_id: str = "C0000000000"
    slack_approver_user_ids: str = ""
    slack_max_timestamp_skew_seconds: int = 300
    slack_api_base_url: str = "https://slack.com/api"
    slack_action_token_ttl_seconds: int = 7 * 24 * 3600
    slack_fake_fail_posts: bool = False
    outbox_max_attempts: int = 5
    outbox_base_backoff_seconds: float = 2.0
    outbox_max_backoff_seconds: float = 300.0
    outbox_lease_seconds: int = 120
    # Phase 5: verifier authentication, concurrency, hardening, readiness.
    probe_verifier_shared_secret: SecretStr | None = None
    probe_verifier_request_timeout_seconds: float = 30.0
    # Refuse a verifier that cannot show no-new-privileges, empty capabilities and cgroup
    # PID/memory limits. Only ever off for in-process tests; live mode forces it on.
    probe_verifier_require_isolation: bool = True
    # Registry probe (`owner/name#issue`) that `readiness --verifier-smoke` executes against
    # its BASE SHA to prove a real Node/Jest probe runs end to end. Issue 0 is reserved
    # for smoke probes and is never a remediation case.
    probe_smoke_probe: str = "apache/superset#0"
    max_concurrent_triage: int = 2
    max_concurrent_remediation: int = 1
    max_concurrent_probes: int = 1
    max_concurrent_remediation_per_repository: int = 1
    capacity_wait_backoff_seconds: float = 5.0
    capacity_lease_grace_seconds: int = 600
    max_request_body_bytes: int = 1_048_576
    operator_rate_limit_per_minute: int = 60
    operator_csrf_trusted_origins: str = ""
    devin_acu_reporting_enabled: bool = False
    public_base_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def probe_verifier_secret_value(self) -> str | None:
        return (
            self.probe_verifier_shared_secret.get_secret_value()
            if self.probe_verifier_shared_secret
            else None
        )

    @property
    def probe_verifier_isolation_required(self) -> bool:
        return self.probe_verifier_require_isolation or self.live_mode

    @property
    def allowed_events(self) -> set[str]:
        return {x.strip() for x in self.github_allowed_events.split(",") if x.strip()}

    @property
    def allowed_actions(self) -> set[str]:
        return {x.strip() for x in self.github_allowed_actions.split(",") if x.strip()}

    @property
    def live_mode(self) -> bool:
        return self.devin_client_mode == "live"

    @property
    def slack_live(self) -> bool:
        return self.slack_client_mode == "live"

    @property
    def github_live(self) -> bool:
        return self.github_client_mode == "live"

    @property
    def approver_user_ids(self) -> frozenset[str]:
        return frozenset(x.strip() for x in self.slack_approver_user_ids.split(",") if x.strip())

    @property
    def pr_author_logins(self) -> frozenset[str]:
        return frozenset(
            x.strip().lower() for x in self.github_pr_author_logins.split(",") if x.strip()
        )

    @property
    def required_checks(self) -> tuple[str, ...]:
        return tuple(x.strip() for x in self.github_required_checks.split(",") if x.strip())

    @property
    def probe_root_path(self) -> Path:
        return Path(self.probe_root).expanduser()

    @property
    def allowed_repositories(self) -> frozenset[str]:
        return frozenset(x.strip().lower() for x in self.github_repository.split(",") if x.strip())

    def repository_allowed(self, repository: str) -> bool:
        return repository.strip().lower() in self.allowed_repositories

    @property
    def simulated(self) -> bool:
        """True unless every provider adapter is live; drives the `mode` metric label."""
        return not (self.live_mode and self.github_live and self.slack_live)

    @property
    def metrics_mode(self) -> str:
        return "simulated" if self.simulated else "live"

    @property
    def csrf_trusted_origins(self) -> frozenset[str]:
        configured = {
            x.strip().rstrip("/").lower()
            for x in self.operator_csrf_trusted_origins.split(",")
            if x.strip()
        }
        for base in (self.dashboard_base_url, self.public_base_url):
            if base.strip():
                configured.add(base.strip().rstrip("/").lower())
        return frozenset(configured)

    @property
    def secret_values(self) -> tuple[str, ...]:
        """Every configured credential, for log redaction and leak tests."""
        candidates: tuple[str, ...] = (
            self.github_webhook_secret,
            self.operator_token,
            self.github_token.get_secret_value() if self.github_token else "",
            self.devin_api_key.get_secret_value() if self.devin_api_key else "",
            self.slack_bot_token.get_secret_value() if self.slack_bot_token else "",
            self.slack_signing_secret.get_secret_value() if self.slack_signing_secret else "",
            (
                self.probe_verifier_shared_secret.get_secret_value()
                if self.probe_verifier_shared_secret
                else ""
            ),
        )
        db_password = urlsplit(self.database_url).password or ""
        if db_password:
            candidates += (db_password, f"{urlsplit(self.database_url).username}:{db_password}")
        return tuple(value for value in candidates if value and value != PLACEHOLDER_SECRET)

    def _require_secret(
        self, name: str, value: SecretStr | None, mode_flag: str, mode_value: str = "live"
    ) -> None:
        raw = value.get_secret_value() if value else ""
        if not raw.strip() or raw == PLACEHOLDER_SECRET or len(raw) < MIN_LIVE_SECRET_LENGTH:
            raise ValueError(
                f"{mode_flag}={mode_value} requires {name} to be a real secret of at least "
                f"{MIN_LIVE_SECRET_LENGTH} characters; refusing to start"
            )

    @model_validator(mode="after")
    def _validate_bounds(self) -> "Settings":
        if self.devin_triage_max_acu <= 0:
            raise ValueError("DEVIN_TRIAGE_MAX_ACU must be a positive integer")
        if self.devin_triage_timeout_seconds <= 0:
            raise ValueError("DEVIN_TRIAGE_TIMEOUT_SECONDS must be positive")
        if self.devin_poll_interval_seconds < 0:
            raise ValueError("DEVIN_POLL_INTERVAL_SECONDS must be non-negative")
        if self.slack_max_timestamp_skew_seconds <= 0:
            raise ValueError("SLACK_MAX_TIMESTAMP_SKEW_SECONDS must be positive")
        if self.slack_action_token_ttl_seconds <= 0:
            raise ValueError("SLACK_ACTION_TOKEN_TTL_SECONDS must be positive")
        if self.outbox_max_attempts <= 0:
            raise ValueError("OUTBOX_MAX_ATTEMPTS must be a positive integer")
        if self.devin_remediation_max_acu <= 0:
            raise ValueError("DEVIN_REMEDIATION_MAX_ACU must be a positive integer")
        if self.devin_remediation_timeout_seconds <= 0:
            raise ValueError("DEVIN_REMEDIATION_TIMEOUT_SECONDS must be positive")
        prefix = self.devin_remediation_branch_prefix
        if not prefix or not prefix.endswith("/") or prefix.startswith("/") or ".." in prefix:
            raise ValueError(
                "DEVIN_REMEDIATION_BRANCH_PREFIX must be a non-empty ref namespace ending in '/'"
            )
        if self.probe_timeout_seconds <= 0:
            raise ValueError("PROBE_TIMEOUT_SECONDS must be positive")
        if self.probe_max_output_bytes < 1024:
            raise ValueError("PROBE_MAX_OUTPUT_BYTES must be at least 1024")
        if self.ci_poll_interval_seconds < 0:
            raise ValueError("CI_POLL_INTERVAL_SECONDS must be non-negative")
        if self.ci_timeout_seconds <= 0:
            raise ValueError("CI_TIMEOUT_SECONDS must be positive")
        if not self.probe_root.strip():
            raise ValueError("PROBE_ROOT must not be empty")
        if not self.pr_author_logins:
            raise ValueError("GITHUB_PR_AUTHOR_LOGINS must name the expected integration identity")
        if self.probe_runner_mode == "remote" and not (self.probe_verifier_url or "").startswith(
            ("http://", "https://")
        ):
            raise ValueError("PROBE_RUNNER_MODE=remote requires PROBE_VERIFIER_URL (http(s) URL)")
        if not self.allowed_repositories:
            raise ValueError("GITHUB_REPOSITORY must name at least one allowlisted repository")
        for name, value in (
            ("MAX_CONCURRENT_TRIAGE", self.max_concurrent_triage),
            ("MAX_CONCURRENT_REMEDIATION", self.max_concurrent_remediation),
            ("MAX_CONCURRENT_PROBES", self.max_concurrent_probes),
            (
                "MAX_CONCURRENT_REMEDIATION_PER_REPOSITORY",
                self.max_concurrent_remediation_per_repository,
            ),
            ("CAPACITY_LEASE_GRACE_SECONDS", self.capacity_lease_grace_seconds),
            ("OPERATOR_RATE_LIMIT_PER_MINUTE", self.operator_rate_limit_per_minute),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.capacity_wait_backoff_seconds < 0:
            raise ValueError("CAPACITY_WAIT_BACKOFF_SECONDS must be non-negative")
        if self.max_request_body_bytes < 4096:
            raise ValueError("MAX_REQUEST_BODY_BYTES must be at least 4096")
        for name, url in (
            ("GITHUB_API_BASE_URL", self.github_api_base_url),
            ("DEVIN_API_BASE_URL", self.devin_api_base_url),
            ("SLACK_API_BASE_URL", self.slack_api_base_url),
            ("PROBE_VERIFIER_URL", self.probe_verifier_url or "http://verifier:8080"),
            ("DASHBOARD_BASE_URL", self.dashboard_base_url),
        ):
            problem = unsafe_service_url(url)
            if problem:
                raise ValueError(f"{name}: {problem}")
        if self.slack_live:
            self._require_secret("SLACK_BOT_TOKEN", self.slack_bot_token, "SLACK_CLIENT_MODE")
            self._require_secret(
                "SLACK_SIGNING_SECRET", self.slack_signing_secret, "SLACK_CLIENT_MODE"
            )
            if not self.approver_user_ids:
                raise ValueError("SLACK_CLIENT_MODE=live requires SLACK_APPROVER_USER_IDS")
            if not self.slack_channel_id.strip():
                raise ValueError("SLACK_CLIENT_MODE=live requires SLACK_CHANNEL_ID")
            if not self.slack_api_base_url.startswith("https://"):
                raise ValueError("SLACK_API_BASE_URL must use https in live mode")
            if problem := non_public_service_host(self.slack_api_base_url):
                raise ValueError(f"SLACK_API_BASE_URL {problem}")
            if self.slack_fake_fail_posts:
                raise ValueError("SLACK_FAKE_FAIL_POSTS is a fake-mode-only switch")
        if self.github_live:
            self._require_secret("GITHUB_TOKEN", self.github_token, "GITHUB_CLIENT_MODE")
            if not self.github_api_base_url.startswith("https://"):
                raise ValueError("GITHUB_API_BASE_URL must use https in live mode")
            if problem := non_public_service_host(self.github_api_base_url):
                raise ValueError(f"GITHUB_API_BASE_URL {problem}")
            if self.github_fake_fail_labels:
                raise ValueError("GITHUB_FAKE_FAIL_LABELS is a fake-mode-only switch")
        if not self.live_mode:
            return self
        missing = [
            name
            for name, value in (
                (
                    "DEVIN_API_KEY",
                    self.devin_api_key.get_secret_value() if self.devin_api_key else "",
                ),
                ("DEVIN_ORG_ID", self.devin_org_id or ""),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(
                "DEVIN_CLIENT_MODE=live requires " + ", ".join(missing) + "; refusing to start"
            )
        if not self.devin_api_base_url.startswith("https://"):
            raise ValueError("DEVIN_API_BASE_URL must use https in live mode")
        if problem := non_public_service_host(self.devin_api_base_url):
            raise ValueError(f"DEVIN_API_BASE_URL {problem}")
        if self.devin_triage_timeout_seconds < MIN_LIVE_TRIAGE_TIMEOUT_SECONDS:
            raise ValueError(
                "DEVIN_TRIAGE_TIMEOUT_SECONDS must be at least "
                f"{MIN_LIVE_TRIAGE_TIMEOUT_SECONDS:.0f} in live mode; the short value in "
                ".env.example is for fake-mode simulations only"
            )
        for name, secret in (
            ("GITHUB_WEBHOOK_SECRET", self.github_webhook_secret),
            ("OPERATOR_TOKEN", self.operator_token),
        ):
            if secret == PLACEHOLDER_SECRET or len(secret) < MIN_LIVE_SECRET_LENGTH:
                raise ValueError(
                    f"{name} must be a unique secret of at least {MIN_LIVE_SECRET_LENGTH} "
                    "characters in live mode"
                )
        if self.devin_poll_interval_seconds < MIN_LIVE_POLL_INTERVAL_SECONDS:
            raise ValueError(
                "DEVIN_POLL_INTERVAL_SECONDS must be at least "
                f"{MIN_LIVE_POLL_INTERVAL_SECONDS:g}s in live mode"
            )
        # Phase 4 fail-closed checks: a live Devin session may open a real PR, so the
        # evidence chain (GitHub, probe runner, CI) must be real as well.
        if not self.github_live:
            raise ValueError(
                "DEVIN_CLIENT_MODE=live requires GITHUB_CLIENT_MODE=live; PR and CI evidence "
                "cannot come from the fake GitHub adapter"
            )
        if self.devin_remediation_timeout_seconds < MIN_LIVE_REMEDIATION_TIMEOUT_SECONDS:
            raise ValueError(
                "DEVIN_REMEDIATION_TIMEOUT_SECONDS must be at least "
                f"{MIN_LIVE_REMEDIATION_TIMEOUT_SECONDS:.0f} in live mode"
            )
        if self.probe_runner_mode != "remote":
            raise ValueError(
                "DEVIN_CLIENT_MODE=live requires PROBE_RUNNER_MODE=remote; the fake probe runner "
                "is not independent evidence and probes must never execute inside a process "
                "that holds Devin, GitHub, Slack, operator or database credentials "
                "(see docs/threat-model.md)"
            )
        if not self.probe_root_path.is_dir():
            raise ValueError(
                f"PROBE_ROOT {self.probe_root!r} must be an existing directory in live mode"
            )
        if self.ci_poll_interval_seconds < MIN_LIVE_CI_POLL_INTERVAL_SECONDS:
            raise ValueError(
                "CI_POLL_INTERVAL_SECONDS must be at least "
                f"{MIN_LIVE_CI_POLL_INTERVAL_SECONDS:g}s in live mode"
            )
        return self

    @model_validator(mode="after")
    def _validate_verifier_auth(self) -> "Settings":
        if self.probe_runner_mode == "remote":
            self._require_secret(
                "PROBE_VERIFIER_SHARED_SECRET",
                self.probe_verifier_shared_secret,
                "PROBE_RUNNER_MODE",
                "remote",
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
