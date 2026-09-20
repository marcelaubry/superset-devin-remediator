import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from remediator.config import Settings
from remediator.devin.client import CreateSessionRequest, SessionSnapshot
from remediator.devin.fake import FakeDevinClient
from remediator.devin.tags import correlation_tags, operation_key
from remediator.devin.triage import TRIAGE_OUTPUT_SCHEMA
from remediator.lifecycle import CaseState, InvalidTransition, transition
from remediator.models import (
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    EventStatus,
    NotificationOutbox,
    OutboxChannel,
    Recommendation,
    StateTransition,
    WebhookEvent,
)
from remediator.worker import Worker
from remediator.worker.processor import process_case, process_event


@pytest.fixture
async def integration_session(test_database_url: str, database_available: bool) -> AsyncSession:
    engine = create_async_engine(test_database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE notification_outbox, state_transitions, attempts, "
                "webhook_events, cases CASCADE"
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def seed_remediation_session(
    client: FakeDevinClient, case: Case, key: str
) -> SessionSnapshot:
    return await client.create_session(
        CreateSessionRequest(
            prompt="",
            repository=case.repository,
            base_sha="0" * 40,
            max_acu_limit=1,
            operation_key=key,
            tags=correlation_tags(case.repository, case.issue_number, "REMEDIATION", case.id, "x"),
            structured_output_schema=TRIAGE_OUTPUT_SCHEMA,
        )
    )


def payload(number: int, body: str, labels: list[str]) -> dict[str, object]:
    return {
        "action": "opened",
        "repository": {"full_name": "apache/superset"},
        "issue": {
            "number": number,
            "title": "Fix issue",
            "body": body,
            "html_url": f"https://github.com/apache/superset/issues/{number}",
            "labels": [{"name": label} for label in labels],
        },
    }


@pytest.mark.asyncio
async def test_processor_success(integration_session: AsyncSession) -> None:
    event = WebhookEvent(
        delivery_id="integration-good",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=payload(
            4213,
            (
                "Steps to reproduce:\n1. Run.\nExpected behavior works. "
                "Actual behavior fails. Acceptance criteria: fixed. Similar existing pattern."
            ),
            ["bug", "devin-candidate"],
        ),
        status=EventStatus.PENDING,
    )
    integration_session.add(event)
    await integration_session.commit()
    await process_event(integration_session, event, FakeDevinClient(), Settings())
    case = await integration_session.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    transitions = list(
        (
            await integration_session.scalars(
                select(StateTransition).where(StateTransition.case_id == case.id)
            )
        ).all()
    )
    assert transitions[-1].to_state == CaseState.AWAITING_REMEDIATION_APPROVAL
    assert event.status == EventStatus.PROCESSED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "number,expected", [(4515, CaseState.FAILED), (4529, CaseState.HUMAN_BLOCKED)]
)
async def test_processor_failure_modes(
    integration_session: AsyncSession, number: int, expected: CaseState
) -> None:
    body = (
        "Steps to reproduce:\n1. Run.\nExpected behavior works. "
        "Actual behavior fails. Acceptance criteria: fixed. Similar existing pattern."
    )
    event = WebhookEvent(
        delivery_id=f"integration-{number}",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=payload(number, body, ["bug", "devin-candidate"]),
        status=EventStatus.PENDING,
    )
    integration_session.add(event)
    await integration_session.commit()
    await process_event(integration_session, event, FakeDevinClient(), Settings())
    case = await integration_session.scalar(select(Case).where(Case.issue_number == number))
    assert case and case.state == expected


