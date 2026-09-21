"""Triage lifecycle: structured output takes precedence over an idle session, and blocked
cases are reconciled by re-reading (GET) their retained session, never by creating another.

All flows run against PostgreSQL with the fake Devin/Slack clients; nothing live is called.
"""

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remediator.api.operator import get_devin_client
from remediator.approvals import triage_result_hash
from remediator.config import Settings
from remediator.devin.fake import FakeDevinClient, FakeScenario
from remediator.lifecycle import CaseState, transition
from remediator.models import (
    OUTBOX_KIND_SLACK_APPROVAL_REQUEST,
    RECONCILED_NO_OUTPUT_PREFIX,
    ApprovalRequest,
    Attempt,
    AttemptStatus,
    Case,
    NotificationOutbox,
    StateTransition,
)
from remediator.slack.blocks import NON_CANDIDATE_WARNING
from remediator.worker import Worker
from remediator.worker.processor import process_case, process_event, reconcile_blocked_triage
from tests.integration.test_phase2_triage import _event, _settings
from tests.integration.test_phase3_approval import Harness, harness, harness_factory  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db(integration_session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    async with integration_session_factory() as session:
        yield session


async def _attempts(session: AsyncSession, case: Case) -> list[Attempt]:
    return list(
        (
            await session.scalars(
                select(Attempt).where(Attempt.case_id == case.id).order_by(Attempt.started_at)
            )
        ).all()
    )


async def _approvals(session: AsyncSession, case: Case) -> list[ApprovalRequest]:
    return list(
        (
            await session.scalars(select(ApprovalRequest).where(ApprovalRequest.case_id == case.id))
        ).all()
    )


async def _slack_rows(session: AsyncSession, case: Case) -> list[NotificationOutbox]:
    return list(
        (
            await session.scalars(
                select(NotificationOutbox).where(
                    NotificationOutbox.case_id == case.id,
                    NotificationOutbox.kind == OUTBOX_KIND_SLACK_APPROVAL_REQUEST,
                )
            )
        ).all()
    )


async def _triage(
    db: AsyncSession, number: int, delivery: str, client: FakeDevinClient, **settings: object
) -> Case:
    event = _event(number, delivery)
    db.add(event)
    await db.commit()
    await process_event(db, event, client, _settings(**settings))
    case = await db.scalar(select(Case).where(Case.issue_number == number))
    assert case is not None
    return case


async def _assert_single_authoritative_round(
    db: AsyncSession, case: Case, attempt: Attempt, outcome: str
) -> None:
    assert case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    assert attempt.status == AttemptStatus.SUCCEEDED
    assert attempt.structured_output is not None
    assert attempt.structured_output["outcome"] == outcome
    approvals = await _approvals(db, case)
    assert len(approvals) == 1
    assert approvals[0].attempt_id == attempt.id
    assert approvals[0].triage_result_hash == triage_result_hash(attempt.structured_output)
    assert len(await _slack_rows(db, case)) == 1


# ------------------------------------------------------------- poll precedence


async def test_waiting_for_user_without_output_and_blocking_question_is_human_blocked(
    db: AsyncSession,
) -> None:
    """The genuine block: no structured output, session explicitly waiting for the user."""
    client = FakeDevinClient(scenarios={4213: FakeScenario.WAITING_FOR_HUMAN})
    case = await _triage(db, 4213, "reconcile-genuine-block", client)
    assert case.state == CaseState.HUMAN_BLOCKED
    (attempt,) = await _attempts(db, case)
    assert attempt.status == AttemptStatus.BLOCKED
    assert attempt.structured_output is None
    assert attempt.devin_session_id
    assert await _approvals(db, case) == []
    assert await _slack_rows(db, case) == []
    assert client.create_calls == 1


async def test_awaiting_instructions_with_valid_output_is_accepted_as_completed_triage(
    db: AsyncSession,
) -> None:
    """Idle status *with* structured output is a finished triage, never HUMAN_BLOCKED."""
    client = FakeDevinClient(scenarios={4213: FakeScenario.IDLE_WITH_OUTPUT})
    case = await _triage(db, 4213, "reconcile-idle-output", client)
    (attempt,) = await _attempts(db, case)
    await _assert_single_authoritative_round(db, case, attempt, "remediation_candidate")
    assert client.create_calls == 1
    states = await db.scalars(
        select(StateTransition.to_state).where(StateTransition.case_id == case.id)
    )
    assert CaseState.HUMAN_BLOCKED.value not in list(states.all())


async def test_valid_remediation_candidate_creates_exactly_one_approval_and_slack_row(
    db: AsyncSession,
) -> None:
    client = FakeDevinClient(scenarios={4213: FakeScenario.IDLE_WITH_OUTPUT})
    case = await _triage(db, 4213, "reconcile-one-approval", client)
    (attempt,) = await _attempts(db, case)
    await _assert_single_authoritative_round(db, case, attempt, "remediation_candidate")
    # A second pass over the settled case must not mint another approval or Slack row.
    await process_case(db, case, client, _settings())
    assert len(await _approvals(db, case)) == 1
    assert len(await _slack_rows(db, case)) == 1
    assert client.create_calls == 1


async def test_idle_needs_human_output_creates_warning_bearing_approval(
    harness: Harness,  # noqa: F811
) -> None:
    """Every schema-valid result goes to a human: a non-candidate recommendation submitted
    by an idle session still yields one approval whose Slack card carries the warning."""
    case = await harness.triage(4214, scenario=FakeScenario.IDLE_WITH_NEEDS_HUMAN_OUTPUT)
    assert case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    async with harness.factory() as session:
        (attempt,) = await _attempts(session, case)
        assert attempt.structured_output is not None
        assert attempt.structured_output["outcome"] == "needs_human"
        request = await harness.approval(case.id)
        assert request.triage_result_hash == triage_result_hash(attempt.structured_output)
        assert len(await _approvals(session, case)) == 1
    assert len(await harness.outbox(case.id, OUTBOX_KIND_SLACK_APPROVAL_REQUEST)) == 1
    assert await harness.drain() == 1
    messages = await harness.fake_messages()
    assert len(messages) == 1
    text = json.dumps(messages[0].blocks)
    assert "needs_human" in text
    assert NON_CANDIDATE_WARNING in text


async def test_malformed_output_with_waiting_status_fails_instead_of_blocking(
    db: AsyncSession,
) -> None:
    client = FakeDevinClient(scenarios={4213: FakeScenario.IDLE_WITH_MALFORMED_OUTPUT})
    case = await _triage(db, 4213, "reconcile-idle-malformed", client)
    assert case.state == CaseState.FAILED
    assert case.failure_reason and "structured output invalid" in case.failure_reason
    (attempt,) = await _attempts(db, case)
    assert attempt.status == AttemptStatus.FAILED
    assert await _approvals(db, case) == []
    assert await _slack_rows(db, case) == []


# --------------------------------------------------------- reconciliation paths


async def _blocked_case(
    db: AsyncSession, number: int, delivery: str, client: FakeDevinClient | None = None
) -> tuple[Case, Attempt, FakeDevinClient]:
    client = client or FakeDevinClient(scenarios={number: FakeScenario.WAITING_FOR_HUMAN})
    case = await _triage(db, number, delivery, client)
    assert case.state == CaseState.HUMAN_BLOCKED
    (attempt,) = await _attempts(db, case)
    assert attempt.devin_session_id
    return case, attempt, client


async def test_worker_restart_discovers_output_on_existing_blocked_session(
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    """A HUMAN_BLOCKED case with a retained session is claimable; processing it re-reads the
    session (GET) and, once output is there, advances the original attempt. No POST."""
    async with integration_session_factory() as session:
        case, attempt, client = await _blocked_case(session, 4213, "reconcile-worker-restart")
        session_id = attempt.devin_session_id
        operation_key = attempt.operation_key
        case_id = case.id

    worker = Worker(
        Settings(
            database_url=test_database_url,
            worker_poll_interval_seconds=0.01,
            worker_concurrency=1,
            human_blocked_reconcile_interval_seconds=0,
        )
    )
    try:
        # First pass: still no output. The case stays HUMAN_BLOCKED and is annotated.
        claimed = await worker._claim_case()
        assert claimed is not None and claimed.id == case_id
        async with integration_session_factory() as session:
            fresh = await session.get(Case, case_id)
            assert fresh is not None
            await process_case(session, fresh, client, worker.settings)
            assert fresh.state == CaseState.HUMAN_BLOCKED
            (only,) = await _attempts(session, fresh)
            assert only.status == AttemptStatus.BLOCKED
            assert (only.reconciliation_reason or "").startswith(RECONCILED_NO_OUTPUT_PREFIX)
        await worker._release_case(case_id)

        # The human answered in Devin; the same session now carries structured output.
        assert session_id is not None
        client.set_scenario(session_id, FakeScenario.IDLE_WITH_OUTPUT)
        claimed = await worker._claim_case()
        assert claimed is not None and claimed.id == case_id
        async with integration_session_factory() as session:
            fresh = await session.get(Case, case_id)
            assert fresh is not None
            await process_case(session, fresh, client, worker.settings)
            (only,) = await _attempts(session, fresh)
            assert only.devin_session_id == session_id
            assert only.operation_key == operation_key
            await _assert_single_authoritative_round(session, fresh, only, "remediation_candidate")
        assert client.create_calls == 1
        assert client.terminate_calls == []
        assert await worker._claim_case() is None  # settled cases are not reclaimed
    finally:
        await worker.devin.aclose()
        await worker.engine.dispose()


async def test_processing_a_blocked_case_without_output_keeps_it_blocked_and_never_posts(
    db: AsyncSession,
) -> None:
    case, attempt, client = await _blocked_case(db, 4213, "reconcile-still-blocked")
    outcome = await reconcile_blocked_triage(db, case, client, _settings())
    assert outcome is None
    assert case.state == CaseState.HUMAN_BLOCKED
    await db.refresh(attempt)
    assert attempt.status == AttemptStatus.BLOCKED
    assert (attempt.reconciliation_reason or "").startswith(RECONCILED_NO_OUTPUT_PREFIX)
    assert client.create_calls == 1
    assert client.terminate_calls == []


async def _legacy_duplicate(
    db: AsyncSession, case: Case, client: FakeDevinClient
) -> tuple[Attempt, Attempt]:
    """Reproduce what the old dashboard retry left behind: two blocked triage attempts for
    one case, each retaining its own (still existing) Devin session."""
    await transition(db, case, CaseState.RECEIVED, "legacy operator retry", "operator")
    await db.commit()
    await process_case(db, case, client, _settings())
    assert case.state == CaseState.HUMAN_BLOCKED
    attempts = await _attempts(db, case)
    assert len(attempts) == 2 and client.create_calls == 2
    first, second = attempts
    assert first.devin_session_id and second.devin_session_id
    assert first.devin_session_id != second.devin_session_id
    first.status = AttemptStatus.BLOCKED
    first.error = None
    await db.commit()
    client._sessions[first.devin_session_id].terminated = False
    client.terminate_calls.clear()
    return first, second


async def test_two_historical_attempts_reconcile_without_a_third(db: AsyncSession) -> None:
    """The bug left two blocked attempts, both with retained sessions. Reconciliation picks
    the newest valid output, supersedes the other, and creates no third attempt."""
    case, first, client = await _blocked_case(db, 4213, "reconcile-two-attempts")
    first, second = await _legacy_duplicate(db, case, client)

    # Both sessions completed and went idle; the newest one is authoritative.
    client.set_scenario(first.devin_session_id, FakeScenario.IDLE_WITH_NEEDS_HUMAN_OUTPUT)
    client.set_scenario(second.devin_session_id, FakeScenario.IDLE_WITH_OUTPUT)
    outcome = await reconcile_blocked_triage(db, case, client, _settings())
    assert outcome is not None
    await db.refresh(first)
    await db.refresh(second)
    assert len(await _attempts(db, case)) == 2
    assert client.create_calls == 2
    await _assert_single_authoritative_round(db, case, second, "remediation_candidate")
    assert first.status == AttemptStatus.CANCELLED
    assert first.error and "superseded" in first.error
    assert first.structured_output is None  # audit trail: the older attempt is untouched
    count = await db.scalar(
        select(func.count()).select_from(Attempt).where(Attempt.case_id == case.id)
    )
    assert count == 2


async def test_reconcile_prefers_newest_valid_output_over_newer_malformed(
    db: AsyncSession,
) -> None:
    case, first, client = await _blocked_case(db, 4213, "reconcile-prefer-valid")
    first, second = await _legacy_duplicate(db, case, client)
    assert first.devin_session_id and second.devin_session_id
    client.set_scenario(first.devin_session_id, FakeScenario.IDLE_WITH_OUTPUT)
    client.set_scenario(second.devin_session_id, FakeScenario.IDLE_WITH_MALFORMED_OUTPUT)
    outcome = await reconcile_blocked_triage(db, case, client, _settings())
    assert outcome is not None
    await db.refresh(first)
    await db.refresh(second)
    await _assert_single_authoritative_round(db, case, first, "remediation_candidate")
    assert second.status == AttemptStatus.CANCELLED
    assert client.create_calls == 2


async def test_duplicate_reconciliation_is_idempotent(db: AsyncSession) -> None:
    case, attempt, client = await _blocked_case(db, 4213, "reconcile-idempotent")
    assert attempt.devin_session_id
    client.set_scenario(attempt.devin_session_id, FakeScenario.IDLE_WITH_OUTPUT)
    assert await reconcile_blocked_triage(db, case, client, _settings()) is not None
    await db.refresh(attempt)
    await _assert_single_authoritative_round(db, case, attempt, "remediation_candidate")
    (approval,) = await _approvals(db, case)
    result_hash = approval.triage_result_hash

    for _ in range(3):
        assert await reconcile_blocked_triage(db, case, client, _settings()) is None
        await process_case(db, case, client, _settings())
    await db.refresh(attempt)
    (approval,) = await _approvals(db, case)
    assert approval.triage_result_hash == result_hash
    assert len(await _attempts(db, case)) == 1
    assert len(await _approvals(db, case)) == 1
    assert len(await _slack_rows(db, case)) == 1
    assert client.create_calls == 1
    assert client.terminate_calls == []


async def test_retry_does_not_replace_a_retained_session_that_has_output(
    db: AsyncSession,
) -> None:
    """Even through the legacy RECEIVED path, a retained session with output is never
    terminated and replaced: the case fails closed pointing at reconciliation."""
    case, attempt, client = await _blocked_case(db, 4213, "reconcile-no-replace")
    assert attempt.devin_session_id
    client.set_scenario(attempt.devin_session_id, FakeScenario.IDLE_WITH_OUTPUT)
    await transition(db, case, CaseState.RECEIVED, "legacy operator retry", "operator")
    await db.commit()
    await process_case(db, case, client, _settings())
    assert client.create_calls == 1
    assert client.terminate_calls == []
    assert case.state == CaseState.FAILED
    assert case.failure_reason and "reconcile the existing session" in case.failure_reason
    assert len(await _attempts(db, case)) == 1


# ------------------------------------------------------------------- operator


@pytest.fixture
async def operator(
    test_app: Any, integration_session_factory: async_sessionmaker[AsyncSession]
) -> Any:
    client = FakeDevinClient(scenarios={4213: FakeScenario.WAITING_FOR_HUMAN})
    test_app.dependency_overrides[get_devin_client] = lambda: client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app), base_url="http://test"
    ) as http:
        yield http, client, integration_session_factory
    test_app.dependency_overrides.pop(get_devin_client, None)


