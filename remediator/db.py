from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Settings


def build_engine(settings: Settings, application_name: str = "remediator") -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"server_settings": {"application_name": application_name[:63]}},
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    from .api.app import session_factory

    if session_factory is None:
        raise RuntimeError("database session factory is not initialized")
    async with session_factory() as session:
        yield session