@pytest.mark.asyncio
async def test_processor_rejection(integration_session: AsyncSession) -> None:
    event = WebhookEvent(
        delivery_id="integration-rejected",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=payload(4321, "It is broken.", ["devin-candidate"]),
        status=EventStatus.PENDING,
    )
    integration_session.add(event)
    await integration_session.commit()
    await process_event(integration_session, event, FakeDevinClient(), Settings())
    case = await integration_session.scalar(select(Case).where(Case.issue_number == 4321))
    assert case and case.state == CaseState.POLICY_REJECTED
    assert (
        await integration_session.scalar(
            select(func.count()).select_from(Attempt).where(Attempt.case_id == case.id)
        )
        == 0
    )
    assert (
        await integration_session.scalar(
            select(func.count())
            .select_from(NotificationOutbox)
            .where(
                NotificationOutbox.case_id == case.id,
                NotificationOutbox.channel == OutboxChannel.SLACK,
            )
        )
        == 0
    )


@pytest.mark.asyncio
async def test_unlabeled_opened_issue_is_evaluated_and_only_remediate_label_remediates(
    integration_session: AsyncSession,
) -> None:
    """Intake needs no label: an unlabeled `opened` issue is triaged (zero-ACU eligibility,
    then the triage session) and parks at approval. `labeled` deliveries for other labels are
    inert, and `devin:remediate` without a delivered Slack approval never starts remediation."""
    settings = Settings(_env_file=None)
    assert settings.github_required_label == ""
    body = (
        "Steps to reproduce:\n1. Run.\nExpected behavior works. "
        "Actual behavior fails. Acceptance criteria: fixed. Similar existing pattern."
    )
    opened = WebhookEvent(
        delivery_id="integration-unlabeled-opened",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=payload(4222, body, []),
        status=EventStatus.PENDING,
    )
    integration_session.add(opened)
    await integration_session.commit()
    await process_event(integration_session, opened, FakeDevinClient(), settings)
    case = await integration_session.scalar(select(Case).where(Case.issue_number == 4222))
    assert case and case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    assert opened.status == EventStatus.PROCESSED

    for delivery, label in (
        ("integration-bug-label", "bug"),
        ("integration-rem", "devin:remediate"),
    ):
        labeled_payload = payload(4222, body, [label])
        labeled_payload["label"] = {"name": label}
        labeled = WebhookEvent(
            delivery_id=delivery,
            event_type="issues",
            action="labeled",
            repository="apache/superset",
            payload=labeled_payload,
            status=EventStatus.PENDING,
        )
        integration_session.add(labeled)
        await integration_session.commit()
        await process_event(integration_session, labeled, FakeDevinClient(), settings)
        await integration_session.refresh(case)
        assert labeled.status == EventStatus.PROCESSED
        assert case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    assert labeled.last_error == "label webhook without matching approval"
    remediation_attempts = await integration_session.scalar(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.case_id == case.id, Attempt.kind == AttemptKind.REMEDIATION)
    )
    assert remediation_attempts == 0


@pytest.mark.asyncio
async def test_processor_triage_infeasible(integration_session: AsyncSession) -> None:
    event = WebhookEvent(
        delivery_id="integration-triage-infeasible",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=payload(
            4533,
            (
                "Steps to reproduce:\n1. Run.\nExpected behavior works. "
                "Actual behavior fails. Acceptance criteria: fixed. Similar existing pattern."
            ),
            ["bug", "devin-candidate"],
        ),
        status=EventStatus.PENDING,
    )
    integration_session.add(event)
    await integration_session.commit()
    await process_event(integration_session, event, FakeDevinClient(), Settings())
    case = await integration_session.scalar(select(Case).where(Case.issue_number == 4533))
    assert case and case.state == CaseState.POLICY_REJECTED
    attempts = list(
        (await integration_session.scalars(select(Attempt).where(Attempt.case_id == case.id))).all()
    )
    assert [attempt.kind for attempt in attempts] == [AttemptKind.TRIAGE]
    assert "triage outcome needs_human" in (
        await integration_session.scalar(
            select(StateTransition.reason)
            .where(
                StateTransition.case_id == case.id,
                StateTransition.to_state == CaseState.POLICY_REJECTED,
            )
            .order_by(StateTransition.created_at.desc())
        )
    )


