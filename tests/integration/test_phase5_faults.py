"""Phase 5 fault injection that needs the real database: concurrent operator actions and
loss of the worker's database connections mid-loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import remediator.worker as worker_module
from remediator.config import Settings
from remediator.lifecycle import CaseState
from remediator.models import Case, EventStatus, StateTransition, WebhookEvent
from remediator.worker import Worker
from tests.integration.test_operator import add_case
from tests.integration.test_worker_loop import payload

HEADERS = {"Authorization": "Bearer operator"}


@pytest.mark.asyncio
async def test_concurrent_operator_retry_and_cancel_resolve_to_exactly_one_winner(
    test_app: FastAPI, integration_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two operators race on the same HUMAN_BLOCKED case. The compare-and-set in
    `transition` never lets both act on the same observed state: either the loser gets 409
    and the history shows one transition, or (when the ASGI transport happens to serialise
    the two requests) the second action legitimately starts from the first one's result,
    and the history is a chain. A lost update (two transitions both leaving HUMAN_BLOCKED)
    is the failure this guards against."""
    races = 0
    winners: list[str] = []
    for round_number in range(5):
        case_id = await add_case(
            integration_session_factory, 5100 + round_number, CaseState.HUMAN_BLOCKED
        )
        transport = httpx.ASGITransport(app=test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            retry, cancel = await asyncio.gather(
                client.post(f"/operator/cases/{case_id}/retry", headers=HEADERS),
                client.post(f"/operator/cases/{case_id}/cancel", headers=HEADERS),
            )
        statuses = sorted([retry.status_code, cancel.status_code])
        assert statuses in ([200, 409], [200, 200]), (retry.text, cancel.text)
        async with integration_session_factory() as session:
            case = await session.get(Case, case_id)
            assert case is not None
            transitions = (
                await session.execute(
                    select(StateTransition.from_state, StateTransition.to_state)
                    .where(StateTransition.case_id == case_id)
                    .order_by(StateTransition.seq)
                )
            ).all()
        chain = [(row[0], row[1]) for row in transitions]
        if statuses == [200, 409]:
            races += 1
            assert len(chain) == 1
            assert chain[0][0] == CaseState.HUMAN_BLOCKED
            assert case.state in {CaseState.RECEIVED, CaseState.CANCELLED}
        else:
            assert chain == [
                (CaseState.HUMAN_BLOCKED, CaseState.RECEIVED),
                (CaseState.RECEIVED, CaseState.CANCELLED),
            ], chain
            assert case.state == CaseState.CANCELLED
        assert chain[-1][1] == case.state
        assert sum(1 for source, _ in chain if source == CaseState.HUMAN_BLOCKED) == 1
        winners.append(case.state)
    assert set(winners) <= {CaseState.RECEIVED, CaseState.CANCELLED}
    print(f"genuine races observed: {races}/5")


@pytest.mark.asyncio
async def test_worker_loop_survives_losing_its_database_connections(
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    """`pg_terminate_backend` on every connection the worker holds stands in for a database
    restart. The loop must log, reconnect and keep processing; nothing is lost or doubled."""
    settings = Settings(
        database_url=test_database_url, worker_poll_interval_seconds=0.01, worker_concurrency=1
    )
    worker = Worker(settings)
    processed: list[str] = []
    killed = asyncio.Event()

    async def fake_process_event(
        session, event, devin, settings, claimed_by=None, resolver=None, capacity=None
    ) -> None:
        processed.append(event.delivery_id)
        event.status = EventStatus.PROCESSED
        event.processed_at = datetime.now(UTC)
        await session.commit()

    async def add_event(delivery_id: str, number: int) -> None:
        async with integration_session_factory() as session:
            session.add(
                WebhookEvent(
                    delivery_id=delivery_id,
                    event_type="issues",
                    action="opened",
                    repository="apache/superset",
                    payload=payload(number),
                    status=EventStatus.PENDING,
                )
            )
            await session.commit()

    async def chaos() -> None:
        await add_event("before-restart", 4301)
        for _ in range(300):
            if "before-restart" in processed:
                break
            await asyncio.sleep(0.01)
        assert "before-restart" in processed
        async with integration_session_factory() as session:
            await session.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE pid <> pg_backend_pid() AND datname = current_database() "
                    "AND application_name = :app"
                ).bindparams(app=f"remediator-worker {worker.worker_id}"[:63])
            )
            await session.commit()
        killed.set()
        await asyncio.sleep(0.1)
        await add_event("after-restart", 4302)
        for _ in range(500):
            if "after-restart" in processed:
                break
            await asyncio.sleep(0.01)
        worker.stop()

    original = worker_module.process_event
    worker_module.process_event = fake_process_event
    try:
        await asyncio.wait_for(asyncio.gather(worker._run_loop(), chaos()), timeout=15)
    finally:
        worker_module.process_event = original
        await worker.devin.aclose()
        await worker.engine.dispose()

    assert killed.is_set()
    assert processed == ["before-restart", "after-restart"]
    async with integration_session_factory() as session:
        statuses = (await session.scalars(select(WebhookEvent.status))).all()
    assert statuses and all(status == EventStatus.PROCESSED for status in statuses)
