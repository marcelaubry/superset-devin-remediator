from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_LIVE_POLL_INTERVAL_SECONDS = 10.0
MAX_RECOMMENDED_POLL_INTERVAL_SECONDS = 30.0


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://remediator:remediator@localhost:5432/remediator"
    github_webhook_secret: str = "change-me"
    operator_token: str = "change-me"
    github_repository: str = "apache/superset"
    github_base_ref: str = "master"
    github_api_base_url: str = "https://api.github.com"
    github_api_token: SecretStr | None = None
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
    simulation_auto_approve_remediation: bool = True
    cookie_secure: bool = False

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

    @model_validator(mode="after")
    def _validate_bounds(self) -> "Settings":
        if self.devin_triage_max_acu <= 0:
            raise ValueError("DEVIN_TRIAGE_MAX_ACU must be a positive integer")
        if self.devin_triage_timeout_seconds <= 0:
            raise ValueError("DEVIN_TRIAGE_TIMEOUT_SECONDS must be positive")
        if self.devin_poll_interval_seconds < 0:
            raise ValueError("DEVIN_POLL_INTERVAL_SECONDS must be non-negative")
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
        if self.devin_poll_interval_seconds < MIN_LIVE_POLL_INTERVAL_SECONDS:
            raise ValueError(
                "DEVIN_POLL_INTERVAL_SECONDS must be at least "
                f"{MIN_LIVE_POLL_INTERVAL_SECONDS:g}s in live mode"
            )
        if self.simulation_auto_approve_remediation:
            raise ValueError(
                "SIMULATION_AUTO_APPROVE_REMEDIATION must be false in live mode "
                "(remediation sessions are not part of Phase 2)"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
