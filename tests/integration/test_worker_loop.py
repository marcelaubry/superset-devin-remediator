import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import remediator.worker as worker_module
from remediator.config import Settings
from remediator.devin.fake import FakeDevinClient
from remediator.devin.tags import remediation_operation_key
from remediator.lifecycle import CaseState, transition
from remediator.models import Attempt, AttemptKind, AttemptStatus, Case, EventStatus, WebhookEvent
from remediator.worker import Worker, is_transient_db_error
from remediator.worker.devin_runner import allocate_attempt_ordinal
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

    async def fake_process_event(
        session, event, devin, settings, claimed_by=None, resolver=None, capacity=None
    ) -> None:
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
            assert fresh.state == CaseState.AWAITING_REMEDIATION_APPROVAL
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


@pytest.mark.asyncio
async def test_event_processing_releases_case_lease(
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    async with integration_session_factory() as session:
        session.add(
            WebhookEvent(
                delivery_id="lease-release",
                event_type="issues",
                action="opened",
                repository="apache/superset",
                payload=payload(4213),
                status=EventStatus.PENDING,
            )
        )
        await session.commit()

    worker = Worker(
        Settings(
            database_url=test_database_url,
            worker_poll_interval_seconds=0.01,
            worker_concurrency=1,
        )
    )

    async def stop_when_done() -> None:
        for _ in range(500):
            async with integration_session_factory() as session:
                status = await session.scalar(select(WebhookEvent.status))
            if status == EventStatus.PROCESSED:
                worker.stop()
                return
            await asyncio.sleep(0.01)
        worker.stop()

    try:
        await asyncio.wait_for(asyncio.gather(worker._run_loop(), stop_when_done()), timeout=10)
    finally:
        await worker.devin.aclose()
        await worker.engine.dispose()

    async with integration_session_factory() as session:
        case = await session.scalar(select(Case).where(Case.issue_number == 4213))
    assert case is not None
    assert case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    assert case.claimed_by is None
    assert case.lease_expires_at is None


def _deadlock() -> OperationalError:
    cause = asyncpg.DeadlockDetectedError("deadlock detected")
    orig = AsyncAdapt_asyncpg_dbapi.Error("deadlock detected")
    orig.__cause__ = cause
    return OperationalError("UPDATE cases ...", {}, orig)


def test_transient_db_error_classification() -> None:
    assert is_transient_db_error(_deadlock()) is True
    unique = AsyncAdapt_asyncpg_dbapi.Error("dup")
    unique.__cause__ = asyncpg.UniqueViolationError("dup")
    assert is_transient_db_error(IntegrityError("INSERT", {}, unique)) is False
    assert is_transient_db_error(RuntimeError("x")) is False


@pytest.mark.asyncio
async def test_deadlock_releases_the_job_for_retry_instead_of_failing_the_case(
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    """A deadlock/serialization failure says nothing about the case: the lease is released
    and the job is retried; the case must never be marked FAILED through a poisoned session."""
    event = WebhookEvent(
        delivery_id="loop-deadlock",
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=payload(4215),
        status=EventStatus.PENDING,
    )
    async with integration_session_factory() as session:
        session.add(event)
        await session.commit()

    settings = Settings(
        database_url=test_database_url, worker_poll_interval_seconds=0.01, worker_concurrency=1
    )
    worker = Worker(settings)
    calls = 0

    async def flaky_process_event(
        session, event, devin, settings, claimed_by=None, resolver=None, capacity=None
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _deadlock()
        event.status = EventStatus.PROCESSED
        event.processed_at = datetime.now(UTC)
        await session.commit()

    original = worker_module.process_event
    worker_module.process_event = flaky_process_event

    async def stop_when_done() -> None:
        for _ in range(500):
            async with integration_session_factory() as session:
                status = await session.scalar(select(WebhookEvent.status))
            if status == EventStatus.PROCESSED:
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
        stored = await session.scalar(select(WebhookEvent))
        assert stored is not None
        assert stored.status == EventStatus.PROCESSED
        assert stored.attempts_count == 2
        assert (await session.scalar(select(Case).where(Case.state == CaseState.FAILED))) is None
    assert calls == 2


@pytest.mark.asyncio
async def test_concurrent_ordinal_allocation_never_collides(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two workers retrying the same case at once must get distinct ordinals (case row lock),
    and the database must refuse a duplicate even if the lock were bypassed."""
    async with integration_session_factory() as session:
        case = Case(
            issue_number=4216,
            repository="apache/superset",
            issue_title="ordinal",
            issue_url="https://github.com/apache/superset/issues/4216",
            state=CaseState.RECEIVED,
        )
        session.add(case)
        await session.commit()
        case_id = case.id

    gate = asyncio.Barrier(4)

    async def allocate() -> int:
        async with integration_session_factory() as session:
            await gate.wait()
            ordinal = await allocate_attempt_ordinal(session, case_id, AttemptKind.REMEDIATION)
            # hold the lock across a yield so the others really do queue behind it
            await asyncio.sleep(0.05)
            session.add(
                Attempt(
                    case_id=case_id,
                    kind=AttemptKind.REMEDIATION,
                    ordinal=ordinal,
                    idempotency_key=f"{case_id}:REMEDIATION:{ordinal}",
                    operation_key=remediation_operation_key(case_id, "a" * 64, "b" * 40, ordinal),
                    status=AttemptStatus.FAILED,
                    finished_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return ordinal

    ordinals = await asyncio.wait_for(asyncio.gather(*(allocate() for _ in range(4))), timeout=10)
    assert sorted(ordinals) == [1, 2, 3, 4]

    async with integration_session_factory() as session:
        session.add(
            Attempt(
                case_id=case_id,
                kind=AttemptKind.REMEDIATION,
                ordinal=2,
                idempotency_key="bypassed-lock",
                operation_key="op:bypassed-lock",
                status=AttemptStatus.FAILED,
                finished_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError, match="uq_attempts_case_kind_ordinal"):
            await session.commit()


def test_remediation_operation_key_keeps_full_hashes() -> None:
    key = remediation_operation_key("case", "a" * 64, "b" * 40, 3)
    assert key.endswith(f":{'a' * 64}:{'b' * 40}:3")
    assert len(key) <= 255
    with pytest.raises(ValueError):
        remediation_operation_key("case", "a" * 12, "b" * 40, 1)


@pytest.mark.asyncio
async def test_sibling_loops_in_one_process_cannot_release_each_others_case_lease(
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    """Loops share a process id but must not share a claim identity: after loop 0 claims a
    case, loop 1 releasing "its" claim (e.g. after a webhook for the same case) must be a
    no-op, otherwise a third loop re-claims the case and two loops run it concurrently."""
    async with integration_session_factory() as session:
        case = Case(
            repository="apache/superset",
            issue_number=4999,
            issue_title="Fix issue",
            issue_url="https://github.com/apache/superset/issues/4999",
            state=CaseState.RECEIVED,
        )
        session.add(case)
        await session.commit()
        case_id = case.id

    worker = Worker(Settings(database_url=test_database_url, worker_concurrency=2))
    try:
        worker_module._loop_slot.set(0)
        claimed = await worker._claim_case()
        assert claimed is not None and claimed.id == case_id
        assert claimed.claimed_by == f"{worker.worker_id}/0"

        worker_module._loop_slot.set(1)
        assert worker.claim_id == f"{worker.worker_id}/1"
        await worker._release_case(case_id)
        assert await worker._claim_case() is None, "sibling loop released or re-claimed the case"
        await worker._heartbeat(case_id)

        async with integration_session_factory() as session:
            fresh = await session.get(Case, case_id)
            assert fresh is not None
            assert fresh.claimed_by == f"{worker.worker_id}/0"
            assert fresh.lease_expires_at is not None

        worker_module._loop_slot.set(0)
        await worker._release_case(case_id)
        async with integration_session_factory() as session:
            fresh = await session.get(Case, case_id)
            assert fresh is not None and fresh.claimed_by is None
    finally:
        worker_module._loop_slot.set(None)
        await worker.engine.dispose()


@pytest.mark.asyncio
async def test_run_exits_with_the_error_when_a_loop_dies_on_a_non_transient_failure(
    test_database_url: str,
) -> None:
    """A loop that raises something `_run_loop` does not retry (schema drift, programming
    error) must take the process down so the supervisor restarts it, instead of leaving a
    live-looking container with no loop claiming work."""
    worker = Worker(
        Settings(
            database_url=test_database_url,
            worker_poll_interval_seconds=0.01,
            worker_concurrency=2,
            worker_shutdown_timeout_seconds=2,
            worker_metrics_port=0,
        )
    )
    iterations = 0

    async def failing_iterate() -> None:
        nonlocal iterations
        iterations += 1
        if iterations == 1:
            raise ProgrammingError("SELECT 1", {}, Exception('relation "cases" does not exist'))
        await asyncio.sleep(0.01)

    worker._iterate = failing_iterate  # type: ignore[method-assign]
    with pytest.raises(ProgrammingError):
        await asyncio.wait_for(worker.run(), timeout=5)
    assert worker.stop_event.is_set()


@pytest.mark.asyncio
async def test_run_returns_cleanly_on_stop(test_database_url: str) -> None:
    worker = Worker(
        Settings(
            database_url=test_database_url,
            worker_poll_interval_seconds=0.01,
            worker_concurrency=1,
            worker_metrics_port=0,
        )
    )

    async def idle_iterate() -> None:
        await asyncio.sleep(0.01)

    worker._iterate = idle_iterate  # type: ignore[method-assign]

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        worker.stop()

    await asyncio.wait_for(asyncio.gather(worker.run(), stop_soon()), timeout=5)
