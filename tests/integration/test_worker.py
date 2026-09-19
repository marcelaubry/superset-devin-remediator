import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from remediator.approvals import approve_remediation
from remediator.config import Settings
from remediator.devin.fake import FakeDevinClient
from remediator.lifecycle import CaseState
from remediator.models import (
    Attempt,
    AttemptKind,
    Case,
    EventStatus,
    NotificationOutbox,
    OutboxChannel,
    StateTransition,
    WebhookEvent,
)
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
    assert case and case.state == CaseState.CI_PASSED
    transitions = list(
        (
            await integration_session.scalars(
                select(StateTransition).where(StateTransition.case_id == case.id)
            )
        ).all()
    )
    assert transitions[-1].to_state == CaseState.CI_PASSED
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
    assert "triage: remediation not feasible" in (
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
async def test_manual_remediation_approval_resumes_processing(
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
    settings = Settings(simulation_auto_approve_remediation=False)
    await process_event(integration_session, event, FakeDevinClient(), settings)
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
    await approve_remediation(integration_session, case, "operator")
    await integration_session.commit()
    await process_case(integration_session, case, FakeDevinClient(), settings)
    assert case.state == CaseState.CI_PASSED
