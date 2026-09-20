from functools import lru_cache
from pathlib import Path
from typing import Literal

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

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

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
    def secret_values(self) -> tuple[str, ...]:
        """Every configured credential, for log redaction and leak tests."""
        candidates = (
            self.github_webhook_secret,
            self.operator_token,
            self.github_token.get_secret_value() if self.github_token else "",
            self.devin_api_key.get_secret_value() if self.devin_api_key else "",
            self.slack_bot_token.get_secret_value() if self.slack_bot_token else "",
            self.slack_signing_secret.get_secret_value() if self.slack_signing_secret else "",
        )
        return tuple(value for value in candidates if value and value != PLACEHOLDER_SECRET)

    def _require_secret(self, name: str, value: SecretStr | None, mode_flag: str) -> None:
        raw = value.get_secret_value() if value else ""
        if not raw.strip() or raw == PLACEHOLDER_SECRET or len(raw) < MIN_LIVE_SECRET_LENGTH:
            raise ValueError(
                f"{mode_flag}=live requires {name} to be a real secret of at least "
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
            if self.slack_fake_fail_posts:
                raise ValueError("SLACK_FAKE_FAIL_POSTS is a fake-mode-only switch")
        if self.github_live:
            self._require_secret("GITHUB_TOKEN", self.github_token, "GITHUB_CLIENT_MODE")
            if not self.github_api_base_url.startswith("https://"):
                raise ValueError("GITHUB_API_BASE_URL must use https in live mode")
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
        if self.devin_triage_timeout_seconds < MIN_LIVE_TRIAGE_TIMEOUT_SECONDS:
            raise ValueError(
                "DEVIN_TRIAGE_TIMEOUT_SECONDS must be at least "
                f"{MIN_LIVE_TRIAGE_TIMEOUT_SECONDS:.0f} in live mode; the short value in "
                ".env.example is for fake-mode simulations only"
            )
        for name, value in (
            ("GITHUB_WEBHOOK_SECRET", self.github_webhook_secret),
            ("OPERATOR_TOKEN", self.operator_token),
        ):
            if value == PLACEHOLDER_SECRET or len(value) < MIN_LIVE_SECRET_LENGTH:
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
