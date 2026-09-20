import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..approvals import (
    confirm_label_webhook,
    create_approval_request,
    is_remediation_label_event,
    issue_label_names,
)
from ..config import Settings
from ..devin.client import DevinClient, DevinError, DevinSessionNotFound
from ..devin.triage import TriageValidationError, validate_triage_output
from ..github_refs import BaseCommitResolver, build_base_commit_resolver
from ..lifecycle import CaseState, transition
from ..models import (
    ACTIVE_ATTEMPT_STATUSES,
    CANCEL_TERMINATION_REASON,
    OUTBOX_KIND_GITHUB_NOT_FEASIBLE,
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    EventStatus,
    NotificationOutbox,
    OutboxChannel,
    Recommendation,
    WebhookEvent,
)
from ..rubric import IssueSnapshot, evaluate
from .devin_runner import DevinRunner, RunOutcome, RunResult, fail_case

__all__ = ["fail_case", "process_case", "process_event"]

logger = logging.getLogger(__name__)

# Case states in which the worker holds (or must re-attach to) a live Devin attempt.
RESUMABLE_STATES = frozenset(
    {
        CaseState.TRIAGE_CREATE_INTENT,
        CaseState.TRIAGING,
        CaseState.RECONCILING_CREATE,
        CaseState.REMEDIATION_CREATE_INTENT,
        CaseState.REMEDIATING,
    }
)


async def _source_issue(session: AsyncSession, case: Case) -> dict[str, Any]:
    source_event = await session.scalar(
        select(WebhookEvent)
        .where(WebhookEvent.case_id == case.id)
        .order_by(desc(WebhookEvent.received_at))
        .limit(1)
    )
    if source_event is None:
        raise ValueError(f"case {case.id} has no source webhook event")
    return cast(dict[str, Any], source_event.payload.get("issue", {}))


async def _finish_triage(
    session: AsyncSession,
    case: Case,
    outcome: RunOutcome,
    settings: Settings,
    claimed_by: str | None = None,
) -> None:
    if outcome.result != RunResult.FINISHED:
        return
    attempt = outcome.attempt
    try:
        result = validate_triage_output(attempt.structured_output)
    except TriageValidationError as exc:
        reason = f"triage output rejected: {exc}"
        attempt.status = AttemptStatus.FAILED
        attempt.error = reason
        attempt.structured_output = None
        await fail_case(session, case, reason, "worker", claimed_by)
        await session.commit()
        return
    attempt.status = AttemptStatus.SUCCEEDED
    attempt.structured_output = result.raw
    await transition(
        session,
        case,
        CaseState.TRIAGED,
        f"triage finished: {result.outcome} ({result.severity}/{result.priority}, "
        f"confidence {result.confidence:.2f})",
        "worker",
        expected_claimed_by=claimed_by,
    )
    if not result.remediation_candidate:
        reason = f"triage outcome {result.outcome}: {result.summary}"
        await transition(
            session,
            case,
            CaseState.POLICY_REJECTED,
            reason,
            "worker",
            expected_claimed_by=claimed_by,
        )
        session.add(
            NotificationOutbox(
                case_id=case.id,
                channel=OutboxChannel.GITHUB,
                kind=OUTBOX_KIND_GITHUB_NOT_FEASIBLE,
                payload={
                    "issue_number": case.issue_number,
                    "outcome": result.outcome,
                    "summary": result.summary,
                    "blocking_questions": list(result.blocking_questions),
                },
            )
        )
        await session.commit()
        return
    # The Slack notification is only an outbox row here; delivery happens asynchronously
    # and its failure can never roll back the validated triage result.
    await create_approval_request(session, case, attempt, result, settings)
    await transition(
        session,
        case,
        CaseState.AWAITING_REMEDIATION_APPROVAL,
        "remediation approval requested",
        "worker",
        expected_claimed_by=claimed_by,
    )
    await session.commit()


