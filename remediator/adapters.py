"""Factories for the Slack and GitHub adapters plus process-wide secret redaction."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .github.client import FakeGitHubClient, GitHubIssuesClient, LiveGitHubClient
from .slack.client import FakeSlackClient, LiveSlackClient, SlackClient


class SettingsRedactingFilter(logging.Filter):
    """Scrubs every configured credential from log records (message and args)."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = tuple(sorted(secrets, key=len, reverse=True))

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_secret_redaction(settings: Settings) -> SettingsRedactingFilter:
    redactor = SettingsRedactingFilter(settings.secret_values)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(redactor)
    root.addFilter(redactor)
    return redactor


def build_slack_client(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> SlackClient:
    if not settings.slack_live:
        return FakeSlackClient(
            session_factory,
            fail_posts=settings.slack_fake_fail_posts,
            failing_issue_attempts=settings.outbox_max_attempts,
        )
    assert settings.slack_bot_token is not None
    return LiveSlackClient(
        settings.slack_bot_token.get_secret_value(), base_url=settings.slack_api_base_url
    )


def build_github_client(settings: Settings) -> GitHubIssuesClient:
    if not settings.github_live:
        return FakeGitHubClient(
            settings.allowed_repositories,
            fail_labels=settings.github_fake_fail_labels,
            failing_issue_attempts=settings.outbox_max_attempts,
        )
    assert settings.github_token is not None
    return LiveGitHubClient(
        settings.github_token.get_secret_value(),
        settings.allowed_repositories,
        base_url=settings.github_api_base_url,
    )
