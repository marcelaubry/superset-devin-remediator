"""Database-backed concurrency controls (Phase 5).

Everything here runs against real PostgreSQL: the guarantees under test (advisory-lock
serialisation, lease expiry, duplicate workers) do not exist in a mocked session.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remediator.capacity import (
    CapacityDenied,
    CapacityLimits,
    CapacityManager,
    waiting_count,
)
from remediator.config import Settings
from remediator.devin.client import CreateSessionRequest, SessionSnapshot
from remediator.devin.fake import FakeDevinClient
from remediator.lifecycle import CaseState
from remediator.models import (
    ACTIVE_ATTEMPT_STATUSES,
    Attempt,
    AttemptKind,
    AttemptStatus,
    CapacityLease,
    CapacityLeaseKind,
    Case,
)
from remediator.worker.processor import process_case

REPO = "apache/superset"


def limits(**overrides: int) -> CapacityLimits:
    base = {
        "triage": 2,
        "remediation": 1,
        "probes": 1,
        "remediation_per_repository": 1,
        "lease_seconds": 600,
    }
    base.update(overrides)
    return CapacityLimits(**base)


async def make_cases(
    factory: async_sessionmaker[AsyncSession],
    count: int,
    *,
    state: CaseState = CaseState.TRIAGE_CREATE_INTENT,
    first_issue: int = 7000,
    repository: str = REPO,
) -> list[uuid.UUID]:
    async with factory() as session:
        cases = [
            Case(
                issue_number=first_issue + index,
                repository=repository,
                issue_title=f"Fix issue {first_issue + index}",
                issue_url=f"https://github.com/{repository}/issues/{first_issue + index}",
                state=state,
                state_entered_at=datetime.now(UTC) - timedelta(seconds=5),
            )
            for index in range(count)
        ]
        session.add_all(cases)
        await session.commit()
        return [case.id for case in cases]


async def active_leases(session: AsyncSession, kind: CapacityLeaseKind) -> int:
    now = datetime.now(UTC)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(CapacityLease)
            .where(
                CapacityLease.kind == kind,
                CapacityLease.released_at.is_(None),
                CapacityLease.expires_at > now,
            )
        )
        or 0
    )


async def acquire_once(
    factory: async_sessionmaker[AsyncSession],
    manager: CapacityManager,
    case_id: uuid.UUID,
    *,
    kind: CapacityLeaseKind = CapacityLeaseKind.REMEDIATION,
    scope: str = REPO,
    per_scope_limit: int | None = None,
) -> bool:
    async with factory() as session, session.begin():
        outcome = await manager.acquire(
            session, kind=kind, case_id=case_id, scope=scope, per_scope_limit=per_scope_limit
        )
        return not isinstance(outcome, CapacityDenied)


@pytest.mark.asyncio
async def test_twenty_cases_queue_instead_of_exceeding_the_limit(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case_ids = await make_cases(integration_session_factory, 20)
    manager = CapacityManager(limits(remediation=1), owner="worker-a")

    granted = [await acquire_once(integration_session_factory, manager, cid) for cid in case_ids]
    assert granted.count(True) == 1 and granted.count(False) == 19

    # Re-acquiring for the holder is idempotent (renewal), never a second slot.
    assert await acquire_once(integration_session_factory, manager, case_ids[granted.index(True)])
    async with integration_session_factory() as session:
        assert await active_leases(session, CapacityLeaseKind.REMEDIATION) == 1

    # Raising the limit admits exactly that many more; the rest still queue.
    wider = CapacityManager(limits(remediation=4), owner="worker-a")
    granted = [await acquire_once(integration_session_factory, wider, cid) for cid in case_ids]
    async with integration_session_factory() as session:
        assert await active_leases(session, CapacityLeaseKind.REMEDIATION) == 4
    assert granted.count(True) == 4


@pytest.mark.asyncio
async def test_duplicate_workers_racing_cannot_exceed_the_limit(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case_ids = await make_cases(integration_session_factory, 20)
    workers = [CapacityManager(limits(triage=3), owner=f"worker-{n}") for n in range(4)]

    results = await asyncio.gather(
        *(
            acquire_once(
                integration_session_factory,
                workers[index % len(workers)],
                cid,
                kind=CapacityLeaseKind.TRIAGE,
            )
            for index, cid in enumerate(case_ids)
        )
    )
    assert results.count(True) == 3
    async with integration_session_factory() as session:
        assert await active_leases(session, CapacityLeaseKind.TRIAGE) == 3


@pytest.mark.asyncio
async def test_expired_leases_recover_and_holder_must_recompete(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first, second = await make_cases(integration_session_factory, 2)
    short = CapacityManager(limits(remediation=1, lease_seconds=1), owner="dead-worker")
    assert await acquire_once(integration_session_factory, short, first)
    assert not await acquire_once(integration_session_factory, short, second)

    await asyncio.sleep(1.1)  # the dead worker never heartbeats

    fresh = CapacityManager(limits(remediation=1), owner="worker-b")
    async with integration_session_factory() as session, session.begin():
        reclaimed = await fresh.expire_stale(session)
    assert reclaimed == 1
    assert await acquire_once(integration_session_factory, fresh, second)
    # The original holder's lease lapsed; it must queue behind the new holder.
    assert not await acquire_once(integration_session_factory, fresh, first)
    async with integration_session_factory() as session:
        reasons = set(
            (
                await session.scalars(
                    select(CapacityLease.release_reason).where(CapacityLease.case_id == first)
                )
            ).all()
        )
        assert reasons <= {
            "lease expired (holder presumed dead)",
            "lease expired before renewal",
        }


@pytest.mark.asyncio
async def test_heartbeat_keeps_a_live_lease_alive(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    (case_id,) = await make_cases(integration_session_factory, 1)
    manager = CapacityManager(limits(remediation=1, lease_seconds=1), owner="worker-a")
    assert await acquire_once(integration_session_factory, manager, case_id)
    for _ in range(3):
        await asyncio.sleep(0.5)
        async with integration_session_factory() as session, session.begin():
            await manager.heartbeat(session, [case_id])
    async with integration_session_factory() as session, session.begin():
        assert await manager.expire_stale(session) == 0
        assert await active_leases(session, CapacityLeaseKind.REMEDIATION) == 1


@pytest.mark.asyncio
async def test_per_repository_limit_and_resource_keys_serialize(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await make_cases(integration_session_factory, 2)
    (other,) = await make_cases(
        integration_session_factory, 1, first_issue=9000, repository="apache/other"
    )
    manager = CapacityManager(limits(remediation=5, remediation_per_repository=1), owner="w")
    assert await acquire_once(integration_session_factory, manager, a, per_scope_limit=1)
    assert not await acquire_once(integration_session_factory, manager, b, per_scope_limit=1)
    assert await acquire_once(
        integration_session_factory, manager, other, scope="apache/other", per_scope_limit=1
    )

    lockfile = "superset-frontend/package-lock.json"
    kind = CapacityLeaseKind.RESOURCE
    assert await acquire_once(integration_session_factory, manager, a, kind=kind, scope=lockfile)
    # Resource keys are mutexes whether or not the caller passes a per-scope limit.
    assert not await acquire_once(
        integration_session_factory, manager, b, kind=kind, scope=lockfile
    )
    assert await acquire_once(
        integration_session_factory, manager, b, kind=kind, scope="superset/migrations"
    )
    async with integration_session_factory() as session, session.begin():
        await manager.release(session, kind=kind, case_id=a, scope=lockfile, reason="done")
    assert await acquire_once(integration_session_factory, manager, b, kind=kind, scope=lockfile)


class CountingDevin(FakeDevinClient):
    """Fake Devin that records, at every create, how many attempts of the kind were live
    (RUNNING/RECONCILING/TERMINATION_PENDING) in the database including the new one."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self.factory = factory
        self.peak = 0

    async def create_session(self, request: CreateSessionRequest) -> SessionSnapshot:
        async with self.factory() as session:
            live = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Attempt)
                    .where(Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES))
                )
                or 0
            )
        self.peak = max(self.peak, live)
        return await super().create_session(request)


