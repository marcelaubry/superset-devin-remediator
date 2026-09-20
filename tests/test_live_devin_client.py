"""LiveDevinClient tests against an in-memory httpx.MockTransport. No real API calls."""

import json
import logging
from collections.abc import Callable

import httpx
import pytest

from remediator.devin.client import (
    CreateSessionRequest,
    DevinApiError,
    DevinSessionNotFound,
    DevinTransportError,
)
from remediator.devin.live import LiveDevinClient
from remediator.devin.triage import TRIAGE_OUTPUT_SCHEMA

API_KEY = "apk_super_secret_value_do_not_leak"
ORG = "org_123"


def _request() -> CreateSessionRequest:
    return CreateSessionRequest(
        prompt="triage please",
        repository="apache/superset",
        base_sha="b" * 40,
        max_acu_limit=5,
        operation_key="op:case:TRIAGE:1",
        tags=("superset-devin-remediator", "issue:apache/superset#1"),
        structured_output_schema=TRIAGE_OUTPUT_SCHEMA,
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: object
) -> LiveDevinClient:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = LiveDevinClient(
        api_key=API_KEY,
        org_id=ORG,
        base_url="https://api.devin.ai/v3",
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
        **kwargs,  # type: ignore[arg-type]
    )
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client


def _session_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "devin-abc",
        "url": "https://app.devin.ai/sessions/abc",
        "status": "new",
        "status_detail": None,
        "tags": ["op:case:TRIAGE:1", "superset-devin-remediator"],
        "structured_output": None,
        "acus_consumed": 0,
        "updated_at": 1_700_000_000,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_session_sends_documented_v3_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=_session_payload())

    client = _client(handler)
    snapshot = await client.create_session(_request())
    await client.aclose()

    assert snapshot.session_id == "devin-abc"
    assert snapshot.status == "new"
    assert snapshot.acus_consumed == 0.0
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == f"/v3/organizations/{ORG}/sessions"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    body = json.loads(request.content)
    assert body["repos"] == ["https://github.com/apache/superset"]
    assert body["max_acu_limit"] == 5
    assert body["structured_output_required"] is True
    assert body["structured_output_schema"]["$schema"].startswith("http://json-schema.org/draft-07")
    assert body["tags"][0] == "op:case:TRIAGE:1"
    assert "superset-devin-remediator" in body["tags"]
    assert body["resumable"] is False
    assert "b" * 12 in body["title"]


@pytest.mark.asyncio
async def test_create_transport_failure_is_uncertain_and_never_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler, max_retries=3)
    with pytest.raises(DevinTransportError, match="outcome unknown"):
        await client.create_session(_request())
    await client.aclose()
    assert calls == 1


@pytest.mark.asyncio
async def test_create_api_error_is_definitive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "ACU quota exhausted"})

    client = _client(handler)
    with pytest.raises(DevinApiError) as excinfo:
        await client.create_session(_request())
    await client.aclose()
    assert excinfo.value.status_code == 402
    assert "quota" in str(excinfo.value)


@pytest.mark.asyncio
async def test_get_retries_transient_failures_with_backoff() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("boom", request=request)
        if attempts == 2:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=_session_payload(status="running", status_detail="working"))

    client = _client(handler, max_retries=3)
    snapshot = await client.get_session("devin-abc")
    await client.aclose()
    assert attempts == 3
    assert snapshot.status_detail == "working"
    assert len(client.sleeps) == 2  # type: ignore[attr-defined]
    assert all(delay >= 0 for delay in client.sleeps)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_gives_up_after_bounded_retries() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, text="nope")

    client = _client(handler, max_retries=2)
    with pytest.raises(DevinApiError):
        await client.get_session("devin-abc")
    await client.aclose()
    assert attempts == 3


@pytest.mark.asyncio
async def test_get_404_is_not_found_without_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, json={"detail": "missing"})

    client = _client(handler, max_retries=3)
    with pytest.raises(DevinSessionNotFound):
        await client.get_session("devin-gone")
    await client.aclose()
    assert attempts == 1