@pytest.mark.asyncio
async def test_focused_frontend_bug_reaches_bounded_devin_triage(
    integration_session: AsyncSession,
) -> None:
    """The digit-only temporal formatting issue mentions the backend only as context and in
    its non-goals; the policy must admit it to triage instead of declaring HUMAN_LED."""
    fixture = json.loads(
        (Path(__file__).parents[2] / "fixtures/github/issue_digit_only_temporal.json").read_text()
    )
    number = fixture["issue"]["number"]
    event = WebhookEvent(
        delivery_id="integration-digit-only-temporal",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=fixture,
        status=EventStatus.PENDING,
    )
    integration_session.add(event)
    await integration_session.commit()
    await process_event(integration_session, event, FakeDevinClient(), Settings())
    case = await integration_session.scalar(select(Case).where(Case.issue_number == number))
    assert case and case.recommendation == Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE
    assert all(check["passed"] for check in case.rubric or [])
    states = list(
        (
            await integration_session.scalars(
                select(StateTransition.to_state)
                .where(StateTransition.case_id == case.id)
                .order_by(StateTransition.seq)
            )
        ).all()
    )
    assert states[:3] == [
        CaseState.ELIGIBILITY_EVALUATED,
        CaseState.TRIAGE_CREATE_INTENT,
        CaseState.TRIAGING,
    ]
    attempts = list(
        (await integration_session.scalars(select(Attempt).where(Attempt.case_id == case.id))).all()
    )
    assert [attempt.kind for attempt in attempts] == [AttemptKind.TRIAGE]
    # Only the (fake) Devin triage session decides remediation_candidate; the policy never does.
    assert case.state == CaseState.AWAITING_REMEDIATION_APPROVAL


@pytest.mark.asyncio
async def test_remediation_candidate_parks_awaiting_slack_approval(
    integration_session: AsyncSession,
) -> None:
    event = WebhookEvent(
        delivery_id="integration-manual-approval",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=payload(
            4213,
            (
                "Steps to reproduce:\n1. Run.\nExpected behavior works. "
                "Actual behavior fails. Acceptance criteria: fixed. Similar existing pattern."
            ),
            ["bug", "devin-candidate"],
        ),
        status=EventStatus.PENDING,
    )
    integration_session.add(event)
    await integration_session.commit()
    settings = Settings()
    devin = FakeDevinClient()
    await process_event(integration_session, event, devin, settings)
    case = await integration_session.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    outbox = list(
        (
            await integration_session.scalars(
                select(NotificationOutbox).where(NotificationOutbox.case_id == case.id)
            )
        ).all()
    )
    assert len(outbox) == 1
    assert outbox[0].channel == OutboxChannel.SLACK
    assert outbox[0].kind == "remediation_approval_requested"
    assert outbox[0].approval_request_id is not None
    # Nothing in Phase 3 may pick the case up as Devin work: no remediation session.
    await process_case(integration_session, case, devin, settings)
    assert case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    kinds = {
        a.kind
        for a in (
            await integration_session.scalars(select(Attempt).where(Attempt.case_id == case.id))
        ).all()
    }
    assert kinds == {AttemptKind.TRIAGE}


