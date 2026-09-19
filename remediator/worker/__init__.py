import asyncio
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import build_github_client, build_slack_client
from ..config import Settings
from ..db import build_engine, build_session_factory
from ..devin import build_devin_client
from ..github_refs import build_base_commit_resolver
from ..lifecycle import CaseState, InvalidTransition
from ..models import (
    ACTIVE_ATTEMPT_STATUSES,
    Attempt,
    AttemptStatus,
    Case,
    EventStatus,
    WebhookEvent,
)
from .outbox import OutboxDispatcher
from .processor import _terminate_running_attempts, fail_case, process_case, process_event

CLAIMABLE_STATES = frozenset(
    {
        CaseState.RECEIVED,
        CaseState.TRIAGE_CREATE_INTENT,
        CaseState.TRIAGING,
        CaseState.RECONCILING_CREATE,
        CaseState.REMEDIATION_CREATE_INTENT,
        CaseState.REMEDIATING,
        CaseState.TERMINATION_PENDING,
    }
)

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = build_engine(settings)
        self.session_factory = build_session_factory(self.engine)
        self.devin = build_devin_client(settings)
        self.base_commits = build_base_commit_resolver(settings)
        self.stop_event = asyncio.Event()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:6]}"
        self.slack = build_slack_client(settings, self.session_factory)
        self.github = build_github_client(settings)
        self.outbox = OutboxDispatcher(
            settings, self.session_factory, self.slack, self.github, self.worker_id
        )

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
                        Case.state.in_(CLAIMABLE_STATES),
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
            state = await session.scalar(select(Case.state).where(Case.id == case_id))
            backoff: datetime | None = None
            if state == CaseState.TERMINATION_PENDING:
                backoff = datetime.now(UTC) + timedelta(
                    seconds=self.settings.devin_poll_interval_seconds
                )
            await session.execute(
                update(Case)
                .where(Case.id == case_id, Case.claimed_by == self.worker_id)
                .values(claimed_by=None, lease_expires_at=backoff)
            )
            await session.commit()

    async def _release_event_case(self, event_id: object) -> None:
        async with self.session_factory() as session:
            case_id = await session.scalar(
                select(WebhookEvent.case_id).where(WebhookEvent.id == event_id)
            )
        if case_id is not None:
            await self._release_case(case_id)

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
                            session,
                            fresh,
                            self.devin,
                            self.settings,
                            self.worker_id,
                            self.base_commits,
                        )
                        logger.info("processed webhook %s", fresh.delivery_id)
                elif case:
                    fresh_case = await session.get(Case, case.id)
                    if fresh_case:
                        await process_case(
                            session,
                            fresh_case,
                            self.devin,
                            self.settings,
                            self.worker_id,
                            self.base_commits,
                        )
                        logger.info("processed case %s in %s", case.id, fresh_case.state)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _cancel_unsent_attempts(self, session: AsyncSession, case_id: object) -> None:
        """Cancel active attempts that never sent a create.

        Attempts whose create was sent may own a live Devin session; they are left
        active so the case's current owner (or TERMINATION_PENDING recovery) can
        terminate or resume them instead of orphaning the session.
        """
        attempts = list(
            (
                await session.scalars(
                    select(Attempt).where(
                        Attempt.case_id == case_id,
                        Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                        Attempt.create_sent_at.is_(None),
                    )
                )
            ).all()
        )
        for attempt in attempts:
            attempt.status = AttemptStatus.CANCELLED
            attempt.error = "case changed concurrently"
            attempt.finished_at = datetime.now(UTC)

    async def _mark_failed(
        self, session: AsyncSession, case_id: object, error: str, claimed_by: str | None = None
    ) -> None:
        case = await session.get(Case, case_id)
        if case is None:
            return
        try:
            async with session.begin_nested():
                await fail_case(session, case, error, "worker", claimed_by)
        except InvalidTransition as exc:
            logger.warning("could not record failure for case %s: %s", case_id, exc)

    async def _run_loop(self) -> None:
        while not self.stop_event.is_set():
            event = await self._claim_event()
            case = None if event else await self._claim_case()
            if not event and not case:
                outbox_row = await self.outbox.claim()
                if outbox_row is not None:
                    await self.outbox.dispatch(outbox_row.id)
                    continue
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
                                await self._cancel_unsent_attempts(session, fresh.case_id)
                            await session.commit()
                elif case:
                    async with self.session_factory() as session:
                        await self._cancel_unsent_attempts(session, case.id)
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
                    await self._release_event_case(event.id)
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
            await self.base_commits.aclose()
            await self.slack.aclose()
            await self.github.aclose()
            await self.engine.dispose()

    def stop(self) -> None:
        self.stop_event.set()
