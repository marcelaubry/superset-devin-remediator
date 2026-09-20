from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remediator.lifecycle import CaseState
from remediator.models import (
    UNRESOLVED_CREATE_ACK,
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    CreateState,
)


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
        assert cancel.json()["state"] == CaseState.TERMINATION_PENDING

        invalid_cancel = await client.post(
            f"/operator/cases/{cancelled_id}/cancel", headers=headers
        )
        assert invalid_cancel.status_code == 409

        hx_headers = {**headers, "HX-Request": "true"}
        hx_retry = await client.post(f"/operator/cases/{blocked_id}/retry", headers=hx_headers)
        assert hx_retry.status_code == 200
        assert "Elapsed in stage:" in hx_retry.text

        hx_cancel = await client.post(f"/operator/cases/{blocked_id}/cancel", headers=hx_headers)
        assert hx_cancel.status_code == 200
        assert "CANCELLED" in hx_cancel.text

        hx_terminal_cancel = await client.post(
            f"/operator/cases/{passed_id}/cancel", headers=hx_headers
        )
        assert hx_terminal_cancel.status_code == 200
        assert "CI_PASSED cannot transition to CANCELLED" in hx_terminal_cancel.text
        assert 'role="alert"' in hx_terminal_cancel.text

        # Phase 3: remediation approval is a Slack-only decision; the dashboard has no
        # approve/reject endpoints and cancel remains the only operator action.
        for case_id in (approval_id, reject_id):
            gone = await client.post(
                f"/operator/cases/{case_id}/approve-remediation", headers=headers
            )
            assert gone.status_code == 404
            gone = await client.post(
                f"/operator/cases/{case_id}/reject-remediation", headers=headers
            )
            assert gone.status_code == 404
        detail = await client.get(f"/cases/{approval_id}", headers=headers)
        assert detail.status_code == 200
        assert "approval happens in Slack" in detail.text
        assert "approve-remediation" not in detail.text

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

        login = await client.post("/login", data={"token": "operator"})
        assert login.status_code == 303
        dashboard = await client.get("/", cookies=login.cookies)
        assert dashboard.status_code == 200

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as anonymous:
            unauthorized_metrics = await anonymous.get("/metrics")
        assert unauthorized_metrics.status_code == 401
        metrics = await client.get("/metrics", headers={"Authorization": "Bearer operator"})
        assert metrics.status_code == 200
        assert "webhook_requests_total" in metrics.text

        health = await client.get("/health")
        assert health.status_code == 200

        detail = await client.get(f"/cases/{case_id}", headers={"Authorization": "Bearer operator"})
        assert detail.status_code == 200
        assert "Scope is bounded." in detail.text

        detail_partial = await client.get(
            f"/partials/case/{case_id}", headers={"Authorization": "Bearer operator"}
        )
        assert detail_partial.status_code == 200
        assert "Age:" in detail_partial.text

        cases_partial = await client.get(
            "/partials/cases", headers={"Authorization": "Bearer operator"}
        )
        assert cases_partial.status_code == 200
        assert f"/cases/{case_id}" in cases_partial.text

        throughput = await client.get(
            "/partials/throughput", headers={"Authorization": "Bearer operator"}
        )
        assert throughput.status_code == 200
        assert "mean time to terminal" in throughput.text

        tampered = await client.get("/", cookies={"operator_session": "9999999999.invalid"})
        assert tampered.status_code == 303
        expired = await client.get("/", cookies={"operator_session": "1.invalid"})
        assert expired.status_code == 303
        valid = login.cookies.get("operator_session")
        assert valid is not None
        assert (await client.get("/", cookies={"operator_session": valid})).status_code == 200
        logged_out = await client.post("/logout", cookies={"operator_session": valid})
        assert logged_out.status_code == 303


async def add_attempt(
    factory: async_sessionmaker[AsyncSession], case_id: UUID, **fields: object
) -> UUID:
    async with factory() as session:
        attempt = Attempt(
            case_id=case_id,
            kind=AttemptKind.TRIAGE,
            idempotency_key=f"{case_id}:TRIAGE:1",
            operation_key=f"op:{case_id}:TRIAGE:1",
            create_sent_at=datetime.now(UTC),
            **fields,  # type: ignore[arg-type]
        )
        session.add(attempt)
        await session.commit()
        return attempt.id


@pytest.mark.asyncio
async def test_operator_actions_respect_possibly_live_sessions(
    test_app, integration_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    waiting_id = await add_case(integration_session_factory, 5101, CaseState.HUMAN_BLOCKED)
    await add_attempt(
        integration_session_factory,
        waiting_id,
        create_state=CreateState.CREATED,
        status=AttemptStatus.BLOCKED,
        devin_session_id="devin-waiting",
    )
    unresolved_id = await add_case(integration_session_factory, 5102, CaseState.HUMAN_BLOCKED)
    unresolved_attempt = await add_attempt(
        integration_session_factory,
        unresolved_id,
        create_state=CreateState.UNRESOLVED,
        status=AttemptStatus.BLOCKED,
    )
    reconciling_id = await add_case(integration_session_factory, 5103, CaseState.RECONCILING_CREATE)
    await add_attempt(
        integration_session_factory,
        reconciling_id,
        create_state=CreateState.PENDING,
        status=AttemptStatus.RECONCILING,
    )
    headers = {"Authorization": "Bearer operator"}
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Cancelling a case whose blocked attempt still holds a session must terminate it.
        cancel = await client.post(f"/operator/cases/{waiting_id}/cancel", headers=headers)
        assert cancel.status_code == 200
        assert cancel.json()["state"] == CaseState.TERMINATION_PENDING

        # Cancelling while the create outcome is uncertain also goes through termination.
        cancel = await client.post(f"/operator/cases/{reconciling_id}/cancel", headers=headers)
        assert cancel.status_code == 200
        assert cancel.json()["state"] == CaseState.TERMINATION_PENDING

        # Retry after an unresolved create is refused until the operator confirms.
        refused = await client.post(f"/operator/cases/{unresolved_id}/retry", headers=headers)
        assert refused.status_code == 409
        assert "confirm_no_session=true" in refused.json()["detail"]
        async with integration_session_factory() as session:
            assert (await session.get(Case, unresolved_id)).state == CaseState.HUMAN_BLOCKED  # type: ignore[union-attr]

        confirmed = await client.post(
            f"/operator/cases/{unresolved_id}/retry?confirm_no_session=true", headers=headers
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["state"] == CaseState.RECEIVED
        async with integration_session_factory() as session:
            attempt = await session.get(Attempt, unresolved_attempt)
            assert attempt is not None
            assert attempt.reconciliation_reason == UNRESOLVED_CREATE_ACK
