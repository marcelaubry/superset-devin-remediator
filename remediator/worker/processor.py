import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import metrics
from ..adapters import build_github_client
from ..approvals import (
    confirm_label_webhook,
    create_approval_request,
    is_remediation_label_event,
    issue_label_names,
)
from ..capacity import CapacityManager
from ..config import Settings
from ..devin.client import DevinClient
from ..devin.triage import TriageValidationError, validate_triage_output
from ..github.client import GitHubIssuesClient
from ..github_refs import BaseCommitResolver, build_base_commit_resolver
from ..lifecycle import REMEDIATION_PHASE_STATES, CaseState, phase_for_state, transition
from ..models import (
    ACTIVE_ATTEMPT_STATUSES,
    CANCEL_TERMINATION_REASON,
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    EventStatus,
    NotificationOutbox,
    OutboxChannel,
    WebhookEvent,
)
from ..probes import build_probe_runner
from ..probes.runner import ProbeRunner
from ..rubric import IssueSnapshot, evaluate
from ..safe_urls import safe_href
from .devin_runner import (
    DevinRunner,
    RunOutcome,
    RunResult,
    fail_case,
    terminate_running_attempts,
)
from .remediation import (
    REMEDIATION_WORK_STATES,
    RemediationPipeline,
    resumable_missing_probe_block,
)

__all__ = [
    "fail_case",
    "process_case",
    "process_event",
    "reconcile_blocked_triage",
    "terminate_running_attempts",
]

logger = logging.getLogger(__name__)

# Case states in which the worker holds (or must re-attach to) a live Devin attempt.
RESUMABLE_STATES = frozenset(
    {
        CaseState.TRIAGE_CREATE_INTENT,
        CaseState.TRIAGING,
        CaseState.RECONCILING_CREATE,
        CaseState.REMEDIATION_CREATE_INTENT,
        CaseState.REMEDIATION_RECONCILING_CREATE,
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
    # Every schema-valid triage result goes to an authorized human, whatever Devin recommended;
    # the recommendation is preserved verbatim and Slack warns when it is not
    # `remediation_candidate`. The Slack notification is only an outbox row here; delivery
    # happens asynchronously and its failure can never roll back the validated triage result.
    await create_approval_request(session, case, attempt, result, settings)
    await transition(
        session,
        case,
        CaseState.AWAITING_REMEDIATION_APPROVAL,
        f"remediation approval requested (Devin recommendation: {result.outcome})",
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
    metrics.eligibility_outcomes_total.labels(
        metrics.mode(), "eligible" if result.eligible else "rejected"
    ).inc()
    if not result.eligible:
        reason = "insufficient context for triage: " + "; ".join(result.missing)
        logger.info("case %s policy-rejected: %s", case.id, reason)
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
                kind="eligibility_rejected",
                payload={
                    "reason": "insufficient_context",
                    "missing": list(result.missing),
                    "advisory_recommendation": result.recommendation.value,
                },
            )
        )
        await session.commit()
        return False
    await transition(
        session,
        case,
        CaseState.TRIAGE_CREATE_INTENT,
        f"triage requested (context complete; advisory {result.recommendation.value})",
        "worker",
        expected_claimed_by=claimed_by,
    )
    await session.commit()
    return True


async def _terminate_case(
    session: AsyncSession,
    case: Case,
    devin: DevinClient,
    claimed_by: str | None = None,
    capacity: CapacityManager | None = None,
) -> None:
    if not await terminate_running_attempts(session, case, devin):
        case.failure_reason = "operator requested cancel; Devin session termination pending"
        await session.commit()
        return
    if capacity is not None:
        # Only now is the remote session confirmed gone; the slot may be reused.
        await capacity.release_all_for_case(session, case.id, "remote termination confirmed")
    await transition(
        session,
        case,
        phase_for_state(case.state).cancelled,
        CANCEL_TERMINATION_REASON,
        "worker",
        expected_claimed_by=claimed_by,
    )
    await session.commit()


def probe_runner_from_settings(settings: Settings) -> ProbeRunner:
    return build_probe_runner(
        settings.probe_runner_mode,
        settings.probe_verifier_url,
        settings.probe_verifier_secret_value,
        require_isolation=settings.probe_verifier_isolation_required,
        request_timeout_seconds=settings.probe_verifier_request_timeout_seconds,
    )


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
    github: GitHubIssuesClient | None = None,
    probes: ProbeRunner | None = None,
    capacity: CapacityManager | None = None,
) -> None:
    resolver = resolver or build_base_commit_resolver(settings)
    capacity = capacity or CapacityManager.from_settings(settings, claimed_by or "worker")
    state = CaseState(case.state)
    if state in REMEDIATION_PHASE_STATES:
        if state not in REMEDIATION_WORK_STATES and not resumable_missing_probe_block(
            case, settings
        ):
            return
        pipeline = RemediationPipeline(
            session,
            case,
            devin,
            github or build_github_client(settings),
            probes or probe_runner_from_settings(settings),
            settings,
            resolver,
            claimed_by=claimed_by,
            capacity=capacity,
        )
        await pipeline.process()
        return
    runner = DevinRunner(
        session, case, devin, settings, resolver, claimed_by=claimed_by, capacity=capacity
    )
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
    if state == CaseState.HUMAN_BLOCKED:
        await reconcile_blocked_triage(session, case, devin, settings, claimed_by, resolver)
    if state == CaseState.TERMINATION_PENDING:
        pending = await _pending_termination_attempt(session, case)
        if pending is None:
            await _terminate_case(session, case, devin, claimed_by, capacity)
        else:
            outcome = await runner.run(pending.kind)
            if pending.kind == AttemptKind.TRIAGE:
                await _finish_triage(session, case, outcome, settings, claimed_by)


async def reconcile_blocked_triage(
    session: AsyncSession,
    case: Case,
    devin: DevinClient,
    settings: Settings,
    claimed_by: str | None = None,
    resolver: BaseCommitResolver | None = None,
) -> RunOutcome | None:
    """Re-read the retained session(s) of a HUMAN_BLOCKED case and ingest structured output.

    GET only: no session is ever created here. When the newest retained session carries
    structured output the case advances exactly as a live poll would have (validation,
    one approval round, one Slack notification); otherwise the case stays HUMAN_BLOCKED
    with the attempt annotated so an operator can tell the session was re-read.
    """
    if CaseState(case.state) != CaseState.HUMAN_BLOCKED:
        return None
    runner = DevinRunner(
        session,
        case,
        devin,
        settings,
        resolver or build_base_commit_resolver(settings),
        claimed_by=claimed_by,
    )
    outcome = await runner.reconcile_blocked(AttemptKind.TRIAGE)
    if outcome is None:
        return None
    await _finish_triage(session, case, outcome, settings, claimed_by)
    return outcome


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
    capacity: CapacityManager | None = None,
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
            issue_url=safe_href(str(issue.get("html_url", ""))) or "",
            state=CaseState.RECEIVED,
        )
        session.add(case)
        try:
            await session.flush()
            metrics.cases_received_total.labels(metrics.mode()).inc()
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
    await process_case(session, case, devin, settings, claimed_by, resolver, capacity=capacity)
    event.status = EventStatus.PROCESSED
    event.last_error = None
    event.processed_at = datetime.now(UTC)
    await session.commit()
