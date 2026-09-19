import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import remediator.worker as worker_module
from remediator.config import Settings
from remediator.devin.fake import FakeDevinClient
from remediator.lifecycle import CaseState, transition
from remediator.models import Case, EventStatus, WebhookEvent
from remediator.worker import Worker
from remediator.worker.processor import process_case


def payload(number: int) -> dict[str, object]:
    return {
        "action": "opened",
        "repository": {"full_name": "apache/superset"},
        "issue": {
            "number": number,
            "title": "Fix issue",
            "body": (
                "Steps to reproduce:\n1. Run.\nExpected behavior works. "
                "Actual behavior fails. Acceptance criteria: fixed. Similar existing pattern."
            ),
            "html_url": f"https://github.com/apache/superset/issues/{number}",
            "labels": [{"name": "bug"}, {"name": "devin-candidate"}],
        },
    }


@pytest.mark.asyncio
async def test_worker_loop_survives_failed_job(
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    first = WebhookEvent(
        delivery_id="loop-first",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=payload(4213),
        status=EventStatus.PENDING,
    )
    second = WebhookEvent(
        delivery_id="loop-second",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=payload(4214),
        status=EventStatus.PENDING,
    )
    async with integration_session_factory() as session:
        session.add_all([first, second])
        await session.commit()

    settings = Settings(
        database_url=test_database_url,
        worker_poll_interval_seconds=0.01,
        worker_concurrency=1,
    )
    worker = Worker(settings)
    calls = 0

    async def fake_process_event(session, event, devin, settings, claimed_by=None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected processor failure")
        event.status = EventStatus.PROCESSED
        event.processed_at = datetime.now(UTC)
        await session.commit()

    original = worker_module.process_event
    worker_module.process_event = fake_process_event

    async def stop_when_done() -> None:
        for _ in range(500):
            async with integration_session_factory() as session:
                statuses = list(
                    (
                        await session.scalars(
                            select(WebhookEvent.status).order_by(WebhookEvent.received_at)
                        )
                    ).all()
                )
            if statuses and all(status != EventStatus.PENDING for status in statuses):
                worker.stop()
                return
            await asyncio.sleep(0.01)
        worker.stop()

    try:
        await asyncio.wait_for(asyncio.gather(worker._run_loop(), stop_when_done()), timeout=5)
    finally:
        worker_module.process_event = original
        await worker.devin.aclose()
        await worker.engine.dispose()

    async with integration_session_factory() as session:
        events = list(
            (await session.scalars(select(WebhookEvent).order_by(WebhookEvent.received_at))).all()
        )
    assert [event.status for event in events] == [EventStatus.FAILED, EventStatus.PROCESSED]
    assert events[0].last_error == "injected processor failure"


@pytest.mark.asyncio
async def test_retry_case_is_claimed_and_processed(
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    async with integration_session_factory() as session:
        event = WebhookEvent(
            delivery_id="retry-source",
            event_type="issues",
            action="opened",
            repository="apache/superset",
            payload=payload(4213),
            status=EventStatus.PROCESSED,
        )
        case = Case(
            issue_number=4213,
            repository="apache/superset",
            issue_title="Fix issue",
            issue_url="https://github.com/apache/superset/issues/4213",
            state=CaseState.FAILED,
            state_entered_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        session.add_all([event, case])
        await session.flush()
        event.case_id = case.id
        await transition(session, case, CaseState.RECEIVED, "operator retry", "operator")
        case.state_entered_at = datetime.now(UTC) - timedelta(seconds=5)
        await session.commit()

    worker = Worker(
        Settings(
            database_url=test_database_url,
            worker_poll_interval_seconds=0.01,
            worker_concurrency=1,
        )
    )
    try:
        claimed = await worker._claim_case()
        assert claimed is not None
        async with integration_session_factory() as session:
            fresh = await session.get(Case, claimed.id)
            assert fresh is not None
            await process_case(session, fresh, FakeDevinClient(), worker.settings)
            assert fresh.state == CaseState.CI_PASSED
    finally:
        await worker.devin.aclose()
        await worker.engine.dispose()


@pytest.mark.asyncio
async def test_remediation_intent_case_is_claimed(
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    async with integration_session_factory() as session:
        event = WebhookEvent(
            delivery_id="remediation-intent-source",
            event_type="issues",
            action="opened",
            repository="apache/superset",
            payload=payload(4213),
            status=EventStatus.PROCESSED,
        )
        case = Case(
            issue_number=4213,
            repository="apache/superset",
            issue_title="Fix issue",
            issue_url="https://github.com/apache/superset/issues/4213",
            state=CaseState.REMEDIATION_CREATE_INTENT,
            state_entered_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        session.add_all([event, case])
        await session.flush()
        event.case_id = case.id
        await session.commit()

    worker = Worker(
        Settings(
            database_url=test_database_url,
            worker_poll_interval_seconds=0.01,
            worker_concurrency=1,
        )
    )
    try:
        claimed = await worker._claim_case()
        assert claimed is not None
        assert claimed.state == CaseState.REMEDIATION_CREATE_INTENT
    finally:
        await worker.devin.aclose()
        await worker.engine.dispose()
