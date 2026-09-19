import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..approvals import approve_remediation
from ..config import Settings
from ..devin.client import DevinClient, DevinSession
from ..lifecycle import TERMINAL_STATES, CaseState, InvalidTransition, transition
from ..models import (
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

logger = logging.getLogger(__name__)


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
        session.add(
            NotificationOutbox(
                case_id=case.id,
                channel=OutboxChannel.GITHUB,
                kind="case_failed",
                payload={"issue_number": case.issue_number, "reason": reason},
            )
        )


async def _timeout_case(
    session: AsyncSession, case: Case, budget: int, claimed_by: str | None = None
) -> None:
    reason = f"Devin poll budget exhausted after {budget} polls"
    await transition(
        session, case, CaseState.TIMED_OUT, reason, "worker", expected_claimed_by=claimed_by
    )
    session.add(
        NotificationOutbox(
            case_id=case.id,
            channel=OutboxChannel.GITHUB,
            kind="case_timed_out",
            payload={"issue_number": case.issue_number, "reason": reason},
        )
    )


async def _run_devin(
    session: AsyncSession,
    case: Case,
    devin: DevinClient,
    kind: AttemptKind,
    settings: Settings,
    claimed_by: str | None = None,
) -> DevinSession:
    target_state = CaseState.TRIAGING if kind == AttemptKind.TRIAGE else CaseState.REMEDIATING
    prompt = f"{kind.value.lower()} issue #{case.issue_number}: {case.issue_title}"
    attempt = await session.scalar(
        select(Attempt)
        .where(Attempt.case_id == case.id, Attempt.kind == kind)
        .order_by(desc(Attempt.started_at))
        .limit(1)
    )
    if attempt and attempt.status == AttemptStatus.RUNNING and attempt.devin_session_id is None:
        await transition(
            session,
            case,
            CaseState.RECONCILING_CREATE,
            "reconciling Devin create",
            "worker",
            expected_claimed_by=claimed_by,
        )
        await session.commit()
        current = await devin.find_session(attempt.idempotency_key)
        if current is None:
            await asyncio.sleep(settings.reconcile_retry_delay_seconds)
            current = await devin.find_session(attempt.idempotency_key)
        if current is None:
            attempt.status = AttemptStatus.FAILED
            attempt.error = "create intent could not be reconciled after retry"
            attempt.finished_at = datetime.now(UTC)
            await fail_case(session, case, attempt.error, "worker", claimed_by)
            await session.commit()
            return DevinSession("", "", "failed", error=attempt.error)
        attempt.devin_session_id = current.session_id
        case.devin_session_id = current.session_id
        case.devin_session_url = current.url
        await transition(
            session,
            case,
            target_state,
            "Devin session reconciled",
            "worker",
            expected_claimed_by=claimed_by,
        )
        await session.commit()
    elif attempt and attempt.status == AttemptStatus.RUNNING and attempt.devin_session_id:
        current = await devin.get_session(attempt.devin_session_id)
    else:
        count = await session.scalar(
            select(func.count())
            .select_from(Attempt)
            .where(Attempt.case_id == case.id, Attempt.kind == kind)
        )
        if count is None or count >= settings.max_attempts_per_kind:
            reason = "attempt cap reached"
            await fail_case(session, case, reason, "worker", claimed_by)
            await session.commit()
            return DevinSession("", "", "failed", error=reason)
        idempotency_key = f"{case.id}:{kind.value}:{int(count) + 1}"
        attempt = Attempt(
            case_id=case.id,
            kind=kind,
            idempotency_key=idempotency_key,
            devin_session_id=None,
            status=AttemptStatus.RUNNING,
        )
        session.add(attempt)
        await session.commit()
        current = await devin.create_session(
            prompt,
            {
                "repository": case.repository,
                "issue_number": str(case.issue_number),
                "kind": kind.value,
            },
            idempotency_key=idempotency_key,
        )
        attempt.devin_session_id = current.session_id
        attempt_id = attempt.id
        case.devin_session_id = current.session_id
        case.devin_session_url = current.url
        try:
            await transition(
                session,
                case,
                target_state,
                "Devin session created",
                "worker",
                expected_claimed_by=claimed_by,
            )
        except InvalidTransition:
            await session.rollback()
            now = datetime.now(UTC)
            await session.execute(
                update(Attempt)
                .where(Attempt.id == attempt_id)
                .values(
                    devin_session_id=current.session_id,
                    status=AttemptStatus.CANCELLED,
                    error="orphaned: lease lost during create",
                    finished_at=now,
                )
            )
            await session.commit()
            await devin.terminate_session(current.session_id)
            logger.warning("terminated orphaned Devin session %s", current.session_id)
            raise
        await session.commit()

    for _ in range(settings.devin_max_polls):
        if current.status != "working":
            break
        current = await devin.get_session(current.session_id)
        if current.status != "working":
            break
        await asyncio.sleep(settings.devin_poll_interval_seconds)

    if current.status == "failed":
        attempt.status = AttemptStatus.FAILED
        attempt.error = current.error
        attempt.finished_at = datetime.now(UTC)
        await fail_case(session, case, current.error or "Devin failed", "worker", claimed_by)
        await session.commit()
    elif current.status == "blocked":
        attempt.status = AttemptStatus.BLOCKED
        attempt.finished_at = datetime.now(UTC)
        await transition(
            session,
            case,
            CaseState.HUMAN_BLOCKED,
            "Devin requested human intervention",
            "worker",
            expected_claimed_by=claimed_by,
        )
        session.add(
            NotificationOutbox(
                case_id=case.id,
                channel=OutboxChannel.GITHUB,
                kind="human_blocked",
                payload={"issue_number": case.issue_number},
            )
        )
        await session.commit()
    elif current.status == "finished":
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.finished_at = datetime.now(UTC)
    else:
        await devin.terminate_session(current.session_id)
        attempt.status = AttemptStatus.CANCELLED
        attempt.error = "poll budget exhausted"
        attempt.finished_at = datetime.now(UTC)
        await _timeout_case(session, case, settings.devin_max_polls, claimed_by)
        await session.commit()
    return current


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
    triage: DevinSession,
    settings: Settings,
    claimed_by: str | None = None,
) -> None:
    if triage.status != "finished":
        return
    await transition(
        session,
        case,
        CaseState.TRIAGED,
        "triage finished",
        "worker",
        expected_claimed_by=claimed_by,
    )
    triage_output = triage.output or {}
    feasible = bool(triage_output.get("remediation_feasible"))
    summary = str(triage_output.get("summary", ""))
    if not feasible:
        reason = f"triage: remediation not feasible — {summary}"
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
                kind="triage_not_feasible",
                payload={"issue_number": case.issue_number, "summary": summary},
            )
        )
        await session.commit()
        return
    session.add(
        NotificationOutbox(
            case_id=case.id,
            channel=OutboxChannel.SLACK,
            kind="remediation_approval_requested",
            payload={
                "issue_number": case.issue_number,
                "issue_url": case.issue_url,
                "devin_session_url": case.devin_session_url,
                "summary": summary,
            },
        )
    )
    await transition(
        session,
        case,
        CaseState.AWAITING_REMEDIATION_APPROVAL,
        "remediation approval requested",
        "worker",
        expected_claimed_by=claimed_by,
    )
    await session.commit()
    if settings.simulation_auto_approve_remediation:
        await approve_remediation(session, case, "simulation", claimed_by)
        await session.commit()


