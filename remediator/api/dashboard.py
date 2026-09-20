from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..lifecycle import (
    REMEDIATION_PHASE,
    REMEDIATION_PHASE_STATES,
    REMEDIATION_RETRYABLE_STATES,
    TERMINAL_STATES,
    CaseState,
    phase_for_state,
)
from ..models import (
    ACTIVE_ATTEMPT_STATUSES,
    FAILURE_CLASS_INFRASTRUCTURE,
    ApprovalRequest,
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    CiSnapshot,
    EventStatus,
    ProbeExecution,
    ProbeSnapshot,
    PullRequestEvidence,
    StateTransition,
    WebhookEvent,
)
from .auth import require_operator
from .hardening import safe_href

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "templates"))


def _humanize(value: datetime | None) -> str:
    if not value:
        return "-"
    seconds = max(0, int((datetime.now(UTC) - value).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _until(value: datetime | None) -> str:
    if not value:
        return "-"
    seconds = int((value - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return "expired"
    return f"in {_humanize(datetime.now(UTC) - timedelta(seconds=seconds))}"


def _attempt_elapsed(attempt: Attempt) -> str:
    start = attempt.create_sent_at or attempt.started_at
    if attempt.finished_at and start:
        return f"{int((attempt.finished_at - start).total_seconds())}s"
    return _humanize(start)


def latest_attempt(case: Case, kind: AttemptKind | None = None) -> Attempt | None:
    attempts = [a for a in case.attempts if kind is None or a.kind == kind]
    if not attempts:
        return None
    floor = datetime.min.replace(tzinfo=UTC)
    return max(attempts, key=lambda attempt: attempt.started_at or floor)


def latest_triage_result(case: Case) -> dict[str, Any] | None:
    attempt = latest_attempt(case, AttemptKind.TRIAGE)
    if attempt is None or attempt.status != AttemptStatus.SUCCEEDED:
        return None
    return attempt.structured_output


def latest_approval(case: Case) -> ApprovalRequest | None:
    if not case.approval_requests:
        return None
    return max(case.approval_requests, key=lambda request: request.created_at)


def approval_json(case: Case) -> dict[str, Any] | None:
    approval = latest_approval(case)
    if approval is None:
        return None
    return {
        "id": str(approval.id),
        "attempt_id": str(approval.attempt_id),
        "triage_schema_version": approval.triage_schema_version,
        "triage_result_hash": approval.triage_result_hash,
        "token_expires_at": approval.token_expires_at.isoformat(),
        "notification_status": approval.notification_status.value,
        "slack_channel": approval.slack_channel,
        "slack_message_ts": approval.slack_message_ts,
        "decision": approval.decision.value,
        "decided_by_slack_user_id": approval.decided_by_slack_user_id,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "decision_action_id": approval.decision_action_id,
        "decision_reason": approval.decision_reason,
        "label_operation": approval.label_operation,
        "delivery_status": approval.delivery_status.value,
        "label_applied_at": (
            approval.label_applied_at.isoformat() if approval.label_applied_at else None
        ),
        "label_confirmed_at": (
            approval.label_confirmed_at.isoformat() if approval.label_confirmed_at else None
        ),
        "github_comment_id": approval.github_comment_id,
        "actions": [
            {
                "slack_user_id": action.slack_user_id,
                "action_id": action.action_id,
                "action_ts": action.action_ts,
                "outcome": action.outcome,
                "created_at": action.created_at.isoformat(),
            }
            for action in approval.actions
        ],
        "events": [
            {
                "seq": event.seq,
                "kind": event.kind,
                "actor": event.actor,
                "detail": event.detail,
                "created_at": event.created_at.isoformat(),
            }
            for event in approval.events
        ],
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _probe_execution_json(run: ProbeExecution) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "target": run.target.value,
        "commit_sha": run.commit_sha,
        "script_hash": run.script_hash,
        "runner_mode": run.runner_mode,
        "command_identity": run.command_identity,
        "expected_exit_code": run.expected_exit_code,
        "exit_code": run.exit_code,
        "verdict": run.verdict.value,
        "timed_out": run.timed_out,
        "duration_ms": run.duration_ms,
        "stdout": run.stdout,
        "stderr": run.stderr,
        "output_truncated": run.output_truncated,
        "error": run.error,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
    }


def _pr_evidence_json(row: PullRequestEvidence) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "repository": row.repository,
        "pr_number": row.pr_number,
        "pr_url": row.pr_url,
        "state": row.state,
        "draft": row.draft,
        "merged": row.merged,
        "base_ref": row.base_ref,
        "base_sha": row.base_sha,
        "head_ref": row.head_ref,
        "head_sha": row.head_sha,
        "head_repository": row.head_repository,
        "author_login": row.author_login,
        "author_type": row.author_type,
        "compare_status": row.compare_status,
        "ahead_by": row.ahead_by,
        "behind_by": row.behind_by,
        "changed_files": list(row.changed_files),
        "closing_reference_source": row.closing_reference_source,
        "closing_issue_numbers": list(row.closing_issue_numbers),
        "checks": list(row.checks),
        "valid": row.valid,
        "verdict": row.verdict,
        "created_at": _iso(row.created_at),
    }


def _safe_check(check: Any) -> Any:
    if isinstance(check, dict) and "html_url" in check:
        return {**check, "html_url": safe_href(check.get("html_url"))}
    return check


def _ci_snapshot_json(row: CiSnapshot) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "head_sha": row.head_sha,
        "overall": row.overall,
        "checks": [_safe_check(check) for check in row.checks],
        "required_checks": list(row.required_checks),
        "missing_required": list(row.missing_required),
        "summary": row.summary,
        "observed_at": _iso(row.observed_at),
    }


def remediation_actions(case: Case) -> dict[str, bool]:
    """Which authenticated operator actions the current state/evidence permits."""
    state = CaseState(case.state)
    if phase_for_state(state) is not REMEDIATION_PHASE:
        return {"cancel": False, "retry": False, "retry_ci": False, "retry_probe": False}
    attempt = latest_attempt(case, AttemptKind.REMEDIATION)
    active = attempt is not None and attempt.status in ACTIVE_ATTEMPT_STATUSES
    verified = attempt is not None and attempt.head_sha is not None and attempt.pr_url is not None
    stage = attempt.failure_stage if attempt is not None else None
    infra = attempt is not None and attempt.failure_class == FAILURE_CLASS_INFRASTRUCTURE
    return {
        "cancel": state not in TERMINAL_STATES,
        "retry": state in REMEDIATION_RETRYABLE_STATES and not active,
        "retry_ci": (
            verified
            and not active
            and stage == "ci"
            and state in {CaseState.CI_FAILED, CaseState.REMEDIATION_FAILED}
        ),
        "retry_probe": (state == CaseState.PROBE_INFRASTRUCTURE_BLOCKED and not active)
        or (
            verified
            and not active
            and infra
            and stage == "probe_head"
            and state == CaseState.REMEDIATION_FAILED
        ),
    }


def _probe_snapshot_json(snapshot: ProbeSnapshot) -> dict[str, Any]:
    return {
        "id": str(snapshot.id),
        "probe_identifier": snapshot.probe_identifier,
        "base_sha": snapshot.base_sha,
        "manifest_path": snapshot.manifest_path,
        "script_path": snapshot.script_path,
        "manifest_hash": snapshot.manifest_hash,
        "script_hash": snapshot.script_hash,
        "registry_commit": snapshot.registry_commit,
        "expected_base_exit_code": snapshot.expected_base_exit_code,
        "expected_head_exit_code": snapshot.expected_head_exit_code,
        "timeout_seconds": snapshot.timeout_seconds,
        "runtime": snapshot.runtime,
        "created_at": _iso(snapshot.created_at),
        # BASE runs recorded before any Devin attempt existed (the dispatch gate).
        "pre_session_executions": [
            _probe_execution_json(r) for r in snapshot.executions if r.attempt_id is None
        ],
    }


def remediation_json(case: Case) -> dict[str, Any] | None:
    """Phase 4 evidence for the operator API/dashboard: every attempt with its probe
    snapshot, PR corroboration, probe executions and CI observations. `case` must have
    been loaded with `ATTEMPT_EVIDENCE_OPTIONS`."""
    attempts = [a for a in case.attempts if a.kind == AttemptKind.REMEDIATION]
    if (
        not attempts
        and not case.probe_snapshots
        and CaseState(case.state) not in REMEDIATION_PHASE_STATES
    ):
        return None
    floor = datetime.min.replace(tzinfo=UTC)
    attempts.sort(key=lambda a: a.started_at or floor)
    rows: list[dict[str, Any]] = []
    for attempt in attempts:
        snapshot = attempt.probe_snapshot
        start = attempt.create_sent_at or attempt.started_at
        end = attempt.finished_at or datetime.now(UTC)
        rows.append(
            {
                "id": str(attempt.id),
                "status": attempt.status,
                "operation_key": attempt.operation_key,
                "create_state": attempt.create_state,
                "devin_session_id": attempt.devin_session_id,
                "devin_session_url": safe_href(attempt.devin_session_url),
                "devin_status": attempt.devin_status,
                "devin_acus_consumed": attempt.devin_acus_consumed,
                "max_acu_limit": attempt.max_acu_limit,
                "acu_report_status": attempt.acu_report_status,
                "acu_reported": attempt.acu_reported,
                "acu_display": (
                    f"{attempt.acu_reported:g} ACU"
                    f"{' (simulated)' if attempt.acu_report_status == 'simulated' else ''}"
                    if attempt.acu_reported is not None
                    and attempt.acu_report_status in ("available", "simulated")
                    else "Unavailable"
                ),
                "base_sha": attempt.base_sha,
                "triage_result_hash": attempt.triage_result_hash,
                "approval_request_id": (
                    str(attempt.approval_request_id) if attempt.approval_request_id else None
                ),
                "started_at": _iso(attempt.started_at),
                "finished_at": _iso(attempt.finished_at),
                "duration_seconds": int((end - start).total_seconds()) if start else None,
                "devin_pull_requests": [
                    {**pr, "pr_url": safe_href(pr.get("pr_url"))}
                    for pr in (attempt.devin_pull_requests or [])
                    if isinstance(pr, dict)
                ],
                "structured_output": attempt.structured_output,
                "pr_url": attempt.pr_url,
                "pr_number": attempt.pr_number,
                "branch": attempt.branch,
                "head_sha": attempt.head_sha,
                "ci_deadline_at": _iso(attempt.ci_deadline_at),
                "failure_stage": attempt.failure_stage,
                "failure_class": attempt.failure_class,
                "error": attempt.error,
                "probe_snapshot": None if snapshot is None else _probe_snapshot_json(snapshot),
                "pull_request_evidence": [
                    _pr_evidence_json(row) for row in attempt.pull_request_evidence
                ],
                "probe_executions": [_probe_execution_json(r) for r in attempt.probe_executions],
                "ci_snapshots": [_ci_snapshot_json(row) for row in attempt.ci_snapshots],
            }
        )
    return {
        "state": case.state,
        "ci_status": case.ci_status,
        "failure_reason": case.failure_reason,
        "ready_for_human_review": CaseState(case.state) == CaseState.CI_PASSED,
        "actions": remediation_actions(case),
        "probe_snapshots": [_probe_snapshot_json(s) for s in case.probe_snapshots],
        "attempts": rows,
    }


TEMPLATE_HELPERS: dict[str, object] = {
    "latest_approval": latest_approval,
    "humanize": _humanize,
    "until": _until,
    "attempt_elapsed": _attempt_elapsed,
    "latest_attempt": latest_attempt,
    "latest_triage_result": latest_triage_result,
    "remediation_actions": remediation_actions,
    "remediation_json": remediation_json,
    "AttemptKind": AttemptKind,
}


ATTEMPT_EVIDENCE_OPTIONS = (
    selectinload(Case.probe_snapshots).selectinload(ProbeSnapshot.executions),
    selectinload(Case.attempts).selectinload(Attempt.probe_snapshot),
    selectinload(Case.attempts).selectinload(Attempt.probe_executions),
    selectinload(Case.attempts).selectinload(Attempt.pull_request_evidence),
    selectinload(Case.attempts).selectinload(Attempt.ci_snapshots),
)


async def load_case(session: AsyncSession, case_id: UUID) -> Case | None:
    return cast(
        Case | None,
        await session.scalar(
            select(Case)
            .options(
                *ATTEMPT_EVIDENCE_OPTIONS,
                selectinload(Case.transitions),
                selectinload(Case.outbox),
                selectinload(Case.approval_requests).selectinload(ApprovalRequest.events),
                selectinload(Case.approval_requests).selectinload(ApprovalRequest.actions),
            )
            .where(Case.id == case_id)
        ),
    )


async def _context(session: AsyncSession) -> dict[str, object]:
    cases = list(
        (
            await session.scalars(
                select(Case)
                .options(selectinload(Case.attempts))
                .order_by(Case.created_at.desc())
                .limit(100)
            )
        ).all()
    )
    events = list(
        (
            await session.scalars(
                select(WebhookEvent).order_by(WebhookEvent.received_at.desc()).limit(100)
            )
        ).all()
    )
    transitions = list(
        (
            await session.scalars(
                select(StateTransition).order_by(StateTransition.seq.desc()).limit(50)
            )
        ).all()
    )
    state_counts = {state.value: 0 for state in CaseState}
    for state, count in (
        await session.execute(select(Case.state, func.count()).group_by(Case.state))
    ).all():
        state_counts[CaseState(state).value] = count
    recommendation_counts = {
        str(recommendation): count
        for recommendation, count in (
            await session.execute(
                select(Case.recommendation, func.count())
                .where(Case.recommendation.is_not(None))
                .group_by(Case.recommendation)
            )
        ).all()
    }
    active = [
        case
        for case in cases
        if case.state not in TERMINAL_STATES
        and case.state
        not in {
            CaseState.HUMAN_BLOCKED,
            CaseState.REMEDIATION_HUMAN_BLOCKED,
            CaseState.PROBE_INFRASTRUCTURE_BLOCKED,
        }
    ]
    completed = [case for case in cases if case.state in TERMINAL_STATES]
    processing_events = [event for event in events if event.status == EventStatus.PROCESSING]
    failures: list[object] = [
        case
        for case in cases
        if case.state
        in {
            CaseState.FAILED,
            CaseState.HUMAN_BLOCKED,
            CaseState.TIMED_OUT,
            CaseState.REMEDIATION_FAILED,
            CaseState.REMEDIATION_HUMAN_BLOCKED,
            CaseState.PROBE_INFRASTRUCTURE_BLOCKED,
            CaseState.REMEDIATION_TIMED_OUT,
            CaseState.CI_FAILED,
        }
    ]
    failures.extend(event for event in events if event.status == EventStatus.FAILED)
    now = datetime.now(UTC)
    received_1h = await session.scalar(
        select(func.count()).select_from(Case).where(Case.created_at >= now - timedelta(hours=1))
    )
    received_24h = await session.scalar(
        select(func.count()).select_from(Case).where(Case.created_at >= now - timedelta(hours=24))
    )
    accepted_1h = await session.scalar(
        select(func.count())
        .select_from(WebhookEvent)
        .where(WebhookEvent.received_at >= now - timedelta(hours=1))
    )
    accepted_24h = await session.scalar(
        select(func.count())
        .select_from(WebhookEvent)
        .where(WebhookEvent.received_at >= now - timedelta(hours=24))
    )
    bucket_rows = (
        await session.execute(
            select(
                Case.state,
                func.count(),
                func.avg(func.extract("epoch", Case.completed_at - Case.created_at)),
            )
            .where(
                Case.state.in_(
                    {
                        CaseState.CI_PASSED,
                        CaseState.POLICY_REJECTED,
                        CaseState.FAILED,
                        CaseState.TIMED_OUT,
                        CaseState.CANCELLED,
                    }
                ),
                Case.completed_at.is_not(None),
            )
            .group_by(Case.state)
        )
    ).all()
    buckets = {
        "succeeded": {"count": 0, "mean_time": "-"},
        "rejected": {"count": 0, "mean_time": "-"},
        "failed": {"count": 0, "mean_time": "-"},
    }
    for state, count, mean in bucket_rows:
        bucket = (
            "succeeded"
            if state == CaseState.CI_PASSED
            else ("rejected" if state == CaseState.POLICY_REJECTED else "failed")
        )
        buckets[bucket] = {
            "count": count,
            "mean_time": f"{float(mean):.1f}s" if mean is not None else "-",
        }
    return {
        "cases": cases,
        "events": events,
        "transitions": transitions,
        "state_counts": state_counts,
        "recommendation_counts": recommendation_counts,
        "active": active,
        "processing_events": processing_events,
        "completed": completed,
        "failures": failures,
        "throughput": {
            "received_1h": received_1h,
            "received_24h": received_24h,
            "accepted_1h": accepted_1h,
            "accepted_24h": accepted_24h,
            "buckets": buckets,
        },
        **TEMPLATE_HELPERS,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", {"request": request, **await _context(session)}
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"request": request})


@router.get("/cases/{case_id}", response_class=HTMLResponse)
async def case_detail(
    case_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    case = await load_case(session, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return templates.TemplateResponse(
        request, "case.html", {"request": request, "case": case, **TEMPLATE_HELPERS}
    )


@router.get("/partials/case/{case_id}", response_class=HTMLResponse)
async def case_detail_partial(
    case_id: UUID,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    case = await load_case(session, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    return templates.TemplateResponse(
        request,
        "partials/case_detail.html",
        {"request": request, "case": case, **TEMPLATE_HELPERS, "error": None},
    )


@router.get("/partials/{name}", response_class=HTMLResponse)
async def partial(
    name: str,
    request: Request,
    _: str = Depends(require_operator),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    context = await _context(session)
    if name not in {
        "active",
        "completed",
        "failures",
        "timeline",
        "overview",
        "throughput",
        "cases",
    }:
        raise HTTPException(status_code=404, detail="partial not found")
    return templates.TemplateResponse(
        request, f"partials/{name}.html", {"request": request, **context}
    )
