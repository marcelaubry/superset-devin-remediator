from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_LIVE_POLL_INTERVAL_SECONDS = 10.0
MAX_RECOMMENDED_POLL_INTERVAL_SECONDS = 30.0
MIN_LIVE_TRIAGE_TIMEOUT_SECONDS = 300.0
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
    github_required_label: str = "devin-candidate"
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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
