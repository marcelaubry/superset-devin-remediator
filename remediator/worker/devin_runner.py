"""Runs one bounded Devin session for a case attempt.

The runner owns the spend boundary: a durable intent row is committed before the
single ``POST /sessions``; an uncertain POST is reconciled by exact operation tag
and never repeated; polling is bounded by an absolute deadline; on timeout the
session is re-read once and then terminated remotely before the case is marked
``TIMED_OUT``.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..devin.client import (
    CreateSessionRequest,
    DevinApiError,
    DevinClient,
    DevinError,
    DevinSessionNotFound,
    DevinTransportError,
    SessionSnapshot,
)
from ..devin.prompt import TRIAGE_PROMPT_VERSION, TriagePromptInput, render_triage_prompt
from ..devin.status import Disposition, classify
from ..devin.tags import correlation_tags, operation_key
from ..devin.triage import TRIAGE_OUTPUT_SCHEMA
from ..github_refs import BaseCommitResolutionError, BaseCommitResolver
from ..lifecycle import TERMINAL_STATES, CaseState, InvalidTransition, transition
from ..models import (
    ACTIVE_ATTEMPT_STATUSES,
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    CreateState,
    NotificationOutbox,
    OutboxChannel,
    WebhookEvent,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


class RunResult(StrEnum):
    FINISHED = "finished"
    FAILED = "failed"
    HUMAN_BLOCKED = "human_blocked"
    TIMED_OUT = "timed_out"
    TERMINATION_PENDING = "termination_pending"
    ORPHANED = "orphaned"


@dataclass(frozen=True)
class RunOutcome:
    result: RunResult
    attempt: Attempt
    snapshot: SessionSnapshot | None = None
    reason: str = ""


def _now() -> datetime:
    return datetime.now(UTC)


def _target_state(kind: AttemptKind) -> CaseState:
    return CaseState.TRIAGING if kind == AttemptKind.TRIAGE else CaseState.REMEDIATING


def _intent_state(kind: AttemptKind) -> CaseState:
    return (
        CaseState.TRIAGE_CREATE_INTENT
        if kind == AttemptKind.TRIAGE
        else CaseState.REMEDIATION_CREATE_INTENT
    )


def _outbox(case: Case, kind: str, **payload: Any) -> NotificationOutbox:
    return NotificationOutbox(
        case_id=case.id,
        channel=OutboxChannel.GITHUB,
        kind=kind,
        payload={"issue_number": case.issue_number, **payload},
    )


async def fail_case(
    session: AsyncSession,
    case: Case,
    reason: str,
    actor: str,
    claimed_by: str | None = None,
) -> None:
    case.failure_reason = reason
    if CaseState(case.state) not in TERMINAL_STATES:
        await transition(
            session, case, CaseState.FAILED, reason, actor, expected_claimed_by=claimed_by
        )
        session.add(_outbox(case, "case_failed", reason=reason))


class DevinRunner:
    def __init__(
        self,
        session: AsyncSession,
        case: Case,
        devin: DevinClient,
        settings: Settings,
        resolver: BaseCommitResolver,
        *,
        claimed_by: str | None = None,
        clock: Clock = _now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.session = session
        self.case = case
        self.devin = devin
        self.settings = settings
        self.resolver = resolver
        self.claimed_by = claimed_by
        self.clock = clock
        self.sleep = sleep

    async def _transition(self, to_state: CaseState, reason: str) -> None:
        await transition(
            self.session,
            self.case,
            to_state,
            reason,
            "worker",
            expected_claimed_by=self.claimed_by,
        )

    # ----------------------------------------------------------------- entry

    async def run(self, kind: AttemptKind) -> RunOutcome:
        attempt = await self.session.scalar(
            select(Attempt)
            .where(
                Attempt.case_id == self.case.id,
                Attempt.kind == kind,
                Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
            )
            .order_by(desc(Attempt.started_at))
            .limit(1)
        )
        if attempt is None:
            created = await self._create(kind)
            if isinstance(created, RunOutcome):
                return created
            attempt = created
        elif attempt.status == AttemptStatus.TERMINATION_PENDING:
            return await self._handle_timeout(attempt)
        elif attempt.devin_session_id is None:
            reconciled = await self._reconcile_create(attempt)
            if isinstance(reconciled, RunOutcome):
                return reconciled
        else:
            logger.info(
                "resuming Devin session %s for case %s", attempt.devin_session_id, self.case.id
            )
            if CaseState(self.case.state) == CaseState.RECONCILING_CREATE:
                await self._transition(_target_state(kind), "Devin session re-attached")
                await self.session.commit()
        return await self._poll(attempt)

    # ---------------------------------------------------------------- create

    async def _create(self, kind: AttemptKind) -> Attempt | RunOutcome:
        case = self.case
        count = await self.session.scalar(
            select(func.count())
            .select_from(Attempt)
            .where(Attempt.case_id == case.id, Attempt.kind == kind)
        )
        ordinal = int(count or 0) + 1
        if ordinal > self.settings.max_attempts_per_kind:
            reason = "attempt cap reached"
            await fail_case(self.session, case, reason, "worker", self.claimed_by)
            await self.session.commit()
            placeholder = Attempt(case_id=case.id, kind=kind, idempotency_key="", operation_key="")
            return RunOutcome(RunResult.FAILED, placeholder, reason=reason)

        key = operation_key(case.id, kind.value, ordinal)
        now = self.clock()
        attempt = Attempt(
            case_id=case.id,
            kind=kind,
            idempotency_key=f"{case.id}:{kind.value}:{ordinal}",
            operation_key=key,
            create_state=CreateState.PENDING,
            status=AttemptStatus.RUNNING,
            started_at=now,
            timeout_at=now + timedelta(seconds=self.settings.devin_triage_timeout_seconds),
            max_acu_limit=self.settings.devin_triage_max_acu,
            prompt_version=TRIAGE_PROMPT_VERSION,
        )
        self.session.add(attempt)
        await self.session.commit()

        request = await self._build_request(attempt, kind)
        if isinstance(request, str):
            attempt.create_state = CreateState.NOT_SENT
            attempt.status = AttemptStatus.FAILED
            attempt.error = request
            attempt.finished_at = self.clock()
            await fail_case(self.session, case, request, "worker", self.claimed_by)
            await self.session.commit()
            return RunOutcome(RunResult.FAILED, attempt, reason=request)

        attempt.devin_tags = list(request.all_tags())
        attempt.create_sent_at = self.clock()
        await self.session.commit()
        try:
            snapshot = await self.devin.create_session(request)
        except DevinApiError as exc:
            reason = f"Devin create rejected: {exc}"
            attempt.create_state = CreateState.API_ERROR
            attempt.status = AttemptStatus.FAILED
            attempt.error = reason
            attempt.finished_at = self.clock()
            await fail_case(self.session, case, reason, "worker", self.claimed_by)
            await self.session.commit()
            return RunOutcome(RunResult.FAILED, attempt, reason=reason)
        except DevinTransportError as exc:
            attempt.create_state = CreateState.UNCERTAIN
            attempt.status = AttemptStatus.RECONCILING
            attempt.reconciliation_reason = f"create outcome unknown: {exc}"
            await self._transition(CaseState.RECONCILING_CREATE, attempt.reconciliation_reason)
            await self.session.commit()
            reconciled = await self._reconcile_create(attempt)
            return reconciled if isinstance(reconciled, RunOutcome) else attempt

        return await self._attach(attempt, snapshot, CreateState.CREATED, "Devin session created")

    async def _build_request(
        self, attempt: Attempt, kind: AttemptKind
    ) -> CreateSessionRequest | str:
        case = self.case
        if case.repository != self.settings.github_repository:
            return f"repository {case.repository} is not the allowlisted repository"
        if kind == AttemptKind.REMEDIATION and self.settings.live_mode:
            return "remediation sessions are disabled in live mode (Phase 2 is triage only)"
        try:
            base_sha = await self.resolver.resolve(case.repository, self.settings.github_base_ref)
        except BaseCommitResolutionError as exc:
            return f"base commit could not be resolved: {exc}"
        attempt.base_sha = base_sha
        issue = await _source_issue(self.session, case)
        try:
            prompt = render_triage_prompt(
                TriagePromptInput(
                    repository=case.repository,
                    base_sha=base_sha,
                    issue_number=case.issue_number,
                    issue_title=str(issue.get("title") or case.issue_title),
                    issue_body=str(issue.get("body") or ""),
                    issue_labels=tuple(
                        str(label.get("name", "")) for label in issue.get("labels", [])
                    ),
                    issue_url=str(issue.get("html_url") or case.issue_url),
                    eligibility_reasons=tuple(
                        f"{check.get('name')}: {check.get('reason')}" for check in case.rubric or []
                    ),
                    operation_key=attempt.operation_key,
                    case_id=str(case.id),
                    attempt_id=str(attempt.id),
                )
            )
        except ValueError as exc:
            return f"prompt rendering refused: {exc}"
        if kind == AttemptKind.REMEDIATION:
            prompt = f"[fake remediation] {prompt}"
        return CreateSessionRequest(
            prompt=prompt,
            repository=case.repository,
            base_sha=base_sha,
            max_acu_limit=self.settings.devin_triage_max_acu,
            operation_key=attempt.operation_key,
            tags=tuple(
                correlation_tags(
                    case.repository, case.issue_number, kind.value, case.id, attempt.id
                )
            ),
            structured_output_schema=TRIAGE_OUTPUT_SCHEMA,
        )

    async def _attach(
        self, attempt: Attempt, snapshot: SessionSnapshot, state: CreateState, reason: str
    ) -> Attempt | RunOutcome:
        case = self.case
        attempt.create_state = state
        attempt.status = AttemptStatus.RUNNING
        attempt.devin_session_id = snapshot.session_id
        attempt.devin_session_url = snapshot.url
        attempt.devin_status = snapshot.status
        attempt.devin_status_detail = snapshot.status_detail
        attempt.reconciliation_reason = None
        case.devin_session_id = snapshot.session_id
        case.devin_session_url = snapshot.url
        attempt_id = attempt.id
        try:
            await self._transition(_target_state(attempt.kind), reason)
        except InvalidTransition:
            await self.session.rollback()
            await self.session.execute(
                update(Attempt)
                .where(Attempt.id == attempt_id)
                .values(
                    create_state=state,
                    devin_session_id=snapshot.session_id,
                    devin_session_url=snapshot.url,
                    status=AttemptStatus.CANCELLED,
                    error="orphaned: lease lost during create",
                    finished_at=self.clock(),
                )
            )
            await self.session.commit()
            try:
                await self.devin.terminate_session(snapshot.session_id)
                logger.warning("terminated orphaned Devin session %s", snapshot.session_id)
            except DevinError as exc:
                logger.error(
                    "could not terminate orphaned session %s: %s", snapshot.session_id, exc
                )
            raise
        await self.session.commit()
        return attempt

    # ------------------------------------------------------------- reconcile

    async def _reconcile_create(self, attempt: Attempt) -> Attempt | RunOutcome:
        case = self.case
        if CaseState(case.state) == _intent_state(attempt.kind):
            await self._transition(CaseState.RECONCILING_CREATE, "reconciling uncertain create")
        attempt.status = AttemptStatus.RECONCILING
        if attempt.create_state == CreateState.PENDING:
            attempt.create_state = CreateState.UNCERTAIN
            attempt.reconciliation_reason = "worker restarted before create outcome was recorded"
        await self.session.commit()

        matches: list[SessionSnapshot] = []
        last_error: str | None = None
        for index in range(max(1, self.settings.reconcile_max_attempts)):
            if index:
                await self.sleep(self.settings.reconcile_retry_delay_seconds)
            try:
                matches = await self.devin.find_sessions_by_tag(attempt.operation_key)
            except DevinError as exc:
                last_error = str(exc)
                continue
            if matches:
                break
        if len(matches) == 1:
            attempt.reconciliation_reason = None
            attached = await self._attach(
                attempt, matches[0], CreateState.RECONCILED, "Devin session reconciled by tag"
            )
            return attached
        if len(matches) > 1:
            reason = (
                f"{len(matches)} Devin sessions carry operation tag {attempt.operation_key}; "
                "refusing to pick one"
            )
        elif last_error:
            reason = f"create outcome could not be established: {last_error}"
        else:
            reason = "no Devin session carries the operation tag; create outcome unknown"
        attempt.create_state = CreateState.UNRESOLVED
        attempt.status = AttemptStatus.BLOCKED
        attempt.reconciliation_reason = reason
        attempt.error = reason
        attempt.finished_at = self.clock()
        case.failure_reason = reason
        await self._transition(CaseState.HUMAN_BLOCKED, reason)
        self.session.add(_outbox(case, "human_blocked", reason=reason))
        await self.session.commit()
        return RunOutcome(RunResult.HUMAN_BLOCKED, attempt, reason=reason)

    # ------------------------------------------------------------------ poll

    async def _record_poll(self, attempt: Attempt, snapshot: SessionSnapshot) -> None:
        now = self.clock()
        attempt.first_polled_at = attempt.first_polled_at or now
        attempt.last_polled_at = now
        attempt.poll_count += 1
        attempt.devin_status = snapshot.status
        attempt.devin_status_detail = snapshot.status_detail
        if snapshot.acus_consumed is not None:
            attempt.devin_acus_consumed = snapshot.acus_consumed
        if snapshot.url and not attempt.devin_session_url:
            attempt.devin_session_url = snapshot.url
            self.case.devin_session_url = snapshot.url

    async def _poll(self, attempt: Attempt) -> RunOutcome:
        session_id = attempt.devin_session_id
        assert session_id is not None
        if attempt.timeout_at is None:
            attempt.timeout_at = (attempt.started_at or self.clock()) + timedelta(
                seconds=self.settings.devin_triage_timeout_seconds
            )
            await self.session.commit()
        deadline = attempt.timeout_at
        consecutive_errors = 0
        while True:
            if self.clock() >= deadline:
                return await self._handle_timeout(attempt)
            try:
                snapshot = await self.devin.get_session(session_id)
            except DevinSessionNotFound:
                reason = f"Devin session {session_id} no longer exists"
                return await self._fail(attempt, reason, None)
            except DevinError as exc:
                consecutive_errors += 1
                attempt.status = AttemptStatus.RECONCILING
                attempt.reconciliation_reason = f"poll failed ({consecutive_errors}x): {exc}"
                await self.session.commit()
                await self.sleep(self.settings.devin_poll_interval_seconds)
                continue
            consecutive_errors = 0
            await self._record_poll(attempt, snapshot)
            mapping = classify(snapshot.status, snapshot.status_detail)
            if mapping.disposition == Disposition.UNKNOWN:
                attempt.status = AttemptStatus.RECONCILING
                attempt.reconciliation_reason = mapping.reason
                await self.session.commit()
                await self.sleep(self.settings.devin_poll_interval_seconds)
                continue
            attempt.status = AttemptStatus.RUNNING
            attempt.reconciliation_reason = None
            await self.session.commit()
            if mapping.disposition == Disposition.LIVE:
                await self.sleep(self.settings.devin_poll_interval_seconds)
                continue
            return await self._settle(attempt, snapshot, mapping.disposition, mapping.reason)

    async def _settle(
        self, attempt: Attempt, snapshot: SessionSnapshot, disposition: Disposition, reason: str
    ) -> RunOutcome:
        if disposition == Disposition.FINISHED:
            attempt.structured_output = snapshot.structured_output
            attempt.finished_at = self.clock()
            await self.session.commit()
            return RunOutcome(RunResult.FINISHED, attempt, snapshot, reason)
        if disposition == Disposition.WAITING_FOR_HUMAN:
            attempt.status = AttemptStatus.BLOCKED
            attempt.error = reason
            attempt.finished_at = self.clock()
            self.case.failure_reason = reason
            await self._transition(CaseState.HUMAN_BLOCKED, reason)
            self.session.add(
                _outbox(self.case, "human_blocked", reason=reason, devin_url=snapshot.url)
            )
            await self.session.commit()
            return RunOutcome(RunResult.HUMAN_BLOCKED, attempt, snapshot, reason)
        return await self._fail(attempt, reason, snapshot)

    async def _fail(
        self, attempt: Attempt, reason: str, snapshot: SessionSnapshot | None
    ) -> RunOutcome:
        attempt.status = AttemptStatus.FAILED
        attempt.error = reason
        attempt.finished_at = self.clock()
        await fail_case(self.session, self.case, reason, "worker", self.claimed_by)
        await self.session.commit()
        return RunOutcome(RunResult.FAILED, attempt, snapshot, reason)

    # --------------------------------------------------------------- timeout

    async def _handle_timeout(self, attempt: Attempt) -> RunOutcome:
        session_id = attempt.devin_session_id
        assert session_id is not None
        final: SessionSnapshot | None = None
        try:
            final = await self.devin.get_session(session_id)
        except DevinSessionNotFound:
            return await self._mark_timed_out(attempt, "session already gone at timeout")
        except DevinError as exc:
            logger.warning("final GET for %s failed: %s", session_id, exc)
        if final is not None:
            await self._record_poll(attempt, final)
            mapping = classify(final.status, final.status_detail)
            if mapping.disposition in {
                Disposition.FINISHED,
                Disposition.WAITING_FOR_HUMAN,
                Disposition.FAILED,
            }:
                if CaseState(self.case.state) == CaseState.TERMINATION_PENDING and (
                    mapping.disposition == Disposition.FAILED
                ):
                    return await self._mark_timed_out(attempt, f"session ended: {mapping.reason}")
                attempt.status = AttemptStatus.RUNNING
                await self.session.commit()
                return await self._settle(attempt, final, mapping.disposition, mapping.reason)
            if mapping.remote_terminal:
                return await self._mark_timed_out(attempt, f"session ended: {mapping.reason}")
        try:
            await self.devin.terminate_session(session_id)
        except DevinSessionNotFound:
            return await self._mark_timed_out(attempt, "session already gone at timeout")
        except DevinError as exc:
            reason = f"timeout reached but DELETE failed: {exc}"
            attempt.status = AttemptStatus.TERMINATION_PENDING
            attempt.reconciliation_reason = reason
            if CaseState(self.case.state) != CaseState.TERMINATION_PENDING:
                await self._transition(CaseState.TERMINATION_PENDING, reason)
            self.case.failure_reason = reason
            await self.session.commit()
            return RunOutcome(RunResult.TERMINATION_PENDING, attempt, final, reason)
        return await self._mark_timed_out(attempt, "session terminated remotely")

    async def _mark_timed_out(self, attempt: Attempt, detail: str) -> RunOutcome:
        deadline = attempt.timeout_at.isoformat() if attempt.timeout_at else "?"
        reason = f"Devin session exceeded deadline {deadline}; {detail}"
        attempt.status = AttemptStatus.TIMED_OUT
        attempt.error = reason
        attempt.reconciliation_reason = None
        attempt.finished_at = self.clock()
        self.case.failure_reason = reason
        await self._transition(CaseState.TIMED_OUT, reason)
        self.session.add(_outbox(self.case, "case_timed_out", reason=reason))
        await self.session.commit()
        return RunOutcome(RunResult.TIMED_OUT, attempt, None, reason)


async def _source_issue(session: AsyncSession, case: Case) -> dict[str, Any]:
    source_event = await session.scalar(
        select(WebhookEvent)
        .where(WebhookEvent.case_id == case.id)
        .order_by(desc(WebhookEvent.received_at))
        .limit(1)
    )
    if source_event is None:
        return {"title": case.issue_title, "body": "", "labels": [], "html_url": case.issue_url}
    return cast(dict[str, Any], source_event.payload.get("issue", {}))