@pytest.mark.asyncio
async def test_find_by_tag_applies_exact_match_and_pagination() -> None:
    pages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal pages
        pages += 1
        assert request.url.params["tags"] == "op:case:TRIAGE:1"
        if pages == 1:
            return httpx.Response(
                200,
                json={
                    "items": [
                        _session_payload(session_id="exact", tags=["op:case:TRIAGE:1"]),
                        _session_payload(session_id="prefix", tags=["op:case:TRIAGE:10"]),
                    ],
                    "has_next_page": True,
                    "end_cursor": "c1",
                },
            )
        assert request.url.params["after"] == "c1"
        return httpx.Response(
            200,
            json={"items": [_session_payload(session_id="exact2")], "has_next_page": False},
        )

    client = _client(handler)
    found = await client.find_sessions_by_tag("op:case:TRIAGE:1")
    await client.aclose()
    assert [s.session_id for s in found] == ["exact", "exact2"]
    assert pages == 2


@pytest.mark.asyncio
async def test_terminate_uses_delete_and_is_not_retried() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=_session_payload(status="exit", status_detail="finished"))

    client = _client(handler)
    snapshot = await client.terminate_session("devin-abc")
    await client.aclose()
    assert calls == ["DELETE"]
    assert snapshot is not None and snapshot.status == "exit"

    def failing(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        raise httpx.ConnectError("down", request=request)

    client = _client(failing, max_retries=3)
    with pytest.raises(DevinTransportError):
        await client.terminate_session("devin-abc")
    await client.aclose()
    assert calls.count("DELETE") == 2


@pytest.mark.asyncio
async def test_api_key_never_appears_in_repr_errors_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"echo {request.headers['Authorization']}")

    root = logging.getLogger()
    handler_obj = logging.StreamHandler()
    root.addHandler(handler_obj)
    try:
        client = _client(handler, max_retries=1)
        assert API_KEY not in repr(client)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(DevinApiError) as excinfo:
                await client.get_session("devin-abc")
            logging.getLogger("remediator.test").warning("leak check %s", API_KEY)
        assert API_KEY not in str(excinfo.value)
        for record in caplog.records:
            assert API_KEY not in record.getMessage()
        await client.aclose()
    finally:
        root.removeHandler(handler_obj)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_status", "detail_fragment"),
    [
        (lambda: httpx.Response(200, json={"total_acus": 3.25}), "available", None),
        (lambda: httpx.Response(200, json={"total_acus": 2}), "available", None),
        (
            lambda: httpx.Response(403, json={"detail": "forbidden"}),
            "unavailable",
            "ViewOrgConsumption",
        ),
        (lambda: httpx.Response(401, json={}), "unavailable", "credentials"),
        (lambda: httpx.Response(404, json={}), "unavailable", "not found"),
        (lambda: httpx.Response(200, json={"total_acus": "3"}), "unavailable", "total_acus"),
        (lambda: httpx.Response(200, json={"total_acus": True}), "unavailable", "total_acus"),
        (lambda: httpx.Response(200, json=[1, 2]), "unavailable", "total_acus"),
    ],
)
async def test_session_consumption_never_estimates_and_maps_failures_to_unavailable(
    response: Callable[[], httpx.Response], expected_status: str, detail_fragment: str | None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/v3/organizations/{ORG}/consumption/daily/sessions/devin-1"
        return response()

    client = _client(handler, max_retries=0)
    report = await client.session_consumption("devin-1")
    assert report.status == expected_status
    if expected_status == "available":
        assert report.acus is not None and report.acus > 0
    else:
        assert report.acus is None
        assert detail_fragment is not None and detail_fragment in (report.detail or "")
        assert API_KEY not in (report.detail or "")


@pytest.mark.asyncio
async def test_session_consumption_transport_error_is_unavailable_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client(handler, max_retries=0)
    report = await client.session_consumption("devin-1")
    assert report.status == "unavailable"
    assert report.acus is None
    assert "transport" in (report.detail or "")