@pytest.mark.asyncio
async def test_reconcile_remediation_create_intent(
    integration_session: AsyncSession,
) -> None:
    event = WebhookEvent(
        delivery_id="integration-reconcile",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=payload(4213, "source", ["devin-candidate"]),
        status=EventStatus.PROCESSED,
    )
    case = Case(
        issue_number=4213,
        repository="apache/superset",
        issue_title="Fix issue",
        issue_url="https://github.com/apache/superset/issues/4213",
        state=CaseState.REMEDIATION_CREATE_INTENT,
    )
    integration_session.add_all([event, case])
    await integration_session.flush()
    event.case_id = case.id
    key = operation_key(case.id, "REMEDIATION", 1)
    attempt = Attempt(
        case_id=case.id,
        kind=AttemptKind.REMEDIATION,
        idempotency_key=f"{case.id}:REMEDIATION:1",
        operation_key=key,
        status=AttemptStatus.RUNNING,
    )
    integration_session.add(attempt)
    await integration_session.commit()
    client = FakeDevinClient()
    await seed_remediation_session(client, case, key)
    await process_case(integration_session, case, client, Settings())
    # The uncertain create is reconciled by exact tag and the paid session is observed to
    # completion without a second POST. Because this legacy attempt carries no Phase 4
    # dispatch context (approval + immutable probe snapshot) the pipeline then fails closed
    # instead of trusting the session's output.
    assert client.create_calls == 1
    assert case.state == CaseState.REMEDIATION_FAILED
    assert case.failure_reason and "probe snapshot" in case.failure_reason
    transitions = list(
        (
            await integration_session.scalars(
                select(StateTransition)
                .where(StateTransition.case_id == case.id)
                .order_by(StateTransition.seq)
            )
        ).all()
    )
    states = [t.to_state for t in transitions]
    assert CaseState.REMEDIATION_RECONCILING_CREATE in states
    assert states.index(CaseState.REMEDIATING) > states.index(
        CaseState.REMEDIATION_RECONCILING_CREATE
    )
    assert CaseState.OUTPUT_VALIDATING in states


@pytest.mark.asyncio
async def test_reconcile_missing_session_fails(
    integration_session: AsyncSession,
) -> None:
    case = Case(
        issue_number=4213,
        repository="apache/superset",
        issue_title="Fix issue",
        issue_url="https://github.com/apache/superset/issues/4213",
        state=CaseState.REMEDIATION_CREATE_INTENT,
    )
    integration_session.add(case)
    await integration_session.flush()
    integration_session.add(
        Attempt(
            case_id=case.id,
            kind=AttemptKind.REMEDIATION,
            idempotency_key=f"{case.id}:REMEDIATION:1",
            operation_key=operation_key(case.id, "REMEDIATION", 1),
            status=AttemptStatus.RUNNING,
        )
    )
    await integration_session.commit()
    client = FakeDevinClient()
    await process_case(integration_session, case, client, Settings(reconcile_retry_delay_seconds=0))
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED
    assert case.failure_reason and "create outcome unknown" in case.failure_reason
    assert client.create_calls == 0


@pytest.mark.asyncio
async def test_poll_budget_times_out(
    integration_session: AsyncSession,
) -> None:
    event = WebhookEvent(
        delivery_id="integration-timeout",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=payload(
            4213,
            (
                "Steps to reproduce:\n1. Run. Expected behavior works. "
                "Actual behavior fails. Acceptance criteria: fixed. Similar existing pattern."
            ),
            ["bug", "devin-candidate"],
        ),
        status=EventStatus.PENDING,
    )
    integration_session.add(event)
    await integration_session.commit()
    settings = Settings(devin_triage_timeout_seconds=0.05, devin_poll_interval_seconds=0)
    client = FakeDevinClient(never_finish_issues={4213})
    await process_event(integration_session, event, client, settings)
    case = await integration_session.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.TIMED_OUT
    attempt = await integration_session.scalar(select(Attempt).where(Attempt.case_id == case.id))
    assert attempt and attempt.status == AttemptStatus.TIMED_OUT
    assert attempt.error and "session terminated remotely" in attempt.error
    assert client.terminate_calls == [attempt.devin_session_id]


@pytest.mark.asyncio
async def test_transition_rejects_wrong_lease_owner(
    integration_session: AsyncSession,
) -> None:
    case = Case(
        issue_number=4801,
        repository="apache/superset",
        issue_title="Lease",
        issue_url="https://github.com/apache/superset/issues/4801",
        state=CaseState.RECEIVED,
        claimed_by="worker-a",
    )
    integration_session.add(case)
    await integration_session.commit()
    case_id = case.id
    with pytest.raises(InvalidTransition, match="lease lost"):
        await transition(
            integration_session,
            case,
            CaseState.ELIGIBILITY_EVALUATED,
            "test",
            "worker",
            expected_claimed_by="worker-b",
        )
    await integration_session.rollback()
    fresh = await integration_session.get(Case, case_id)
    assert fresh and fresh.state == CaseState.RECEIVED


