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
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import metrics
from ..capacity import CapacityDenied, CapacityManager, clear_waiting, mark_waiting
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
from ..devin.prompt import (
    REMEDIATION_PROMPT_VERSION,
    TRIAGE_PROMPT_VERSION,
    RemediationPromptInput,
    TriagePromptInput,
    render_remediation_prompt,
    render_triage_prompt,
)
from ..devin.remediation import (
    REMEDIATION_OUTPUT_SCHEMA,
    REMEDIATION_OUTPUT_SCHEMA_WITHOUT_PROBE,
)
from ..devin.status import Disposition, StatusMapping, classify
from ..devin.tags import correlation_tags, operation_key, remediation_operation_key
from ..devin.triage import TRIAGE_OUTPUT_SCHEMA, TriageValidationError, validate_triage_output
from ..github_refs import BaseCommitResolutionError, BaseCommitResolver
from ..lifecycle import (
    REMEDIATION_PHASE,
    TERMINAL_STATES,
    TERMINATION_PENDING_STATES,
    TRIAGE_PHASE,
    CaseState,
    InvalidTransition,
    PhaseStates,
    phase_for_state,
    transition,
)
from ..models import (
    ACTIVE_ATTEMPT_STATUSES,
    ACU_REPORT_NOT_ATTEMPTED,
    CANCEL_TERMINATION_REASON,
    FAILURE_CLASS_SESSION,
    RECONCILED_NO_OUTPUT_PREFIX,
    UNRESOLVED_CREATE_ACK,
    WORKER_ERROR_TERMINATION_PREFIX,
    ApprovalRequest,
    Attempt,
    AttemptKind,
    AttemptStatus,
    CapacityLeaseKind,
    Case,
    CreateState,
    NotificationOutbox,
    OutboxChannel,
    ProbeExecution,
    ProbeSnapshot,
    WebhookEvent,
)
from ..probe_policy import ProbeStatus
from ..safe_urls import safe_href

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
    # No create intent exists yet: a configured concurrency limit is saturated. The case
    # keeps its state, spends nothing and is retried by the next claim.
    WAITING_FOR_CAPACITY = "waiting_for_capacity"


@dataclass(frozen=True)
class RunOutcome:
    result: RunResult
    attempt: Attempt
    snapshot: SessionSnapshot | None = None
    reason: str = ""


def _now() -> datetime:
    return datetime.now(UTC)


def phase_for_kind(kind: AttemptKind) -> PhaseStates:
    return TRIAGE_PHASE if kind == AttemptKind.TRIAGE else REMEDIATION_PHASE


def _outbox_kind(phase: PhaseStates, event: str) -> str:
    """Record-only outbox kind for a lifecycle event (`case_failed` / `remediation_failed`)."""
    prefix = "remediation" if phase is REMEDIATION_PHASE else "case"
    return f"{prefix}_{event}"


def _outbox(case: Case, kind: str, **payload: Any) -> NotificationOutbox:
    return NotificationOutbox(
        case_id=case.id,
        channel=OutboxChannel.GITHUB,
        kind=kind,
        payload={"issue_number": case.issue_number, **payload},
    )


@dataclass(frozen=True)
class RemediationContext:
    """Everything a remediation attempt is authorised against, resolved before any POST.

    Built by the processor from the case's *current* approved round, the immutable probe
    snapshot taken at dispatch and the persisted BASE probe execution that reproduced the
    defect; the runner never reads probe files or approval state itself.
    """

    approval: ApprovalRequest
    triage_output: dict[str, Any]
    base_ref: str
    # Both are absent only when no probe is registered under PROBE_POLICY=if_available:
    # `pinned_base_sha` then carries the base SHA resolved at dispatch.
    probe: ProbeSnapshot | None = None
    base_execution: ProbeExecution | None = None
    pinned_base_sha: str | None = None
    # Only a persisted `ProbePolicyDecision` may set this; a merely absent snapshot never
    # authorises a session.
    probe_not_configured: bool = False

    def __post_init__(self) -> None:
        if self.probe_not_configured:
            if self.probe is not None or self.pinned_base_sha is None:
                raise ValueError(
                    "a not-configured probe status requires no snapshot and a pinned base SHA"
                )
        elif self.probe is None or self.base_execution is None:
            raise ValueError("remediation context needs a probe snapshot and its BASE execution")

    @property
    def base_sha(self) -> str:
        if self.probe is not None:
            return self.probe.base_sha
        if self.pinned_base_sha is None:
            raise ValueError("remediation context has neither a probe nor a pinned base SHA")
        return self.pinned_base_sha


