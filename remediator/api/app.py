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

from .. import metrics
from ..adapters import install_secret_redaction
from ..capacity import CapacityManager
from ..config import get_settings
from ..db import build_engine, build_session_factory, get_session
from .auth import OperatorAuthRequired
from .dashboard import router as dashboard_router
from .hardening import (
    BodySizeLimitMiddleware,
    TokenBucketLimiter,
    make_operator_guard,
    with_security_headers,
)
from .operator import router as operator_router
from .slack_actions import router as slack_actions_router
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
    install_secret_redaction(settings)
    metrics.configure(settings.metrics_mode)
    app = FastAPI(title="Superset Devin Remediator", lifespan=lifespan)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    limiter = TokenBucketLimiter(
        capacity=settings.operator_rate_limit_per_minute,
        refill_per_second=settings.operator_rate_limit_per_minute / 60.0,
    )
    operator_guard = make_operator_guard(limiter)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        return with_security_headers(await call_next(request))

    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parents[1] / "static")),
        name="static",
    )

    @app.exception_handler(OperatorAuthRequired)
    async def operator_auth_required(_: Request, __: OperatorAuthRequired) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    app.include_router(webhook_router)
    app.include_router(slack_actions_router)
    app.include_router(dashboard_router)
    app.include_router(operator_router, dependencies=[Depends(operator_guard)])

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

    capacity = CapacityManager.from_settings(settings, "api")

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics_endpoint(
        _: str = Depends(require_operator),
        session: AsyncSession = Depends(get_session),
    ) -> PlainTextResponse:
        await metrics.refresh_gauges(session, capacity)
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
