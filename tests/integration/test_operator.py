from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remediator.lifecycle import CaseState
from remediator.models import Case


async def add_case(
    factory: async_sessionmaker[AsyncSession],
    number: int,
    state: CaseState,
) -> UUID:
    async with factory() as session:
        case = Case(
            issue_number=number,
            repository="apache/superset",
            issue_title="Fix issue",
            issue_url=f"https://github.com/apache/superset/issues/{number}",
            state=state,
            rubric=[{"name": "scope", "passed": True, "reason": "Scope is bounded."}],
            state_entered_at=datetime.now(UTC),
        )
        session.add(case)
        await session.commit()
        return case.id


@pytest.mark.asyncio
async def test_operator_retry_cancel_and_case_auth(
    test_app, integration_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    failed_id = await add_case(integration_session_factory, 5001, CaseState.FAILED)
    passed_id = await add_case(integration_session_factory, 5002, CaseState.CI_PASSED)
    triaging_id = await add_case(integration_session_factory, 5003, CaseState.TRIAGING)
    cancelled_id = await add_case(integration_session_factory, 5004, CaseState.CANCELLED)
    blocked_id = await add_case(integration_session_factory, 5005, CaseState.HUMAN_BLOCKED)
    approval_id = await add_case(
        integration_session_factory, 5006, CaseState.AWAITING_REMEDIATION_APPROVAL
    )
    reject_id = await add_case(
        integration_session_factory, 5007, CaseState.AWAITING_REMEDIATION_APPROVAL
    )
    headers = {"Authorization": "Bearer operator"}
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        retry = await client.post(f"/operator/cases/{failed_id}/retry", headers=headers)
        assert retry.status_code == 200
        assert retry.json()["state"] == CaseState.RECEIVED

        invalid_retry = await client.post(f"/operator/cases/{passed_id}/retry", headers=headers)
        assert invalid_retry.status_code == 409

        cancel = await client.post(f"/operator/cases/{triaging_id}/cancel", headers=headers)
        assert cancel.status_code == 200
        assert cancel.json()["state"] == CaseState.CANCELLED

        invalid_cancel = await client.post(
            f"/operator/cases/{cancelled_id}/cancel", headers=headers
        )
        assert invalid_cancel.status_code == 409

        hx_headers = {**headers, "HX-Request": "true"}
        hx_retry = await client.post(f"/operator/cases/{blocked_id}/retry", headers=hx_headers)
        assert hx_retry.status_code == 200
        assert '<section id="case-status">' in hx_retry.text

        hx_cancel = await client.post(f"/operator/cases/{blocked_id}/cancel", headers=hx_headers)
        assert hx_cancel.status_code == 200
        assert "CANCELLED" in hx_cancel.text

        approve = await client.post(
            f"/operator/cases/{approval_id}/approve-remediation", headers=headers
        )
        assert approve.status_code == 200
        assert approve.json()["state"] == CaseState.REMEDIATION_CREATE_INTENT

        reject = await client.post(
            f"/operator/cases/{reject_id}/reject-remediation", headers=headers
        )
        assert reject.status_code == 200
        assert reject.json()["state"] == CaseState.CANCELLED

        invalid_approve = await client.post(
            f"/operator/cases/{passed_id}/approve-remediation", headers=headers
        )
        assert invalid_approve.status_code == 409

        missing_json_auth = await client.get("/api/cases/apache/superset/5001")
        assert missing_json_auth.status_code == 401
        case_json = await client.get("/api/cases/apache/superset/5001", headers=headers)
        assert case_json.status_code == 200
        assert case_json.json()["state"] == CaseState.RECEIVED


@pytest.mark.asyncio
async def test_dashboard_auth_health_metrics_detail_and_throughput(
    test_app, integration_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await add_case(integration_session_factory, 5010, CaseState.CI_PASSED)
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        dashboard_redirect = await client.get("/")
        assert dashboard_redirect.status_code == 303
        assert dashboard_redirect.headers["location"] == "/login"

        dashboard = await client.get("/", cookies={"operator_token": "operator"})
        assert dashboard.status_code == 200

        unauthorized_metrics = await client.get("/metrics")
        assert unauthorized_metrics.status_code == 401
        metrics = await client.get("/metrics", headers={"Authorization": "Bearer operator"})
        assert metrics.status_code == 200
        assert "webhook_events_total" in metrics.text

        health = await client.get("/health")
        assert health.status_code == 200

        detail = await client.get(f"/cases/{case_id}", headers={"Authorization": "Bearer operator"})
        assert detail.status_code == 200
        assert "Scope is bounded." in detail.text

        throughput = await client.get(
            "/partials/throughput", headers={"Authorization": "Bearer operator"}
        )
        assert throughput.status_code == 200
        assert "Mean time to complete" in throughput.text
