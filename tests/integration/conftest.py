import importlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from remediator.api import app as application
from remediator.config import Settings, get_settings
from remediator.db import get_session

app_module = importlib.import_module("remediator.api.app")


@pytest_asyncio.fixture
async def integration_engine(
    test_database_url: str, database_available: bool
) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def integration_session_factory(
    integration_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with integration_engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE notification_outbox, state_transitions, attempts, "
                "webhook_events, cases CASCADE"
            )
        )
    yield async_sessionmaker(integration_engine, expire_on_commit=False)


@pytest.fixture
def test_settings(test_database_url: str) -> Settings:
    return Settings(
        database_url=test_database_url,
        github_webhook_secret="secret",
        operator_token="operator",
        worker_poll_interval_seconds=0.05,
        worker_concurrency=1,
    )


@pytest.fixture
def test_app(
    integration_session_factory: async_sessionmaker[AsyncSession], test_settings: Settings
):
    application.dependency_overrides[get_settings] = lambda: test_settings

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with integration_session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    app_module.session_factory = integration_session_factory
    yield application
    application.dependency_overrides.clear()