async def drive_until_settled(
    factory: async_sessionmaker[AsyncSession],
    case_ids: list[uuid.UUID],
    devin: FakeDevinClient,
    settings: Settings,
    manager: CapacityManager,
    *,
    max_rounds: int = 30,
) -> None:
    async def one(case_id: uuid.UUID) -> None:
        async with factory() as session:
            case = await session.get(Case, case_id)
            assert case is not None
            if case.state in {CaseState.TRIAGE_CREATE_INTENT, CaseState.TRIAGING}:
                await process_case(session, case, devin, settings, capacity=manager)

    for _ in range(max_rounds):
        await asyncio.gather(*(one(cid) for cid in case_ids))
        async with factory() as session:
            remaining = await session.scalar(
                select(func.count())
                .select_from(Case)
                .where(
                    Case.id.in_(case_ids),
                    Case.state.in_([CaseState.TRIAGE_CREATE_INTENT, CaseState.TRIAGING]),
                )
            )
        if not remaining:
            return
    raise AssertionError("cases did not settle within the round budget")


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 3])
async def test_twenty_case_simulation_respects_triage_limit_and_waits_spend_nothing(
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
    limit: int,
) -> None:
    case_ids = await make_cases(integration_session_factory, 20, first_issue=7100)
    settings = Settings(
        database_url=test_database_url,
        devin_poll_interval_seconds=0.01,
        max_concurrent_triage=limit,
    )
    devin = CountingDevin(integration_session_factory)
    manager = CapacityManager.from_settings(settings, "sim-worker")

    await drive_until_settled(integration_session_factory, case_ids, devin, settings, manager)

    assert devin.create_calls == 20, "every case eventually got exactly one triage session"
    assert 1 <= devin.peak <= limit
    if limit > 1:
        assert devin.peak > 1, "raising the limit must permit bounded parallelism"
    async with integration_session_factory() as session:
        assert await active_leases(session, CapacityLeaseKind.TRIAGE) == 0
        assert await waiting_count(session) == 0
        attempts = (
            await session.scalars(select(Attempt).where(Attempt.case_id.in_(case_ids)))
        ).all()
        assert len(attempts) == 20 and all(a.kind is AttemptKind.TRIAGE for a in attempts)
        states = set((await session.scalars(select(Case.state).where(Case.id.in_(case_ids)))).all())
        assert states <= {
            CaseState.AWAITING_REMEDIATION_APPROVAL,
            CaseState.POLICY_REJECTED,
            CaseState.HUMAN_BLOCKED,
            CaseState.TERMINATION_PENDING,
            CaseState.FAILED,
        }