async def _process_eligibility_and_triage(
    session: AsyncSession,
    case: Case,
    devin: DevinClient,
    settings: Settings,
    claimed_by: str | None = None,
) -> None:
    if CaseState(case.state) == CaseState.RECEIVED:
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
            return
        await transition(
            session,
            case,
            CaseState.TRIAGE_CREATE_INTENT,
            "triage requested",
            "worker",
            expected_claimed_by=claimed_by,
        )
        await session.commit()
    triage = await _run_devin(session, case, devin, AttemptKind.TRIAGE, settings, claimed_by)
    await _finish_triage(session, case, triage, settings, claimed_by)


async def _process_remediation(
    session: AsyncSession,
    case: Case,
    devin: DevinClient,
    settings: Settings,
    claimed_by: str | None = None,
) -> None:
    remediation = await _run_devin(
        session, case, devin, AttemptKind.REMEDIATION, settings, claimed_by
    )
    if remediation.status != "finished":
        return
    await transition(
        session,
        case,
        CaseState.OUTPUT_VALIDATING,
        "validating Devin output",
        "worker",
        expected_claimed_by=claimed_by,
    )
    await session.commit()
    pr_url = (remediation.output or {}).get("pr_url")
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
    await transition(
        session,
        case,
        CaseState.PR_VALIDATED,
        "structured PR output validated",
        "worker",
        expected_claimed_by=claimed_by,
    )
    await session.commit()
    await transition(
        session,
        case,
        CaseState.CI_PENDING,
        "waiting for CI",
        "worker",
        expected_claimed_by=claimed_by,
    )
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
) -> None:
    attempts = list(
        (
            await session.scalars(
                select(Attempt).where(
                    Attempt.case_id == case.id, Attempt.status == AttemptStatus.RUNNING
                )
            )
        ).all()
    )
    for attempt in attempts:
        if attempt.devin_session_id:
            await devin.terminate_session(attempt.devin_session_id)
        attempt.status = AttemptStatus.CANCELLED
        attempt.error = "operator requested cancel"
        attempt.finished_at = datetime.now(UTC)


async def _terminate_case(
    session: AsyncSession,
    case: Case,
    devin: DevinClient,
    claimed_by: str | None = None,
) -> None:
    await _terminate_running_attempts(session, case, devin)
    await transition(
        session,
        case,
        CaseState.CANCELLED,
        "operator requested cancel",
        "worker",
        expected_claimed_by=claimed_by,
    )
    await session.commit()


async def process_case(
    session: AsyncSession,
    case: Case,
    devin: DevinClient,
    settings: Settings,
    claimed_by: str | None = None,
) -> None:
    state = CaseState(case.state)
    if state in {CaseState.RECEIVED, CaseState.TRIAGE_CREATE_INTENT}:
        await _process_eligibility_and_triage(session, case, devin, settings, claimed_by)
    if CaseState(case.state) == CaseState.REMEDIATION_CREATE_INTENT:
        await _process_remediation(session, case, devin, settings, claimed_by)
    if CaseState(case.state) == CaseState.TERMINATION_PENDING:
        await _terminate_case(session, case, devin, claimed_by)


async def process_event(
    session: AsyncSession,
    event: WebhookEvent,
    devin: DevinClient,
    settings: Settings,
    claimed_by: str | None = None,
) -> None:
    issue = event.payload.get("issue", {})
    case = await session.scalar(
        select(Case).where(
            Case.repository == event.repository, Case.issue_number == issue.get("number")
        )
    )
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
        except Exception as exc:
            from sqlalchemy.exc import IntegrityError

            if not isinstance(exc, IntegrityError):
                raise
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
    await process_case(session, case, devin, settings, claimed_by)
    event.status = EventStatus.PROCESSED
    event.last_error = None
    event.processed_at = datetime.now(UTC)
    await session.commit()