async def _evaluate_eligibility(session: AsyncSession, case: Case, claimed_by: str | None) -> bool:
    issue = await _source_issue(session, case)
    result = evaluate(
        IssueSnapshot(
            str(issue.get("title", "")),
            str(issue.get("body", "")),
            [str(label.get("name", "")) for label in issue.get("labels", [])],
        )
    )
    case.recommendation = result.recommendation
    case.rubric = [
        {"name": check.name, "passed": check.passed, "reason": check.reason}
        for check in result.checks
    ]
    await transition(
        session,
        case,
        CaseState.ELIGIBILITY_EVALUATED,
        "; ".join(f"{c.name}: {c.reason}" for c in result.checks),
        "worker",
        expected_claimed_by=claimed_by,
    )
    await session.commit()
    if result.recommendation != Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE:
        await transition(
            session,
            case,
            CaseState.POLICY_REJECTED,
            f"recommendation {result.recommendation.value}",
            "worker",
            expected_claimed_by=claimed_by,
        )
        session.add(
            NotificationOutbox(
                case_id=case.id,
                channel=OutboxChannel.GITHUB,
                kind="eligibility_rejected",
                payload={"recommendation": result.recommendation.value},
            )
        )
        await session.commit()
        return False
    await transition(
        session,
        case,
        CaseState.TRIAGE_CREATE_INTENT,
        "triage requested",
        "worker",
        expected_claimed_by=claimed_by,
    )
    await session.commit()
    return True


async def _process_remediation(
    session: AsyncSession,
    case: Case,
    runner: DevinRunner,
    claimed_by: str | None = None,
) -> None:
    outcome = await runner.run(AttemptKind.REMEDIATION)
    if outcome.result != RunResult.FINISHED:
        return
    outcome.attempt.status = AttemptStatus.SUCCEEDED
    await transition(
        session,
        case,
        CaseState.OUTPUT_VALIDATING,
        "validating Devin output",
        "worker",
        expected_claimed_by=claimed_by,
    )
    await session.commit()
    pr_url = (outcome.attempt.structured_output or {}).get("pr_url")
    if not isinstance(pr_url, str) or not pr_url:
        reason = "missing pr_url in structured output"
        await fail_case(session, case, reason, "worker", claimed_by)
        await session.commit()
        return
    case.pr_url = pr_url
    try:
        case.pr_number = int(pr_url.rsplit("/", 1)[-1])
    except ValueError:
        case.pr_number = None
    for state, reason in (
        (CaseState.PR_VALIDATED, "structured PR output validated"),
        (CaseState.CI_PENDING, "waiting for CI"),
    ):
        await transition(session, case, state, reason, "worker", expected_claimed_by=claimed_by)
        await session.commit()
    case.ci_status = "success"
    await transition(
        session,
        case,
        CaseState.CI_PASSED,
        "fake CI passed",
        "worker",
        expected_claimed_by=claimed_by,
    )
    session.add(
        NotificationOutbox(
            case_id=case.id,
            channel=OutboxChannel.GITHUB,
            kind="case_completed",
            payload={"pr_url": pr_url},
        )
    )
    await session.commit()


async def _terminate_running_attempts(
    session: AsyncSession, case: Case, devin: DevinClient
) -> bool:
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


async def _terminate_case(
    session: AsyncSession,
    case: Case,
    devin: DevinClient,
    claimed_by: str | None = None,
) -> None:
    if not await _terminate_running_attempts(session, case, devin):
        case.failure_reason = "operator requested cancel; Devin session termination pending"
        await session.commit()
        return
    await transition(
        session,
        case,
        CaseState.CANCELLED,
        CANCEL_TERMINATION_REASON,
        "worker",
        expected_claimed_by=claimed_by,
    )
    await session.commit()


async def _pending_termination_attempt(session: AsyncSession, case: Case) -> Attempt | None:
    attempt: Attempt | None = await session.scalar(
        select(Attempt)
        .where(Attempt.case_id == case.id, Attempt.status == AttemptStatus.TERMINATION_PENDING)
        .limit(1)
    )
    return attempt