async def test_operator_reconcile_performs_get_and_never_post(operator: Any) -> None:
    http, client, factory = operator
    headers = {"Authorization": "Bearer operator"}
    async with factory() as session:
        case, attempt, _ = await _blocked_case(session, 4213, "reconcile-operator", client)
        case_id, session_id = case.id, attempt.devin_session_id
    assert session_id
    polls_before = client._sessions[session_id].polls

    # Ordinary retry is refused while the retained session has not been reconciled.
    refused = await http.post(f"/operator/cases/{case_id}/retry", headers=headers)
    assert refused.status_code == 409
    assert "Reconcile existing session" in refused.json()["detail"]

    # Still no output: GET happened, nothing created, case stays blocked.
    response = await http.post(f"/operator/cases/{case_id}/reconcile-session", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == CaseState.HUMAN_BLOCKED
    assert "no structured output yet" in response.json()["detail"]
    assert client._sessions[session_id].polls == polls_before + 1
    assert client.create_calls == 1

    client.set_scenario(session_id, FakeScenario.IDLE_WITH_OUTPUT)
    response = await http.post(f"/operator/cases/{case_id}/reconcile-session", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == CaseState.AWAITING_REMEDIATION_APPROVAL
    assert client.create_calls == 1
    assert client.terminate_calls == []
    async with factory() as session:
        fresh = await session.get(Case, case_id)
        assert fresh is not None
        (only,) = await _attempts(session, fresh)
        assert only.devin_session_id == session_id
        await _assert_single_authoritative_round(session, fresh, only, "remediation_candidate")

    # Repeating the action is a no-op once the case has left HUMAN_BLOCKED.
    again = await http.post(f"/operator/cases/{case_id}/reconcile-session", headers=headers)
    assert again.status_code == 409
    async with factory() as session:
        fresh = await session.get(Case, case_id)
        assert fresh is not None
        assert len(await _approvals(session, fresh)) == 1
        assert len(await _slack_rows(session, fresh)) == 1
    assert client.create_calls == 1


async def test_dashboard_offers_reconcile_not_retry_for_retained_session(operator: Any) -> None:
    http, client, factory = operator
    headers = {"Authorization": "Bearer operator"}
    async with factory() as session:
        case, attempt, _ = await _blocked_case(session, 4213, "reconcile-dashboard", client)
        case_id = case.id
    page = await http.get(f"/cases/{case_id}", headers=headers)
    assert page.status_code == 200, page.text
    assert "Reconcile existing session" in page.text
    assert f"/operator/cases/{case_id}/reconcile-session" in page.text
    assert f'action="/operator/cases/{case_id}/retry"' not in page.text

    # Once the retained session is proven empty, a replacement retry becomes available.
    response = await http.post(f"/operator/cases/{case_id}/reconcile-session", headers=headers)
    assert response.status_code == 200
    page = await http.get(f"/cases/{case_id}", headers=headers)
    assert "Reconcile existing session" in page.text
    assert "Retry (replacement triage)" in page.text
    assert client.create_calls == 1
