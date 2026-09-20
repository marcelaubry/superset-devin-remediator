"""Authenticated `/metrics` for the worker process.

Prometheus counters and histograms live in the process that increments them; the API and
worker are separate containers, so the worker's series (`capacity_denied_total`,
`probe_outcomes_total`, `provider_requests_total`, ...) are served here and the
database-derived gauges (`case_queue_depth`, `active_jobs`, ...) only by the API. Scrape both.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
from collections.abc import Iterator

import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from ..config import Settings

log = logging.getLogger(__name__)


def build_app(settings: Settings) -> Starlette:
    async def metrics_endpoint(request: Request) -> Response:
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, settings.operator_token):
            return PlainTextResponse("unauthorized", status_code=401)
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[Route("/metrics", metrics_endpoint), Route("/health", health)],
    )


def build_server(settings: Settings) -> uvicorn.Server | None:
    if settings.worker_metrics_port <= 0:
        return None
    config = uvicorn.Config(
        build_app(settings),
        host=settings.worker_metrics_host,
        port=settings.worker_metrics_port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    return _EmbeddedServer(config)


class _EmbeddedServer(uvicorn.Server):
    """Runs inside the worker's event loop; the worker owns SIGTERM/SIGINT handling."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield
