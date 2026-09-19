import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine


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