@pytest.mark.asyncio
async def test_worker_heartbeat_renews_case_lease(
    integration_session: AsyncSession,
    test_database_url: str,
) -> None:
    case = Case(
        issue_number=4802,
        repository="apache/superset",
        issue_title="Heartbeat",
        issue_url="https://github.com/apache/superset/issues/4802",
        state=CaseState.RECEIVED,
    )
    integration_session.add(case)
    await integration_session.commit()
    worker = Worker(Settings(database_url=test_database_url, worker_lease_seconds=30))
    try:
        async with integration_session.begin():
            case.claimed_by = worker.worker_id
            case.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        before = case.lease_expires_at
        await worker._heartbeat(case_id=case.id)
    finally:
        await worker.devin.aclose()
        await worker.engine.dispose()
    await integration_session.refresh(case)
    assert case.lease_expires_at and case.lease_expires_at > before


@pytest.mark.asyncio
async def test_reconcile_retries_missing_session(
    integration_session: AsyncSession,
) -> None:
    case = Case(
        issue_number=4803,
        repository="apache/superset",
        issue_title="Reconcile",
        issue_url="https://github.com/apache/superset/issues/4803",
        state=CaseState.REMEDIATION_CREATE_INTENT,
        claimed_by="worker",
    )
    integration_session.add(case)
    await integration_session.flush()
    key = operation_key(case.id, "REMEDIATION", 1)
    integration_session.add(
        Attempt(
            case_id=case.id,
            kind=AttemptKind.REMEDIATION,
            idempotency_key=f"{case.id}:REMEDIATION:1",
            operation_key=key,
            status=AttemptStatus.RUNNING,
        )
    )
    await integration_session.commit()

    client = FakeDevinClient()
    await seed_remediation_session(client, case, key)

    class RetryClient(FakeDevinClient):
        def __init__(self, source: FakeDevinClient) -> None:
            super().__init__()
            self._sessions = source._sessions
            self._by_operation = source._by_operation
            self.calls = 0

        async def find_sessions_by_tag(self, tag: str) -> list[SessionSnapshot]:
            self.calls += 1
            if self.calls == 1:
                return []
            return await super().find_sessions_by_tag(tag)

    retry_client = RetryClient(client)
    await process_case(
        integration_session,
        case,
        retry_client,
        Settings(reconcile_retry_delay_seconds=0),
        claimed_by="worker",
    )
    assert retry_client.calls == 2
    assert retry_client.create_calls == 0
    assert case.state == CaseState.REMEDIATION_FAILED
    assert case.failure_reason and "probe snapshot" in case.failure_reason


@pytest.mark.asyncio
async def test_create_orphan_is_terminated(
    integration_session: AsyncSession,
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case = Case(
        issue_number=4804,
        repository="apache/superset",
        issue_title="Orphan",
        issue_url="https://github.com/apache/superset/issues/4804",
        state=CaseState.TRIAGE_CREATE_INTENT,
        claimed_by="worker",
    )
    integration_session.add(case)
    await integration_session.commit()
    case_id = case.id
    client = FakeDevinClient()
    terminated: list[str] = []
    original_create = client.create_session

    async def create_and_steal(request: CreateSessionRequest) -> SessionSnapshot:
        created = await original_create(request)
        async with integration_session_factory() as other:
            await other.execute(update(Case).where(Case.id == case_id).values(claimed_by="other"))
            await other.commit()
        return created

    async def terminate(session_id: str) -> SessionSnapshot | None:
        terminated.append(session_id)
        return None

    client.create_session = create_and_steal
    client.terminate_session = terminate
    with pytest.raises(InvalidTransition, match="lease lost"):
        await process_case(
            integration_session,
            case,
            client,
            Settings(),
            claimed_by="worker",
        )
    attempt = await integration_session.scalar(select(Attempt).where(Attempt.case_id == case_id))
    assert attempt and attempt.status == AttemptStatus.CANCELLED
    assert attempt.devin_session_id in terminated
