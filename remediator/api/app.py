import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ..config import get_settings
from ..db import build_engine, build_session_factory
from .auth import OperatorAuthRequired
from .dashboard import router as dashboard_router
from .operator import router as operator_router
from .webhooks import router as webhook_router

engine: AsyncEngine | None = None
session_factory: async_sessionmaker[AsyncSession] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global engine, session_factory
    engine = build_engine(get_settings())
    session_factory = build_session_factory(engine)
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    app = FastAPI(title="Superset Devin Remediator", lifespan=lifespan)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parents[1] / "static")),
        name="static",
    )

    @app.exception_handler(OperatorAuthRequired)
    async def operator_auth_required(_: Request, __: OperatorAuthRequired) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    app.include_router(webhook_router)
    app.include_router(dashboard_router)
    app.include_router(operator_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        if session_factory is None:
            raise HTTPException(status_code=503, detail="database unavailable")
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ok"}

    from .auth import require_operator

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(
        _: str = Depends(require_operator),
    ) -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
