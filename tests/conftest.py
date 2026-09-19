import os

import pytest
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

# Tests never touch the real Devin API and must not inherit slow live-mode pacing.
os.environ["DEVIN_CLIENT_MODE"] = "fake"
os.environ.setdefault("DEVIN_POLL_INTERVAL_SECONDS", "0")
os.environ.setdefault("DEVIN_TRIAGE_TIMEOUT_SECONDS", "5")
os.environ.pop("DEVIN_API_KEY", None)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://remediator:remediator@localhost:5432/remediator_test",
    )


@pytest.fixture(scope="session")
def database_available(test_database_url: str) -> bool:
    import asyncio

    async def check() -> bool:
        engine = create_async_engine(test_database_url)
        try:
            async with engine.connect():
                return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    available = asyncio.run(check())
    if not available:
        pytest.skip("PostgreSQL integration tests skipped: TEST_DATABASE_URL is unreachable")
    return available


@pytest.fixture(scope="session", autouse=True)
def migrate_test_database(test_database_url: str, database_available: bool) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    command.upgrade(config, "head")