async def process_case(
    session: AsyncSession,
    case: Case,
    devin: DevinClient,
    settings: Settings,
    claimed_by: str | None = None,
    resolver: BaseCommitResolver | None = None,
) -> None:
    resolver = resolver or build_base_commit_resolver(settings)
    runner = DevinRunner(session, case, devin, settings, resolver, claimed_by=claimed_by)
    state = CaseState(case.state)
    if state == CaseState.RECEIVED:
        if not await _evaluate_eligibility(session, case, claimed_by):
            return
        state = CaseState(case.state)
    if state in {CaseState.TRIAGE_CREATE_INTENT, CaseState.TRIAGING} or (
        state == CaseState.RECONCILING_CREATE
        and await _active_kind(session, case) == AttemptKind.TRIAGE
    ):
        outcome = await runner.run(AttemptKind.TRIAGE)
        await _finish_triage(session, case, outcome, settings, claimed_by)
    if CaseState(case.state) in {
        CaseState.REMEDIATION_CREATE_INTENT,
        CaseState.REMEDIATING,
        CaseState.RECONCILING_CREATE,
    }:
        await _process_remediation(session, case, runner, claimed_by)
    if state == CaseState.TERMINATION_PENDING:
        pending = await _pending_termination_attempt(session, case)
        if pending is None:
            await _terminate_case(session, case, devin, claimed_by)
        else:
            outcome = await runner.run(pending.kind)
            if pending.kind == AttemptKind.TRIAGE:
                await _finish_triage(session, case, outcome, settings, claimed_by)


async def _active_kind(session: AsyncSession, case: Case) -> AttemptKind | None:
    kind: AttemptKind | None = await session.scalar(
        select(Attempt.kind)
        .where(Attempt.case_id == case.id, Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES))
        .order_by(desc(Attempt.started_at))
        .limit(1)
    )
    return kind


async def process_event(
    session: AsyncSession,
    event: WebhookEvent,
    devin: DevinClient,
    settings: Settings,
    claimed_by: str | None = None,
    resolver: BaseCommitResolver | None = None,
) -> None:
    issue = event.payload.get("issue", {})
    case = await session.scalar(
        select(Case).where(
            Case.repository == event.repository, Case.issue_number == issue.get("number")
        )
    )
    if is_remediation_label_event(event, settings.github_remediation_label):
        confirmed = False
        if case is not None:
            confirmed = await confirm_label_webhook(
                session,
                case,
                settings.github_remediation_label,
                event.delivery_id,
                issue_labels=issue_label_names(issue),
            )
            event.case_id = case.id
        logger.info(
            "remediation label webhook %s for %s#%s: %s",
            event.delivery_id,
            event.repository,
            issue.get("number"),
            "confirmed approval" if confirmed else "ignored (no matching delivered approval)",
        )
        event.status = EventStatus.PROCESSED
        event.last_error = None if confirmed else "label webhook without matching approval"
        event.processed_at = datetime.now(UTC)
        await session.commit()
        return
    if case is None:
        case = Case(
            repository=event.repository,
            issue_number=int(issue["number"]),
            issue_title=str(issue.get("title", "")),
            issue_url=str(issue.get("html_url", "")),
            state=CaseState.RECEIVED,
        )
        session.add(case)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            case = await session.scalar(
                select(Case).where(
                    Case.repository == event.repository, Case.issue_number == issue.get("number")
                )
            )
            if case is None:
                raise
    if CaseState(case.state) != CaseState.RECEIVED:
        logger.info(
            "ignoring delivery %s for case %s already in %s",
            event.delivery_id,
            case.id,
            case.state,
        )
        event.case_id = case.id
        event.status = EventStatus.PROCESSED
        event.last_error = None
        event.processed_at = datetime.now(UTC)
        await session.commit()
        return
    if claimed_by is not None and case.claimed_by is None:
        case.claimed_by = claimed_by
        case.lease_expires_at = datetime.now(UTC) + timedelta(seconds=settings.worker_lease_seconds)
    event.case_id = case.id
    await session.commit()
    await process_case(session, case, devin, settings, claimed_by, resolver)
    event.status = EventStatus.PROCESSED
    event.last_error = None
    event.processed_at = datetime.now(UTC)
    await session.commit()