async def active_attempts_with_session(session: AsyncSession, case_id: Any) -> list[Attempt]:
    """Active attempts that may own a live Devin session (created or uncertain)."""
    return list(
        (
            await session.scalars(
                select(Attempt).where(
                    Attempt.case_id == case_id,
                    Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                    Attempt.create_sent_at.is_not(None),
                )
            )
        ).all()
    )


async def fail_case(
    session: AsyncSession,
    case: Case,
    reason: str,
    actor: str,
    claimed_by: str | None = None,
) -> None:
    """Fail a case, or park it in TERMINATION_PENDING while a Devin session may be live.

    A case never becomes terminal while an active attempt still owns (or may own) a
    remote session; the TERMINATION_PENDING recovery path performs the DELETE first.
    """
    case.failure_reason = reason
    state = CaseState(case.state)
    if state in TERMINAL_STATES:
        return
    phase = phase_for_state(state)
    live = await active_attempts_with_session(session, case.id)
    if live:
        pending_reason = f"{WORKER_ERROR_TERMINATION_PREFIX}: {reason}; terminating Devin session"
        for attempt in live:
            attempt.status = AttemptStatus.TERMINATION_PENDING
            attempt.reconciliation_reason = pending_reason
        if state != phase.termination_pending:
            await transition(
                session,
                case,
                phase.termination_pending,
                pending_reason,
                actor,
                expected_claimed_by=claimed_by,
            )
        return
    await transition(session, case, phase.failed, reason, actor, expected_claimed_by=claimed_by)
    session.add(_outbox(case, _outbox_kind(phase, "failed"), reason=reason))


