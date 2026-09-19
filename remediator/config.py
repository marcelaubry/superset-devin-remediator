from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://remediator:remediator@localhost:5432/remediator"
    github_webhook_secret: str = "change-me"
    operator_token: str = "change-me"
    github_repository: str = "apache/superset"
    github_allowed_events: str = "issues"
    github_allowed_actions: str = "opened,labeled"
    github_required_label: str = "devin-candidate"
    worker_poll_interval_seconds: float = 1.0
    worker_concurrency: int = 2
    devin_client: str = "fake"
    log_level: str = "INFO"
    simulation_auto_approve_remediation: bool = True

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def allowed_events(self) -> set[str]:
        return {x.strip() for x in self.github_allowed_events.split(",") if x.strip()}

    @property
    def allowed_actions(self) -> set[str]:
        return {x.strip() for x in self.github_allowed_actions.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
