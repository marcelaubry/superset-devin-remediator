import asyncio
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import build_engine, build_session_factory
from ..devin import build_devin_client
from ..lifecycle import CaseState, InvalidTransition
from ..models import Attempt, AttemptStatus, Case, EventStatus, WebhookEvent
from .processor import _terminate_running_attempts, fail_case, process_case, process_event

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = build_engine(settings)
        self.session_factory = build_session_factory(self.engine)
        self.devin = build_devin_client(settings)
        self.stop_event = asyncio.Event()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:6]}"

    async def _claim_event(self) -> WebhookEvent | None:
        while True:
            now = datetime.now(UTC)
            async with self.session_factory() as session:
                async with session.begin():
                    event = await session.scalar(
                        select(WebhookEvent)
                        .where(
                            (WebhookEvent.status == EventStatus.PENDING)
                            | (
                                (WebhookEvent.status == EventStatus.PROCESSING)
                                & (
                                    WebhookEvent.lease_expires_at.is_(None)
                                    | (WebhookEvent.lease_expires_at < now)
                                )
                            )
                        )
                        .order_by(WebhookEvent.received_at)
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                    if not event:
                        return None
                    event.attempts_count += 1
                    if event.attempts_count > self.settings.event_max_attempts:
                        event.status = EventStatus.FAILED
                        event.last_error = "exceeded EVENT_MAX_ATTEMPTS"
                        event.processed_at = now
                        if event.case_id:
                            case = await session.get(Case, event.case_id)
                            if case:
                                await _terminate_running_attempts(session, case, self.devin)
                                await fail_case(session, case, event.last_error, "worker")
                        continue
                    event.status = EventStatus.PROCESSING
                    event.claimed_at = now
                    event.claimed_by = self.worker_id
                    event.lease_expires_at = now + timedelta(
                        seconds=self.settings.worker_lease_seconds
                    )
                    logger.info("claimed webhook %s", event.delivery_id)
                    return event

    async def _claim(self) -> WebhookEvent | None:
        return await self._claim_event()

    async def _claim_case(self) -> Case | None:
        now = datetime.now(UTC)
        active_event = exists(
            select(WebhookEvent.id).where(
                WebhookEvent.case_id == Case.id,
                (
                    (WebhookEvent.status == EventStatus.PENDING)
                    | (
                        (WebhookEvent.status == EventStatus.PROCESSING)
                        & (WebhookEvent.lease_expires_at >= now)
                    )
                ),
            )
        )
        async with self.session_factory() as session:
            async with session.begin():
                case = await session.scalar(
                    select(Case)
                    .where(
                        Case.state.in_(
                            {
                                CaseState.RECEIVED,
                                CaseState.REMEDIATION_CREATE_INTENT,
                                CaseState.TERMINATION_PENDING,
                                CaseState.TRIAGE_CREATE_INTENT,
                            }
                        ),
                        (Case.lease_expires_at.is_(None) | (Case.lease_expires_at < now)),
                        ~active_event,
                    )
                    .order_by(Case.state_entered_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if case:
                    case.claimed_by = self.worker_id
                    case.lease_expires_at = now + timedelta(
                        seconds=self.settings.worker_lease_seconds
                    )
                    logger.info("claimed case %s in %s", case.id, case.state)
                return case

    async def _release_event(self, event_id: object) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.id == event_id, WebhookEvent.claimed_by == self.worker_id)
                .values(claimed_by=None, lease_expires_at=None)
            )
            await session.commit()

    async def _release_case(self, case_id: object) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(Case)
                .where(Case.id == case_id, Case.claimed_by == self.worker_id)
                .values(claimed_by=None, lease_expires_at=None)
            )
            await session.commit()

    async def _heartbeat(
        self, case_id: object | None = None, event_id: object | None = None
    ) -> None:
        expires = datetime.now(UTC) + timedelta(seconds=self.settings.worker_lease_seconds)
        async with self.session_factory() as session:
            if case_id is not None:
                result = cast(
                    Any,
                    await session.execute(
                        update(Case)
                        .where(Case.id == case_id, Case.claimed_by == self.worker_id)
                        .values(lease_expires_at=expires)
                    ),
                )
                if result.rowcount == 0:
                    logger.warning("case lease lost for %s", case_id)
            if event_id is not None:
                result = cast(
                    Any,
                    await session.execute(
                        update(WebhookEvent)
                        .where(
                            WebhookEvent.id == event_id, WebhookEvent.claimed_by == self.worker_id
                        )
                        .values(lease_expires_at=expires)
                    ),
                )
                if result.rowcount == 0:
                    logger.warning("event lease lost for %s", event_id)
            await session.commit()

    async def _heartbeat_loop(
        self, case_id: object | None = None, event_id: object | None = None
    ) -> None:
        while True:
            await asyncio.sleep(max(self.settings.worker_lease_seconds / 3, 0.01))
            await self._heartbeat(case_id, event_id)

    async def _run_job(self, event: WebhookEvent | None = None, case: Case | None = None) -> None:
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(case.id if case else None, event.id if event else None)
        )
        try:
            async with self.session_factory() as session:
                if event:
                    fresh = await session.get(WebhookEvent, event.id)
                    if fresh:
                        await process_event(
                            session, fresh, self.devin, self.settings, self.worker_id
                        )
                        logger.info("processed webhook %s", fresh.delivery_id)
                elif case:
                    fresh_case = await session.get(Case, case.id)
                    if fresh_case:
                        await process_case(
                            session, fresh_case, self.devin, self.settings, self.worker_id
                        )
                        logger.info("processed case %s in %s", case.id, fresh_case.state)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _mark_failed(
        self, session: AsyncSession, case_id: object, error: str, claimed_by: str | None = None
    ) -> None:
        case = await session.get(Case, case_id)
        if case:
            await fail_case(session, case, error, "worker", claimed_by)

    async def _run_loop(self) -> None:
        while not self.stop_event.is_set():
            event = await self._claim_event()
            case = None if event else await self._claim_case()
            if not event and not case:
                await asyncio.sleep(self.settings.worker_poll_interval_seconds)
                continue
            try:
                await self._run_job(event, case)
            except InvalidTransition as exc:
                logger.info("case changed concurrently, abandoning job: %s", exc)
                if event:
                    async with self.session_factory() as session:
                        fresh = await session.get(WebhookEvent, event.id)
                        if fresh:
                            fresh.status = EventStatus.PROCESSED
                            fresh.last_error = str(exc)
                            fresh.processed_at = datetime.now(UTC)
                            if fresh.case_id:
                                attempts = list(
                                    (
                                        await session.scalars(
                                            select(Attempt).where(
                                                Attempt.case_id == fresh.case_id,
                                                Attempt.status == AttemptStatus.RUNNING,
                                            )
                                        )
                                    ).all()
                                )
                                for attempt in attempts:
                                    attempt.status = AttemptStatus.CANCELLED
                                    attempt.error = "case changed concurrently"
                                    attempt.finished_at = datetime.now(UTC)
                            await session.commit()
                elif case:
                    async with self.session_factory() as session:
                        attempts = list(
                            (
                                await session.scalars(
                                    select(Attempt).where(
                                        Attempt.case_id == case.id,
                                        Attempt.status == AttemptStatus.RUNNING,
                                    )
                                )
                            ).all()
                        )
                        for attempt in attempts:
                            attempt.status = AttemptStatus.CANCELLED
                            attempt.error = "case changed concurrently"
                            attempt.finished_at = datetime.now(UTC)
                        await session.commit()
                    await self._release_case(case.id)
            except Exception as exc:
                logger.exception("job failed")
                if event:
                    async with self.session_factory() as session:
                        failed = await session.get(WebhookEvent, event.id)
                        if failed:
                            failed.status = EventStatus.FAILED
                            failed.last_error = str(exc)
                            failed.processed_at = datetime.now(UTC)
                            if failed.case_id:
                                await self._mark_failed(
                                    session, failed.case_id, str(exc), self.worker_id
                                )
                            await session.commit()
                elif case:
                    async with self.session_factory() as session:
                        await self._mark_failed(session, case.id, str(exc), self.worker_id)
                        await session.commit()
            finally:
                if event:
                    await self._release_event(event.id)
                if case:
                    await self._release_case(case.id)

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._run_loop()) for _ in range(self.settings.worker_concurrency)
        ]
        try:
            await self.stop_event.wait()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks), timeout=self.settings.worker_shutdown_timeout_seconds
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self.devin.aclose()
            await self.engine.dispose()

    def stop(self) -> None:
        self.stop_event.set()