async def allocate_attempt_ordinal(session: AsyncSession, case_id: Any, kind: AttemptKind) -> int:
    """Next 1-based ordinal for (case, kind), allocated under a case row lock so two workers
    retrying concurrently cannot both compute the same number. FOR NO KEY UPDATE keeps
    foreign-key inserts referencing the case (outbox rows, probe executions) unblocked; the
    partial unique index on (case_id, kind, ordinal) and the unique operation_key are the
    database backstop should the lock ever be bypassed."""
    await session.execute(select(Case.id).where(Case.id == case_id).with_for_update(key_share=True))
    highest = await session.scalar(
        select(func.max(Attempt.ordinal)).where(Attempt.case_id == case_id, Attempt.kind == kind)
    )
    count = await session.scalar(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.case_id == case_id, Attempt.kind == kind)
    )
    return max(int(highest or 0), int(count or 0)) + 1


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
        remediation: RemediationContext | None = None,
        capacity: CapacityManager | None = None,
    ) -> None:
        self.session = session
        self.case = case
        self.devin = devin
        self.settings = settings
        self.resolver = resolver
        self.claimed_by = claimed_by
        self.clock = clock
        self.sleep = sleep
        self.remediation = remediation
        self.capacity = capacity
        self.phase: PhaseStates = TRIAGE_PHASE

    @staticmethod
    def _lease_kind(kind: AttemptKind) -> CapacityLeaseKind:
        return (
            CapacityLeaseKind.REMEDIATION
            if kind == AttemptKind.REMEDIATION
            else CapacityLeaseKind.TRIAGE
        )

    async def _acquire_capacity(self, kind: AttemptKind) -> CapacityDenied | None:
        """Take this case's session slot before any create intent. Denied → no attempt row,
        no POST, the case is marked waiting and left in its current state."""
        if self.capacity is None:
            return None
        lease_kind = self._lease_kind(kind)
        per_scope = (
            self.capacity.limits.remediation_per_repository
            if lease_kind is CapacityLeaseKind.REMEDIATION
            else None
        )
        outcome = await self.capacity.acquire(
            self.session,
            kind=lease_kind,
            case_id=self.case.id,
            scope=self.case.repository.lower(),
            per_scope_limit=per_scope,
        )
        if isinstance(outcome, CapacityDenied):
            metrics.capacity_denied_total.labels(metrics.mode(), kind.value.lower()).inc()
            await mark_waiting(self.session, self.case, outcome)
            await self.session.commit()
            return outcome
        await clear_waiting(self.session, self.case)
        return None

    async def _bind_lease(self, attempt: Attempt) -> None:
        """Attach (or re-create after a restart) the lease for an attempt that already
        exists. The session is already live, so it is counted even if that temporarily
        exceeds the limit; new sessions then wait until it finishes."""
        if self.capacity is None:
            return
        await self.capacity.acquire(
            self.session,
            kind=self._lease_kind(attempt.kind),
            case_id=self.case.id,
            scope=self.case.repository.lower(),
            attempt_id=attempt.id,
            force=True,
        )

    async def _release_capacity(self, kind: AttemptKind, reason: str) -> None:
        if self.capacity is None:
            return
        await self.capacity.release(
            self.session, kind=self._lease_kind(kind), case_id=self.case.id, reason=reason
        )

    async def _settle_capacity(self, outcome: RunOutcome) -> None:
        """Release the session slot once the attempt is no longer live. TERMINATION_PENDING
        keeps the slot: the remote session may still be running (and billing)."""
        if outcome.result in {RunResult.TERMINATION_PENDING, RunResult.WAITING_FOR_CAPACITY}:
            return
        await self._release_capacity(outcome.attempt.kind, f"attempt {outcome.result.value}")
        await self.session.commit()

    async def _report_consumption(self, attempt: Attempt) -> None:
        """Optional official ACU figure. Never raises, never estimates, never blocks."""
        if attempt.devin_session_id is None or attempt.acu_reported_at is not None:
            return
        if not self.settings.devin_acu_reporting_enabled:
            attempt.acu_report_status = ACU_REPORT_NOT_ATTEMPTED
            return
        try:
            report = await self.devin.session_consumption(attempt.devin_session_id)
        except Exception as exc:  # noqa: BLE001 - reporting must never fail remediation
            logger.warning("ACU report failed for %s: %s", attempt.devin_session_id, type(exc))
            attempt.acu_report_status = "unavailable"
            attempt.acu_report_detail = f"client error: {type(exc).__name__}"
        else:
            attempt.acu_report_status = report.status
            attempt.acu_reported = report.acus
            attempt.acu_report_detail = report.detail or None
        attempt.acu_reported_at = self.clock()

    def _timeout_seconds(self) -> float:
        if self.phase is REMEDIATION_PHASE:
            return self.settings.devin_remediation_timeout_seconds
        return self.settings.devin_triage_timeout_seconds

    def _max_acu(self) -> int:
        if self.phase is REMEDIATION_PHASE:
            return self.settings.devin_remediation_max_acu
        return self.settings.devin_triage_max_acu

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
        self.phase = phase_for_kind(kind)
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
            denied = await self._acquire_capacity(kind)
            if denied is not None:
                placeholder = Attempt(
                    case_id=self.case.id, kind=kind, idempotency_key="", operation_key=""
                )
                return RunOutcome(
                    RunResult.WAITING_FOR_CAPACITY,
                    placeholder,
                    reason=f"waiting for capacity: {denied.label}",
                )
            created = await self._create(kind)
            if isinstance(created, RunOutcome):
                await self._settle_capacity(created)
                return created
            attempt = created
            await self._bind_lease(attempt)
            await self.session.commit()
            outcome = await self._poll(attempt)
        elif attempt.status == AttemptStatus.TERMINATION_PENDING:
            if attempt.devin_session_id is None:
                outcome = await self._terminate_by_tag(attempt)
            else:
                outcome = await self._handle_timeout(attempt)
        elif attempt.devin_session_id is None:
            await self._bind_lease(attempt)
            reconciled = await self._reconcile_create(attempt)
            if isinstance(reconciled, RunOutcome):
                await self._settle_capacity(reconciled)
                return reconciled
            outcome = await self._poll(attempt)
        else:
            logger.info(
                "resuming Devin session %s for case %s", attempt.devin_session_id, self.case.id
            )
            await self._bind_lease(attempt)
            if CaseState(self.case.state) in {self.phase.reconciling, self.phase.intent}:
                await self._transition(self.phase.running, "Devin session re-attached")
            await self.session.commit()
            outcome = await self._poll(attempt)
        if outcome.result is not RunResult.TERMINATION_PENDING and outcome.attempt.id is not None:
            await self._report_consumption(outcome.attempt)
            await self.session.commit()
        if outcome.result not in {RunResult.TERMINATION_PENDING, RunResult.WAITING_FOR_CAPACITY}:
            metrics.session_outcomes_total.labels(
                metrics.mode(), kind.value.lower(), outcome.result.value
            ).inc()
        await self._settle_capacity(outcome)
        return outcome

    # ------------------------------------------------------------- reconcile

    async def blocked_attempts_with_session(self, kind: AttemptKind) -> list[Attempt]:
        """BLOCKED attempts of `kind` that still retain a remote session id, newest first."""
        return list(
            (
                await self.session.scalars(
                    select(Attempt)
                    .where(
                        Attempt.case_id == self.case.id,
                        Attempt.kind == kind,
                        Attempt.status == AttemptStatus.BLOCKED,
                        Attempt.devin_session_id.is_not(None),
                    )
                    .order_by(desc(Attempt.started_at), desc(Attempt.id))
                )
            ).all()
        )

    async def reconcile_blocked(self, kind: AttemptKind) -> RunOutcome | None:
        """Re-read the retained sessions of a HUMAN_BLOCKED case; never POST.

        Each BLOCKED attempt that still holds a session id is re-read with a single GET,
        newest first. The first snapshot that carries structured output is ingested on its
        *original* attempt (operation key and audit trail preserved) and the case leaves
        HUMAN_BLOCKED through TRIAGING, exactly as a live poll would have settled it. Any
        newer blocked attempt without output is superseded so the ingested attempt is the
        case's current triage round. Attempts whose session has no output (or no longer
        exists) are annotated with `RECONCILED_NO_OUTPUT_PREFIX`, which is what permits a
        replacement later. Returns the settled outcome, or None when nothing was ingested.
        """
        self.phase = phase_for_kind(kind)
        candidates = await self.blocked_attempts_with_session(kind)
        with_output: list[tuple[Attempt, SessionSnapshot, bool]] = []
        for attempt in candidates:
            session_id = attempt.devin_session_id
            assert session_id is not None
            try:
                snapshot = await self.devin.get_session(session_id)
            except DevinSessionNotFound:
                attempt.reconciliation_reason = (
                    f"{RECONCILED_NO_OUTPUT_PREFIX}: session {session_id} no longer exists"
                )
                await self.session.commit()
                continue
            except DevinError as exc:
                attempt.reconciliation_reason = f"reconcile GET failed: {exc}"
                await self.session.commit()
                continue
            await self._record_poll(attempt, snapshot)
            if snapshot.structured_output is None:
                attempt.reconciliation_reason = (
                    f"{RECONCILED_NO_OUTPUT_PREFIX}: session {snapshot.status}"
                    f"/{snapshot.status_detail or 'no detail'}"
                )
                await self.session.commit()
                continue
            with_output.append(
                (attempt, snapshot, self._output_is_valid(kind, snapshot.structured_output))
            )
        if not with_output:
            return None
        # Newest valid output wins; only when no retained output is valid does the newest
        # (malformed) one settle the case through the ordinary malformed-output failure.
        chosen = next((entry for entry in with_output if entry[2]), with_output[0])
        attempt, snapshot, _ = chosen
        session_id = attempt.devin_session_id
        reason = (
            f"reconciled retained Devin session {session_id}: structured output present "
            f"({snapshot.status}/{snapshot.status_detail or 'no detail'})"
        )
        for other in candidates:
            if other.id != attempt.id and other.status == AttemptStatus.BLOCKED:
                other.status = AttemptStatus.CANCELLED
                other.error = f"superseded by reconciled attempt {attempt.operation_key}"
        attempt.status = AttemptStatus.RUNNING
        attempt.error = None
        attempt.reconciliation_reason = None
        self.case.failure_reason = None
        if CaseState(self.case.state) == self.phase.human_blocked:
            await self._transition(self.phase.running, reason)
        await self.session.commit()
        metrics.reconciliations_total.labels(
            metrics.mode(), "blocked_session", "structured_output"
        ).inc()
        outcome = await self._settle(attempt, snapshot, Disposition.FINISHED, reason)
        await self._report_consumption(attempt)
        await self.session.commit()
        return outcome

    @staticmethod
    def _output_is_valid(kind: AttemptKind, output: dict[str, Any]) -> bool:
        if kind != AttemptKind.TRIAGE:
            return True
        try:
            validate_triage_output(output)
        except TriageValidationError:
            return False
        return True

    # ---------------------------------------------------------------- create

    async def _create(self, kind: AttemptKind) -> Attempt | RunOutcome:
        case = self.case
        ordinal = await allocate_attempt_ordinal(self.session, case.id, kind)
        if ordinal > self.settings.max_attempts_per_kind:
            reason = "attempt cap reached"
            await fail_case(self.session, case, reason, "worker", self.claimed_by)
            await self.session.commit()
            placeholder = Attempt(case_id=case.id, kind=kind, idempotency_key="", operation_key="")
            return RunOutcome(RunResult.FAILED, placeholder, reason=reason)

        refused = await self._settle_previous_sessions(kind)
        if refused is not None:
            await fail_case(self.session, case, refused, "worker", self.claimed_by)
            await self.session.commit()
            placeholder = Attempt(case_id=case.id, kind=kind, idempotency_key="", operation_key="")
            return RunOutcome(RunResult.FAILED, placeholder, reason=refused)

        context = self.remediation
        if kind == AttemptKind.REMEDIATION:
            if context is None:
                reason = "remediation attempt requested without an approved dispatch context"
                await fail_case(self.session, case, reason, "worker", self.claimed_by)
                await self.session.commit()
                placeholder = Attempt(
                    case_id=case.id, kind=kind, idempotency_key="", operation_key=""
                )
                return RunOutcome(RunResult.FAILED, placeholder, reason=reason)
            key = remediation_operation_key(
                case.id, context.approval.triage_result_hash, context.base_sha, ordinal
            )
        else:
            key = operation_key(case.id, kind.value, ordinal)
        now = self.clock()
        attempt = Attempt(
            case_id=case.id,
            kind=kind,
            ordinal=ordinal,
            idempotency_key=f"{case.id}:{kind.value}:{ordinal}",
            operation_key=key,
            create_state=CreateState.PENDING,
            status=AttemptStatus.RUNNING,
            started_at=now,
            timeout_at=now + timedelta(seconds=self._timeout_seconds()),
            max_acu_limit=self._max_acu(),
            prompt_version=(
                REMEDIATION_PROMPT_VERSION
                if kind == AttemptKind.REMEDIATION
                else TRIAGE_PROMPT_VERSION
            ),
        )
        if context is not None and kind == AttemptKind.REMEDIATION:
            attempt.approval_request_id = context.approval.id
            attempt.triage_result_hash = context.approval.triage_result_hash
            attempt.base_sha = context.base_sha
            attempt.probe_status = (
                ProbeStatus.NOT_CONFIGURED.value
                if context.probe_not_configured
                else ProbeStatus.VERIFIED.value
            )
            if context.probe is not None:
                attempt.probe_snapshot_id = context.probe.id
            if context.base_execution is not None:
                context.base_execution.attempt = attempt
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
            await self._transition(self.phase.reconciling, attempt.reconciliation_reason)
            await self.session.commit()
            reconciled = await self._reconcile_create(attempt)
            return reconciled if isinstance(reconciled, RunOutcome) else attempt

        return await self._attach(attempt, snapshot, CreateState.CREATED, "Devin session created")

    async def _settle_previous_sessions(self, kind: AttemptKind) -> str | None:
        """Before a new POST, make sure no earlier attempt of this kind may still be live.

        Blocked attempts with a known session are terminated remotely; unresolved
        creates require an explicit operator acknowledgement because a session may
        exist that we could not find by tag. Returns a refusal reason, or None.
        """
        previous = list(
            (
                await self.session.scalars(
                    select(Attempt).where(
                        Attempt.case_id == self.case.id,
                        Attempt.kind == kind,
                        Attempt.status.in_([AttemptStatus.BLOCKED, AttemptStatus.RECONCILING]),
                    )
                )
            ).all()
        )
        for attempt in previous:
            if attempt.create_state == CreateState.UNRESOLVED:
                if attempt.reconciliation_reason != UNRESOLVED_CREATE_ACK:
                    return (
                        f"attempt {attempt.operation_key} has an unresolved create; "
                        "an operator must confirm no live session exists before a new POST"
                    )
                attempt.status = AttemptStatus.CANCELLED
                attempt.finished_at = attempt.finished_at or self.clock()
                continue
            if attempt.devin_session_id is None:
                continue
            try:
                snapshot = await self.devin.get_session(attempt.devin_session_id)
                if snapshot.structured_output is not None:
                    return (
                        f"attempt {attempt.operation_key} retains Devin session "
                        f"{attempt.devin_session_id} with structured output; reconcile the "
                        "existing session instead of creating a replacement"
                    )
                await self.devin.terminate_session(attempt.devin_session_id)
            except DevinSessionNotFound:
                pass
            except DevinError as exc:
                return (
                    f"could not terminate previous Devin session {attempt.devin_session_id} "
                    f"before retry: {exc}"
                )
            attempt.status = AttemptStatus.CANCELLED
            attempt.error = f"{attempt.error or ''}; terminated before retry".lstrip("; ")
            attempt.finished_at = attempt.finished_at or self.clock()
        await self.session.commit()
        return None

    async def _build_request(
        self, attempt: Attempt, kind: AttemptKind
    ) -> CreateSessionRequest | str:
        case = self.case
        if not self.settings.repository_allowed(case.repository):
            return f"repository {case.repository} is not an allowlisted repository"
        if kind == AttemptKind.REMEDIATION:
            return await self._build_remediation_request(attempt)
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

    async def _build_remediation_request(self, attempt: Attempt) -> CreateSessionRequest | str:
        case = self.case
        context = self.remediation
        if context is None:
            return "remediation attempt requested without an approved dispatch context"
        probe = context.probe
        if probe is not None and (
            probe.repository != case.repository or probe.issue_number != case.issue_number
        ):
            return "probe snapshot does not belong to this case"
        approval = context.approval
        approved_by = approval.decided_by_slack_user_id or "unknown"
        approved_at = approval.decided_at.isoformat() if approval.decided_at else "unknown"
        issue = await _source_issue(self.session, case)
        try:
            prompt = render_remediation_prompt(
                RemediationPromptInput(
                    repository=case.repository,
                    base_ref=context.base_ref,
                    base_sha=context.base_sha,
                    branch_prefix=self.settings.devin_remediation_branch_prefix,
                    issue_number=case.issue_number,
                    issue_title=str(issue.get("title") or case.issue_title),
                    issue_body=str(issue.get("body") or ""),
                    issue_url=str(issue.get("html_url") or case.issue_url),
                    triage_output=context.triage_output,
                    triage_result_hash=approval.triage_result_hash,
                    probe_identifier=None if probe is None else probe.probe_identifier,
                    probe_hash=None if probe is None else probe.script_hash,
                    probe_script=None if probe is None else probe.script_content,
                    probe_expected_base_exit=(
                        None if probe is None else probe.expected_base_exit_code
                    ),
                    probe_expected_head_exit=(
                        None if probe is None else probe.expected_head_exit_code
                    ),
                    probe_registry_path=(
                        self.settings.probe_root if probe is None else probe.manifest_path
                    ),
                    approved_by=approved_by,
                    approved_at=approved_at,
                    operation_key=attempt.operation_key,
                    case_id=str(case.id),
                    attempt_id=str(attempt.id),
                )
            )
        except ValueError as exc:
            return f"prompt rendering refused: {exc}"
        return CreateSessionRequest(
            prompt=prompt,
            repository=case.repository,
            base_sha=context.base_sha,
            max_acu_limit=self.settings.devin_remediation_max_acu,
            operation_key=attempt.operation_key,
            tags=tuple(
                correlation_tags(
                    case.repository,
                    case.issue_number,
                    AttemptKind.REMEDIATION.value,
                    case.id,
                    attempt.id,
                )
            ),
            structured_output_schema=(
                REMEDIATION_OUTPUT_SCHEMA_WITHOUT_PROBE
                if context.probe_not_configured
                else REMEDIATION_OUTPUT_SCHEMA
            ),
            title=f"Remediate {case.repository}#{case.issue_number}",
        )

    async def _attach(
        self, attempt: Attempt, snapshot: SessionSnapshot, state: CreateState, reason: str
    ) -> Attempt | RunOutcome:
        case = self.case
        attempt.create_state = state
        attempt.status = AttemptStatus.RUNNING
        attempt.devin_session_id = snapshot.session_id
        snapshot = replace(snapshot, url=safe_href(snapshot.url) or "")
        attempt.devin_session_url = snapshot.url
        attempt.devin_status = snapshot.status
        attempt.devin_status_detail = snapshot.status_detail
        attempt.reconciliation_reason = None
        attempt.timeout_at = self.clock() + timedelta(seconds=self._timeout_seconds())
        case.devin_session_id = snapshot.session_id
        case.devin_session_url = snapshot.url
        attempt_id = attempt.id
        try:
            await self._transition(self.phase.running, reason)
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
        if CaseState(case.state) == self.phase.intent:
            await self._transition(self.phase.reconciling, "reconciling uncertain create")
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
        await self._transition(self.phase.human_blocked, reason)
        self.session.add(_outbox(case, self._human_blocked_kind(), reason=reason))
        await self.session.commit()
        return RunOutcome(RunResult.HUMAN_BLOCKED, attempt, reason=reason)

    def _human_blocked_kind(self) -> str:
        return "remediation_human_blocked" if self.phase is REMEDIATION_PHASE else "human_blocked"

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
                seconds=self._timeout_seconds()
            )
            await self.session.commit()
        deadline = attempt.timeout_at
        consecutive_errors = 0
        while True:
            if await self._cancel_requested():
                attempt.status = AttemptStatus.TERMINATION_PENDING
                attempt.reconciliation_reason = CANCEL_TERMINATION_REASON
                await self.session.commit()
                return await self._handle_timeout(attempt)
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
            mapping = self._disposition(snapshot)
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

    async def _cancel_requested(self) -> bool:
        """Re-read the case state so an operator cancel interrupts an in-flight poll loop."""
        fresh = await self.session.scalar(select(Case.state).where(Case.id == self.case.id))
        if fresh is None:
            return False
        state = CaseState(fresh)
        if state != CaseState(self.case.state):
            await self.session.refresh(self.case)
        return state in TERMINATION_PENDING_STATES

    @staticmethod
    def _disposition(snapshot: SessionSnapshot) -> StatusMapping:
        """Status mapping with structured output taking precedence over an idle session.

        An interactive Devin session reports ``waiting_for_user`` ("awaiting instructions")
        after it has completed its task and submitted ``structured_output``. That is not a
        blocking question: when the snapshot carries structured output the attempt is
        settled as FINISHED and the output is validated downstream (a malformed document
        fails the attempt there). Only a waiting session *without* output is a human block.
        Only structured API fields are consulted; chat text is never inspected.
        """
        mapping = classify(snapshot.status, snapshot.status_detail)
        if (
            mapping.disposition == Disposition.WAITING_FOR_HUMAN
            and snapshot.structured_output is not None
        ):
            return StatusMapping(
                Disposition.FINISHED,
                f"structured output submitted ({mapping.reason}; treated as finished)",
                mapping.remote_terminal,
            )
        return mapping

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
            await self._transition(self.phase.human_blocked, reason)
            self.session.add(
                _outbox(
                    self.case, self._human_blocked_kind(), reason=reason, devin_url=snapshot.url
                )
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
        if self.phase is REMEDIATION_PHASE:
            attempt.failure_stage = "session"
            attempt.failure_class = FAILURE_CLASS_SESSION
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
            mapping = self._disposition(final)
            if mapping.disposition in {
                Disposition.FINISHED,
                Disposition.WAITING_FOR_HUMAN,
                Disposition.FAILED,
            }:
                if CaseState(self.case.state) == self.phase.termination_pending and (
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
            pending = attempt.reconciliation_reason or ""
            trigger = (
                pending.split(";")[0]
                if pending.startswith((CANCEL_TERMINATION_REASON, WORKER_ERROR_TERMINATION_PREFIX))
                else "timeout reached"
            )
            reason = f"{trigger}; DELETE failed: {exc}"
            attempt.status = AttemptStatus.TERMINATION_PENDING
            attempt.reconciliation_reason = reason
            if CaseState(self.case.state) != self.phase.termination_pending:
                await self._transition(self.phase.termination_pending, reason)
            self.case.failure_reason = reason
            await self.session.commit()
            return RunOutcome(RunResult.TERMINATION_PENDING, attempt, final, reason)
        return await self._mark_timed_out(attempt, "session terminated remotely")

    async def _terminate_by_tag(self, attempt: Attempt) -> RunOutcome:
        """Termination requested for an attempt whose create outcome is still unknown."""
        try:
            matches = await self.devin.find_sessions_by_tag(attempt.operation_key)
            for match in matches:
                try:
                    await self.devin.terminate_session(match.session_id)
                except DevinSessionNotFound:
                    pass
        except DevinError as exc:
            reason = f"termination pending: could not look up or delete session by tag: {exc}"
            attempt.reconciliation_reason = reason
            self.case.failure_reason = reason
            await self.session.commit()
            return RunOutcome(RunResult.TERMINATION_PENDING, attempt, None, reason)
        detail = (
            f"{len(matches)} session(s) carrying the operation tag terminated"
            if matches
            else "no session carries the operation tag"
        )
        return await self._mark_terminated(attempt, detail)

    async def _mark_timed_out(self, attempt: Attempt, detail: str) -> RunOutcome:
        return await self._mark_terminated(attempt, detail)

    async def _mark_terminated(self, attempt: Attempt, detail: str) -> RunOutcome:
        """Record the confirmed end of a remote session.

        Termination reached through the deadline becomes TIMED_OUT; termination requested
        by an operator cancel becomes CANCELLED; termination forced by a worker error
        becomes FAILED. Terminal case states are only entered here, after confirmation.
        """
        pending = attempt.reconciliation_reason or ""
        now = self.clock()
        phase = self.phase
        expired = attempt.timeout_at is not None and now >= attempt.timeout_at
        if pending.startswith(CANCEL_TERMINATION_REASON) and not expired:
            reason = f"{CANCEL_TERMINATION_REASON}; {detail}"
            attempt_status, case_state, kind = (
                AttemptStatus.CANCELLED,
                phase.cancelled,
                _outbox_kind(phase, "cancelled"),
            )
            result = RunResult.FAILED
        elif pending.startswith(WORKER_ERROR_TERMINATION_PREFIX) and not expired:
            reason = f"{pending}; {detail}"
            attempt_status, case_state, kind = (
                AttemptStatus.FAILED,
                phase.failed,
                _outbox_kind(phase, "failed"),
            )
            result = RunResult.FAILED
        else:
            deadline = attempt.timeout_at.isoformat() if attempt.timeout_at else "?"
            reason = f"Devin session exceeded deadline {deadline}; {detail}"
            attempt_status, case_state, kind = (
                AttemptStatus.TIMED_OUT,
                phase.timed_out,
                _outbox_kind(phase, "timed_out"),
            )
            result = RunResult.TIMED_OUT
        attempt.status = attempt_status
        attempt.error = reason
        attempt.reconciliation_reason = None
        attempt.finished_at = now
        self.case.failure_reason = reason
        await self._transition(case_state, reason)
        self.session.add(_outbox(self.case, kind, reason=reason))
        await self.session.commit()
        return RunOutcome(result, attempt, None, reason)


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


async def terminate_running_attempts(session: AsyncSession, case: Case, devin: DevinClient) -> bool:
    """Terminate every session an active attempt owns or may own.

    Returns False when at least one termination could not be confirmed; such attempts
    are parked as TERMINATION_PENDING so the case is retried rather than closed.
    """
    attempts = list(
        (
            await session.scalars(
                select(Attempt).where(
                    Attempt.case_id == case.id,
                    Attempt.status.in_([*ACTIVE_ATTEMPT_STATUSES, AttemptStatus.BLOCKED]),
                )
            )
        ).all()
    )
    confirmed = True
    for attempt in attempts:
        try:
            if attempt.devin_session_id:
                await devin.terminate_session(attempt.devin_session_id)
            elif attempt.create_sent_at is not None:
                for match in await devin.find_sessions_by_tag(attempt.operation_key):
                    try:
                        await devin.terminate_session(match.session_id)
                    except DevinSessionNotFound:
                        pass
        except DevinSessionNotFound:
            pass
        except DevinError as exc:
            logger.warning(
                "could not terminate Devin session for attempt %s: %s", attempt.operation_key, exc
            )
            attempt.status = AttemptStatus.TERMINATION_PENDING
            attempt.reconciliation_reason = f"{CANCEL_TERMINATION_REASON}; DELETE failed: {exc}"
            confirmed = False
            continue
        attempt.status = AttemptStatus.CANCELLED
        attempt.error = CANCEL_TERMINATION_REASON
        attempt.reconciliation_reason = None
        attempt.finished_at = datetime.now(UTC)
    return confirmed
