import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..devin.client import DevinClient, DevinSession
from ..lifecycle import CaseState, transition
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


async def _run_devin(
    session: AsyncSession, case: Case, devin: DevinClient, kind: AttemptKind, settings: Settings
) -> DevinSession:
    prompt = f"{kind.value.lower()} issue #{case.issue_number}: {case.issue_title}"
    created = await devin.create_session(
        prompt,
        {"repository": case.repository, "issue_number": str(case.issue_number), "kind": kind.value},
    )
    attempt = Attempt(
        case_id=case.id,
        kind=kind,
        devin_session_id=created.session_id,
        status=AttemptStatus.RUNNING,
    )
    session.add(attempt)
    case.devin_session_id = created.session_id
    case.devin_session_url = created.url
    await session.commit()
    current = created
    for _ in range(5):
        current = await devin.get_session(created.session_id)
        if current.status != "working":
            break
        await asyncio.sleep(0.01)
    if current.status == "failed":
        attempt.status = AttemptStatus.FAILED
        attempt.error = current.error
        attempt.finished_at = datetime.now(UTC)
        case.failure_reason = current.error
        await transition(session, case, CaseState.FAILED, current.error or "Devin failed", "worker")
        await session.commit()
    elif current.status == "blocked":
        attempt.status = AttemptStatus.BLOCKED
        attempt.finished_at = datetime.now(UTC)
        await transition(
            session, case, CaseState.HUMAN_BLOCKED, "Devin requested human intervention", "worker"
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
    else:
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.finished_at = datetime.now(UTC)
        await session.commit()
    return current


async def process_case(
    session: AsyncSession, case: Case, devin: DevinClient, settings: Settings
) -> None:
    source_event = await session.scalar(
        select(WebhookEvent)
        .where(WebhookEvent.case_id == case.id)
        .order_by(desc(WebhookEvent.received_at))
        .limit(1)
    )
    if source_event is None:
        raise ValueError(f"case {case.id} has no source webhook event")
    issue: dict[str, Any] = source_event.payload.get("issue", {})
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
    )
    await session.commit()
    if result.recommendation != Recommendation.GOOD_CANDIDATE:
        await transition(
            session,
            case,
            CaseState.POLICY_REJECTED,
            f"recommendation {result.recommendation.value}",
            "worker",
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
        session, case, CaseState.AWAITING_TRIAGE_APPROVAL, "triage approval requested", "worker"
    )
    await session.commit()
    if not settings.simulation_auto_approve:
        return
    await transition(
        session,
        case,
        CaseState.TRIAGE_CREATE_INTENT,
        "auto-approved: SIMULATION_AUTO_APPROVE=true",
        "worker",
    )
    await session.commit()
    await transition(session, case, CaseState.TRIAGING, "triage session created", "worker")
    triage = await _run_devin(session, case, devin, AttemptKind.TRIAGE, settings)
    if triage.status != "finished":
        return
    await transition(session, case, CaseState.TRIAGED, "triage finished", "worker")
    await session.commit()
    await transition(
        session,
        case,
        CaseState.AWAITING_REMEDIATION_APPROVAL,
        "remediation approval requested",
        "worker",
    )
    await session.commit()
    await transition(
        session,
        case,
        CaseState.REMEDIATION_CREATE_INTENT,
        "auto-approved: SIMULATION_AUTO_APPROVE=true",
        "worker",
    )
    await session.commit()
    await transition(session, case, CaseState.REMEDIATING, "remediation session created", "worker")
    remediation = await _run_devin(session, case, devin, AttemptKind.REMEDIATION, settings)
    if remediation.status != "finished":
        return
    await transition(
        session, case, CaseState.OUTPUT_VALIDATING, "validating Devin output", "worker"
    )
    await session.commit()
    pr_url = (remediation.output or {}).get("pr_url")
    if not isinstance(pr_url, str) or not pr_url:
        case.failure_reason = "missing pr_url in structured output"
        await transition(session, case, CaseState.FAILED, case.failure_reason, "worker")
        await session.commit()
    else:
        case.pr_url = pr_url
        case.pr_number = int(pr_url.rsplit("/", 1)[-1])
        await transition(
            session, case, CaseState.PR_VALIDATED, "structured PR output validated", "worker"
        )
        await session.commit()
        await transition(session, case, CaseState.CI_PENDING, "waiting for CI", "worker")
        await session.commit()
        case.ci_status = "success"
        await transition(session, case, CaseState.CI_PASSED, "fake CI passed", "worker")
        session.add(
            NotificationOutbox(
                case_id=case.id,
                channel=OutboxChannel.SLACK,
                kind="case_completed",
                payload={"pr_url": pr_url},
            )
        )
        await session.commit()


async def process_event(
    session: AsyncSession, event: WebhookEvent, devin: DevinClient, settings: Settings
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
        await session.flush()
    elif CaseState(case.state) != CaseState.RECEIVED:
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
    event.case_id = case.id
    await session.commit()
    await process_case(session, case, devin, settings)
    event.status = EventStatus.PROCESSED
    event.last_error = None
    event.processed_at = datetime.now(UTC)
    await session.commit()
