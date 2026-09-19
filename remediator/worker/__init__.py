import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import build_engine, build_session_factory
from ..devin import build_devin_client
from ..lifecycle import CaseState, transition
from ..models import Case, EventStatus, WebhookEvent
from .processor import process_case, process_event

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = build_engine(settings)
        self.session_factory = build_session_factory(self.engine)
        self.devin = build_devin_client(settings)
        self.stop_event = asyncio.Event()

    async def _claim(self) -> WebhookEvent | None:
        async with self.session_factory() as session:
            async with session.begin():
                event = await session.scalar(
                    select(WebhookEvent)
                    .where(WebhookEvent.status == EventStatus.PENDING)
                    .order_by(WebhookEvent.received_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if event:
                    event.status = EventStatus.PROCESSING
                    event.claimed_at = datetime.now(UTC)
                    event.attempts_count += 1
            return event

    async def _claim_case(self) -> Case | None:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.worker_poll_interval_seconds)
        pending_event = exists(
            select(WebhookEvent.id).where(
                WebhookEvent.case_id == Case.id,
                WebhookEvent.status.in_({EventStatus.PENDING, EventStatus.PROCESSING}),
            )
        )
        async with self.session_factory() as session:
            async with session.begin():
                case = await session.scalar(
                    select(Case)
                    .where(
                        Case.state == CaseState.RECEIVED,
                        Case.state_entered_at < cutoff,
                        ~pending_event,
                    )
                    .order_by(Case.state_entered_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if case:
                    case.state_entered_at = datetime.now(UTC)
            return case

    async def _mark_failed(self, session: AsyncSession, case_id: object, error: str) -> None:
        case = await session.get(Case, case_id)
        if case and CaseState(case.state) not in {
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.POLICY_REJECTED,
            CaseState.CI_PASSED,
        }:
            await transition(session, case, CaseState.FAILED, error, "worker")

    async def _run_loop(self) -> None:
        while not self.stop_event.is_set():
            event = await self._claim()
            case = None if event else await self._claim_case()
            if not event and not case:
                await asyncio.sleep(self.settings.worker_poll_interval_seconds)
                continue
            try:
                async with self.session_factory() as session:
                    if event:
                        fresh = await session.get(WebhookEvent, event.id)
                        if fresh:
                            await process_event(session, fresh, self.devin, self.settings)
                    elif case:
                        fresh_case = await session.get(Case, case.id)
                        if fresh_case:
                            await process_case(session, fresh_case, self.devin, self.settings)
            except Exception as exc:
                logger.exception("job failed")
                if event:
                    async with self.session_factory() as session:
                        failed = await session.get(WebhookEvent, event.id)
                        if failed:
                            failed.status = EventStatus.FAILED
                            failed.last_error = str(exc)
                            if failed.case_id:
                                await self._mark_failed(session, failed.case_id, str(exc))
                            await session.commit()
                elif case:
                    async with self.session_factory() as session:
                        await self._mark_failed(session, case.id, str(exc))
                        await session.commit()

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._run_loop()) for _ in range(self.settings.worker_concurrency)
        ]
        try:
            await self.stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.devin.aclose()
            await self.engine.dispose()

    def stop(self) -> None:
        self.stop_event.set()