@pytest.mark.asyncio
async def test_waiting_for_capacity_creates_no_session_and_no_attempt(
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    holder, waiter = await make_cases(integration_session_factory, 2, first_issue=7200)
    settings = Settings(
        database_url=test_database_url, devin_poll_interval_seconds=0.01, max_concurrent_triage=1
    )
    manager = CapacityManager.from_settings(settings, "worker-a")
    assert await acquire_once(
        integration_session_factory, manager, holder, kind=CapacityLeaseKind.TRIAGE
    )

    devin = FakeDevinClient()
    async with integration_session_factory() as session:
        case = await session.get(Case, waiter)
        assert case is not None
        await process_case(session, case, devin, settings, capacity=manager)
        await session.refresh(case)
        assert case.state == CaseState.TRIAGE_CREATE_INTENT
        assert case.waiting_for == "TRIAGE (1/1)"
        assert case.waiting_since is not None
        assert devin.create_calls == 0
        assert not (await session.scalars(select(Attempt).where(Attempt.case_id == waiter))).all()

    async with integration_session_factory() as session, session.begin():
        await manager.release(
            session, kind=CapacityLeaseKind.TRIAGE, case_id=holder, reason="holder finished"
        )
    async with integration_session_factory() as session:
        case = await session.get(Case, waiter)
        assert case is not None
        await process_case(session, case, devin, settings, capacity=manager)
        await session.refresh(case)
        assert devin.create_calls == 1
        assert case.waiting_for is None
        assert case.state != CaseState.TRIAGE_CREATE_INTENT


@pytest.mark.asyncio
async def test_cancel_releases_capacity_only_after_remote_termination_is_confirmed(
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    (case_id,) = await make_cases(
        integration_session_factory, 1, first_issue=7300, state=CaseState.TERMINATION_PENDING
    )
    settings = Settings(database_url=test_database_url, max_concurrent_triage=1)
    manager = CapacityManager.from_settings(settings, "worker-a")
    async with integration_session_factory() as session, session.begin():
        attempt = Attempt(
            case_id=case_id,
            kind=AttemptKind.TRIAGE,
            status=AttemptStatus.RUNNING,
            idempotency_key=f"idem-{case_id}",
            operation_key=f"op-{case_id}",
            devin_session_id="fake-triage-7300-unknown",
            started_at=datetime.now(UTC),
        )
        session.add(attempt)
        await session.flush()
        outcome = await manager.acquire(
            session,
            kind=CapacityLeaseKind.TRIAGE,
            case_id=case_id,
            scope=REPO,
            attempt_id=attempt.id,
        )
        assert isinstance(outcome, CapacityLease)

    flaky = FakeDevinClient(fail_terminate=True)
    async with integration_session_factory() as session:
        case = await session.get(Case, case_id)
        assert case is not None
        await process_case(session, case, flaky, settings, capacity=manager)
        await session.refresh(case)
        assert case.state == CaseState.TERMINATION_PENDING
        assert flaky.terminate_calls == ["fake-triage-7300-unknown"]
        assert await active_leases(session, CapacityLeaseKind.TRIAGE) == 1, (
            "unconfirmed termination must keep the slot: the remote session may still bill"
        )

    confirming = FakeDevinClient()  # unknown session → DevinSessionNotFound → confirmed gone
    async with integration_session_factory() as session:
        case = await session.get(Case, case_id)
        assert case is not None
        await process_case(session, case, confirming, settings, capacity=manager)
        await session.refresh(case)
        assert case.state == CaseState.CANCELLED
        assert await active_leases(session, CapacityLeaseKind.TRIAGE) == 0
        reasons = (
            await session.scalars(
                select(CapacityLease.release_reason).where(CapacityLease.case_id == case_id)
            )
        ).all()
        assert reasons == ["attempt failed"] or reasons == ["remote termination confirmed"]


@pytest.mark.asyncio
async def test_reconcile_finished_releases_leases_whose_attempt_is_over(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Worker crashed between settling the attempt and releasing: the next acquire by any
    worker sweeps the orphaned lease before counting."""
    crashed, waiting = await make_cases(integration_session_factory, 2, first_issue=7400)
    manager = CapacityManager(limits(remediation=1), owner="crashed-worker")
    async with integration_session_factory() as session, session.begin():
        attempt = Attempt(
            case_id=crashed,
            kind=AttemptKind.REMEDIATION,
            status=AttemptStatus.SUCCEEDED,
            finished_at=datetime.now(UTC),
            idempotency_key=f"idem-{crashed}",
            operation_key=f"op-{crashed}",
        )
        session.add(attempt)
        await session.flush()
        await manager.acquire(
            session,
            kind=CapacityLeaseKind.REMEDIATION,
            case_id=crashed,
            scope=REPO,
            attempt_id=attempt.id,
        )
    other = CapacityManager(limits(remediation=1), owner="worker-b")
    assert await acquire_once(integration_session_factory, other, waiting)
    async with integration_session_factory() as session:
        lease = await session.scalar(select(CapacityLease).where(CapacityLease.case_id == crashed))
        assert lease is not None and lease.release_reason == "attempt no longer active (reconciled)"
