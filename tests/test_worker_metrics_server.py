import httpx
import pytest

from remediator import metrics
from remediator.config import Settings
from remediator.worker.metrics_server import build_app, build_server


def _settings(**overrides: object) -> Settings:
    return Settings(operator_token="worker-metrics-token", **overrides)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_worker_metrics_require_operator_token_and_expose_process_counters() -> None:
    metrics.capacity_denied_total.labels(metrics.mode(), "triage").inc()
    transport = httpx.ASGITransport(app=build_app(_settings()))
    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
        assert (await client.get("/metrics")).status_code == 401
        wrong = await client.get("/metrics", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        cookie = await client.get("/metrics", cookies={"operator_session": "x"})
        assert cookie.status_code == 401
        ok = await client.get("/metrics", headers={"Authorization": "Bearer worker-metrics-token"})
        assert ok.status_code == 200
        assert 'capacity_denied_total{kind="triage",mode="simulated"}' in ok.text
        assert (await client.get("/health")).text == "ok"


def test_worker_metrics_server_disabled_with_port_zero() -> None:
    assert build_server(_settings(worker_metrics_port=0)) is None
    server = build_server(_settings(worker_metrics_port=18001))
    assert server is not None
    assert server.config.port == 18001
