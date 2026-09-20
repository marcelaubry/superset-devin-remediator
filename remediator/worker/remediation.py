"""Phase 4: approved remediation → bounded Devin session → independent verification → CI.

Every stage reads only persisted evidence (attempt rows, probe snapshot, GitHub responses,
probe executions) and writes its verdict back before transitioning. Devin structured output
and Devin's `pull_requests[]` are treated as *claims* that GitHub and the immutable probe
must corroborate; any contradiction ends the attempt without spending anything further.

Nothing here merges, closes issues, or asks Devin to fix a failed head probe.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..approvals import enqueue, is_current_request, open_request_for_case, triage_result_hash
from ..capacity import (
    CapacityDenied,
    CapacityManager,
    clear_waiting,
    mark_waiting,
    validate_resource_key,
)
from ..config import Settings
from ..devin.client import DevinClient
from ..devin.remediation import (
    RemediationValidationError,
    validate_remediation_output,
)
from ..devin.triage import TriageValidationError, validate_triage_output
from ..github.client import (
    CheckRun,
    GitHubApiError,
    GitHubIssuesClient,
    PullRequestNotFound,
    PullRequestSnapshot,
)
from ..github_refs import BaseCommitResolver
from ..lifecycle import (
    REMEDIATION_SESSION_STATES,
    CaseState,
    transition,
)
from ..models import (
    ACTIVE_ATTEMPT_STATUSES,
    CANCEL_TERMINATION_REASON,
    FAILURE_CLASS_INFRASTRUCTURE,
    FAILURE_CLASS_POLICY,
    FAILURE_CLASS_SESSION,
    FAILURE_CLASS_VERIFICATION,
    OUTBOX_KIND_SLACK_REMEDIATION_UPDATE,
    ApprovalDecision,
    ApprovalRequest,
    Attempt,
    AttemptKind,
    AttemptStatus,
    CapacityLeaseKind,
    Case,
    CiSnapshot,
    DeliveryStatus,
    NotificationOutbox,
    OutboxChannel,
    ProbeExecution,
    ProbeSnapshot,
    ProbeTarget,
    ProbeVerdict,
    PullRequestEvidence,
)
from ..probes.registry import (
    ProbeRegistryError,
    load_approved_probe,
    manifest_cache_inputs,
    manifest_setup_steps,
    manifest_setup_timeout,
)
from ..probes.remote import VerifierBusyError
from ..probes.runner import ProbeRunner, ProbeRunResult, ProbeRunSpec
from ..slack.blocks import RemediationProgress, remediation_headline
from .devin_runner import (
    Clock,
    DevinRunner,
    RemediationContext,
    RunResult,
    Sleep,
    fail_case,
    terminate_running_attempts,
)

logger = logging.getLogger(__name__)

# Post-PR stages; each needs the remediation attempt that produced the PR.
PIPELINE_STATES = frozenset(
    {
        CaseState.OUTPUT_VALIDATING,
        CaseState.PR_DISCOVERED,
        CaseState.PR_VALIDATING,
        CaseState.PROBE_VALIDATING_HEAD,
        CaseState.PR_VALIDATED,
        CaseState.CI_PENDING,
    }
)
# Pre-session stages: zero ACUs are spent here, the BASE probe must reproduce the defect
# before a create intent may exist.
PRE_SESSION_STATES = frozenset({CaseState.REMEDIATION_APPROVED, CaseState.PROBE_VALIDATING_BASE})
REMEDIATION_WORK_STATES = PRE_SESSION_STATES | REMEDIATION_SESSION_STATES | PIPELINE_STATES
_MILESTONE_STATES = frozenset(
    {
        CaseState.REMEDIATION_CREATE_INTENT,
        CaseState.REMEDIATING,
        CaseState.REMEDIATION_RECONCILING_CREATE,
        CaseState.PR_DISCOVERED,
        CaseState.PROBE_VALIDATING_HEAD,
        CaseState.PR_VALIDATED,
        CaseState.CI_PENDING,
    }
)

# Paths a remediation PR may never touch: the immutable probe registry, CI workflows and
# repository settings. Matched as path prefixes against GitHub's reported file list.
FORBIDDEN_PATH_PREFIXES: tuple[str, ...] = (".github/",)
FORBIDDEN_PATHS: frozenset[str] = frozenset(
    {"CODEOWNERS", "SECURITY.md", ".pre-commit-config.yaml", "setup.cfg", "pyproject.toml"}
)

# CI vocabulary persisted on `ci_snapshots.overall` / `cases.ci_status`.
CI_PENDING = "pending"
CI_ABSENT = "absent"
CI_PASSED = "success"
CI_FAILED = "failure"
CI_TIMED_OUT = "timed_out"

_FAILED_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required", "stale"})
_PASSED_CONCLUSIONS = frozenset({"success"})
_NEUTRAL_CONCLUSIONS = frozenset({"neutral", "skipped"})

_PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)/?$"
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class TransientVerificationError(Exception):
    """GitHub (or another dependency) is unavailable; leave the state untouched and retry."""


class CapacityWait(Exception):
    """A configured concurrency limit is saturated. The case keeps its state, `waiting_for`
    is already committed, nothing was spent; the worker re-claims after the backoff."""

    def __init__(self, denied: CapacityDenied) -> None:
        super().__init__(f"waiting for capacity: {denied.label}")
        self.denied = denied


def manifest_resource_keys(manifest: dict[str, Any]) -> tuple[str, ...]:
    """Optional `resource_keys` declared by the approved probe manifest (lockfiles,
    migrations, shared config). Conflicting remediations queue instead of racing."""
    raw = manifest.get("resource_keys")
    if not isinstance(raw, list):
        return ()
    return tuple(dict.fromkeys(validate_resource_key(str(key)) for key in raw))


def _now() -> datetime:
    return datetime.now(UTC)


def parse_pr_url(url: str) -> tuple[str, int] | None:
    match = _PR_URL_RE.match(url.strip())
    if match is None:
        return None
    return match.group("repo").lower(), int(match.group("number"))


def issue_reference_in_body(body: str, repository: str, issue_number: int) -> bool:
    """Exact `owner/repo#N` (case-insensitive) or a closing keyword with `#N`; `#10` never
    matches `#1`."""
    escaped_repo = re.escape(repository)
    full = re.compile(rf"(?<![A-Za-z0-9_./-]){escaped_repo}#{issue_number}(?![0-9])", re.I)
    if full.search(body):
        return True
    keyword = re.compile(
        rf"\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s*:?\s*#"
        rf"{issue_number}(?![0-9])",
        re.I,
    )
    return keyword.search(body) is not None


def evaluate_checks(
    checks: tuple[CheckRun, ...], required: tuple[str, ...]
) -> tuple[str, list[str], str]:
    """Overall CI state for one head SHA.

    Returns (overall, missing_required, summary). `required` empty means "every check GitHub
    reports must pass" — but absent checks are still *absent*, never a pass.
    """
    if not checks:
        missing = list(required)
        return CI_ABSENT, missing, "no check runs reported for this commit"
    by_name: dict[str, CheckRun] = {}
    for observed in checks:
        by_name.setdefault(observed.name, observed)
    required_names = list(required) if required else sorted(by_name)
    missing = [name for name in required_names if name not in by_name]
    failed: list[str] = []
    pending: list[str] = []
    passed: list[str] = []
    for name in required_names:
        run = by_name.get(name)
        if run is None:
            continue
        elif run.status != "completed":
            pending.append(name)
        elif run.conclusion in _PASSED_CONCLUSIONS:
            passed.append(name)
        elif run.conclusion in _NEUTRAL_CONCLUSIONS and not required:
            passed.append(name)
        elif run.conclusion in _NEUTRAL_CONCLUSIONS:
            failed.append(f"{name} ({run.conclusion}; required)")
        else:
            failed.append(f"{name} ({run.conclusion})")
    if failed:
        return CI_FAILED, missing, "failed: " + ", ".join(failed)
    if pending or missing:
        parts = []
        if pending:
            parts.append("pending: " + ", ".join(pending))
        if missing:
            parts.append("missing required: " + ", ".join(missing))
        return CI_PENDING, missing, "; ".join(parts)
    return CI_PASSED, [], f"passed: {', '.join(passed)}"


def enqueue_remediation_update(session: AsyncSession, request: ApprovalRequest) -> None:
    """Slack progress update through the outbox; failure there never touches case truth."""
    if request.slack_message_ts is None:
        return
    enqueue(
        session,
        request,
        OutboxChannel.SLACK,
        OUTBOX_KIND_SLACK_REMEDIATION_UPDATE,
        {"approval_request_id": str(request.id)},
    )


async def latest_remediation_attempt(session: AsyncSession, case_id: Any) -> Attempt | None:
    attempt: Attempt | None = await session.scalar(
        select(Attempt)
        .where(Attempt.case_id == case_id, Attempt.kind == AttemptKind.REMEDIATION)
        .order_by(Attempt.started_at.desc(), Attempt.id.desc())
        .limit(1)
    )
    return attempt


async def remediation_progress(session: AsyncSession, case: Case) -> RemediationProgress:
    """Slack/dashboard evidence built purely from persisted rows (never from Devin prose)."""
    attempt = await latest_remediation_attempt(session, case.id)
    state = str(case.state)
    progress = RemediationProgress(case_state=state, headline=remediation_headline(state))
    probe_base: str | None = None
    probe_head: str | None = None
    snapshot = await latest_probe_snapshot(session, case.id)
    if snapshot is not None:
        # BASE runs before any attempt exists; HEAD runs belong to the attempt whose PR
        # they verify. Both hang off the same immutable snapshot.
        clause = (
            ProbeExecution.attempt_id == attempt.id
            if attempt is not None and attempt.probe_snapshot_id == snapshot.id
            else ProbeExecution.attempt_id.is_(None)
        )
        runs = (
            await session.scalars(
                select(ProbeExecution)
                .where(ProbeExecution.probe_snapshot_id == snapshot.id, clause)
                .order_by(ProbeExecution.started_at)
            )
        ).all()
        for run in runs:
            text = (
                f"{run.verdict.value.lower()} (exit {run.exit_code}, "
                f"expected {run.expected_exit_code})"
            )
            if run.target == ProbeTarget.BASE:
                probe_base = text
            else:
                probe_head = text
    if attempt is None:
        blocked = CaseState(case.state) in {
            CaseState.REMEDIATION_HUMAN_BLOCKED,
            CaseState.PROBE_INFRASTRUCTURE_BLOCKED,
            CaseState.REMEDIATION_FAILED,
        }
        return RemediationProgress(
            case_state=state,
            headline=progress.headline,
            probe_base=probe_base,
            failure_reason=case.failure_reason if blocked else None,
        )
    ci: CiSnapshot | None = await session.scalar(
        select(CiSnapshot)
        .where(CiSnapshot.attempt_id == attempt.id)
        .order_by(CiSnapshot.observed_at.desc(), CiSnapshot.id.desc())
        .limit(1)
    )
    failure = None
    if attempt.status in {AttemptStatus.FAILED, AttemptStatus.TIMED_OUT, AttemptStatus.CANCELLED}:
        failure = attempt.error or case.failure_reason
    elif CaseState(case.state) in {
        CaseState.REMEDIATION_HUMAN_BLOCKED,
        CaseState.PROBE_INFRASTRUCTURE_BLOCKED,
    }:
        failure = case.failure_reason or attempt.error
    return RemediationProgress(
        case_state=state,
        headline=progress.headline,
        devin_session_url=attempt.devin_session_url,
        pr_url=attempt.pr_url,
        pr_number=attempt.pr_number,
        head_sha=attempt.head_sha,
        probe_base=probe_base,
        probe_head=probe_head,
        ci_summary=f"{ci.overall}: {ci.summary}" if ci is not None else None,
        failure_reason=failure,
        ready_for_review=CaseState(case.state) == CaseState.CI_PASSED,
    )


async def latest_probe_snapshot(session: AsyncSession, case_id: Any) -> ProbeSnapshot | None:
    snapshot: ProbeSnapshot | None = await session.scalar(
        select(ProbeSnapshot)
        .where(ProbeSnapshot.case_id == case_id)
        .order_by(ProbeSnapshot.created_at.desc(), ProbeSnapshot.id.desc())
        .limit(1)
    )
    return snapshot


class RemediationPipeline:
    def __init__(
        self,
        session: AsyncSession,
        case: Case,
        devin: DevinClient,
        github: GitHubIssuesClient,
        probes: ProbeRunner,
        settings: Settings,
        resolver: BaseCommitResolver,
        *,
        claimed_by: str | None = None,
        clock: Clock = _now,
        sleep: Sleep | None = None,
        capacity: CapacityManager | None = None,
    ) -> None:
        self.session = session
        self.case = case
        self.devin = devin
        self.github = github
        self.probes = probes
        self.settings = settings
        self.resolver = resolver
        self.claimed_by = claimed_by
        self.clock = clock
        self.sleep = sleep
        self.capacity = capacity
        self._request: ApprovalRequest | None = None

    # -- driver --------------------------------------------------------------------

    async def process(self) -> None:
        case = self.case
        entered = CaseState(case.state)
        try:
            if CaseState(case.state) == CaseState.REMEDIATION_APPROVED:
                await self._dispatch()
            if CaseState(case.state) == CaseState.PROBE_VALIDATING_BASE:
                await self._probe_base()
                await self._milestone(entered)
            if CaseState(case.state) in REMEDIATION_SESSION_STATES:
                await self._run_session()
                await self._milestone(entered)
            steps: dict[CaseState, Callable[[Attempt], Awaitable[None]]] = {
                CaseState.OUTPUT_VALIDATING: self._validate_output,
                CaseState.PR_DISCOVERED: self._pr_discovered,
                CaseState.PR_VALIDATING: self._validate_pull_request,
                CaseState.PROBE_VALIDATING_HEAD: self._probe_head,
                CaseState.PR_VALIDATED: self._enter_ci,
                CaseState.CI_PENDING: self._poll_ci,
            }
            while CaseState(case.state) in PIPELINE_STATES:
                attempt = await latest_remediation_attempt(self.session, case.id)
                if attempt is None:
                    await self._fail("no remediation attempt recorded for this case", "pipeline")
                    break
                before = CaseState(case.state)
                await steps[before](attempt)
                await self.session.commit()
                if CaseState(case.state) == before:
                    break  # waiting on an external system (CI); the worker re-claims later
                await self._milestone(before)
        except TransientVerificationError as exc:
            logger.warning("case %s: %s; will retry", case.id, exc)
            await self.session.rollback()
        except CapacityWait as exc:
            logger.info("case %s: %s", case.id, exc)
            await self.session.commit()
        except BaseException:
            # The session is unusable after a failed flush/commit (PendingRollbackError on
            # the next statement); roll it back and let the worker classify the error
            # instead of masking it with a follow-up notification failure.
            await self.session.rollback()
            raise
        else:
            if CaseState(case.state) != entered or CaseState(case.state) == CaseState.CI_PENDING:
                await self._notify()
                await self.session.commit()

    async def _notify(self) -> None:
        request = await open_request_for_case(self.session, self.case.id)
        if request is not None:
            enqueue_remediation_update(self.session, request)

    async def _milestone(self, before: CaseState) -> None:
        """Slack progress for states a human wants to see as they happen (session running, PR
        discovered, each probe verdict, CI entered). Terminal/blocked states are announced
        by `process()` once the claim ends."""
        now = CaseState(self.case.state)
        if now != before and now in _MILESTONE_STATES:
            await self._notify()
            await self.session.commit()

    async def _transition(self, to_state: CaseState, reason: str) -> None:
        await transition(
            self.session,
            self.case,
            to_state,
            reason,
            "worker",
            expected_claimed_by=self.claimed_by,
        )

    async def _fail(
        self,
        reason: str,
        stage: str,
        *,
        failure_class: str = FAILURE_CLASS_VERIFICATION,
        attempt: Attempt | None = None,
    ) -> None:
        if attempt is not None:
            attempt.failure_stage = stage
            attempt.failure_class = failure_class
            attempt.error = reason
        logger.info("case %s remediation failed at %s: %s", self.case.id, stage, reason)
        await fail_case(self.session, self.case, reason, "worker", self.claimed_by)
        await self.session.commit()

    async def _human_block(self, reason: str, stage: str, attempt: Attempt | None = None) -> None:
        if attempt is not None:
            attempt.failure_stage = stage
            attempt.failure_class = FAILURE_CLASS_POLICY
            attempt.error = reason
        self.case.failure_reason = reason
        await self._transition(CaseState.REMEDIATION_HUMAN_BLOCKED, reason)
        self.session.add(
            NotificationOutbox(
                case_id=self.case.id,
                channel=OutboxChannel.GITHUB,
                kind="remediation_human_blocked",
                payload={"issue_number": self.case.issue_number, "reason": reason},
            )
        )
        await self.session.commit()

    # -- dispatch preconditions (zero ACUs) ------------------------------------------

    async def _approved_request(self) -> ApprovalRequest | str:
        request = await open_request_for_case(self.session, self.case.id, for_update=True)
        if request is None:
            return "no approval request exists for this case"
        if request.decision != ApprovalDecision.APPROVED:
            return f"approval request is {request.decision.value}, not APPROVED"
        if not await is_current_request(self.session, request):
            return "approval belongs to a superseded triage round"
        if (
            request.delivery_status != DeliveryStatus.CONFIRMED
            or request.label_confirmed_at is None
        ):
            return "remediation label has not been confirmed by a signed GitHub webhook"
        return request

    async def _validated_triage(self, request: ApprovalRequest) -> dict[str, Any] | str:
        attempt = await self.session.get(Attempt, request.attempt_id)
        if attempt is None or attempt.structured_output is None:
            return "approved triage attempt has no structured output"
        try:
            result = validate_triage_output(attempt.structured_output)
        except TriageValidationError as exc:
            return f"approved triage output no longer validates: {exc}"
        if result.outcome != "remediation_candidate":
            return f"triage outcome is {result.outcome}, not remediation_candidate"
        if triage_result_hash(result.raw) != request.triage_result_hash:
            return "triage result hash does not match the approved hash"
        return result.raw

    async def _dispatch(self) -> None:
        case = self.case
        settings = self.settings
        if not settings.repository_allowed(case.repository):
            await self._fail(
                f"repository {case.repository} is not allowlisted",
                "dispatch",
                failure_class=FAILURE_CLASS_POLICY,
            )
            return
        request = await self._approved_request()
        if isinstance(request, str):
            await self._fail(request, "dispatch", failure_class=FAILURE_CLASS_POLICY)
            return
        triage = await self._validated_triage(request)
        if isinstance(triage, str):
            await self._fail(triage, "dispatch", failure_class=FAILURE_CLASS_POLICY)
            return
        if not settings.github_base_ref.strip():
            await self._fail(
                "default branch is not configured", "dispatch", failure_class=FAILURE_CLASS_POLICY
            )
            return
        try:
            issue = await self.github.get_issue(case.repository, case.issue_number)
        except GitHubApiError as exc:
            if exc.retryable:
                raise TransientVerificationError(f"GitHub issue lookup failed: {exc}") from exc
            await self._fail(
                f"GitHub issue lookup failed: {exc}",
                "dispatch",
                failure_class=FAILURE_CLASS_INFRASTRUCTURE,
            )
            return
        label = settings.github_remediation_label
        if label not in issue.labels:
            await self._fail(
                f"label {label} is not present on {case.repository}#{case.issue_number}",
                "dispatch",
                failure_class=FAILURE_CLASS_POLICY,
            )
            return
        if issue.state != "open":
            await self._human_block(f"issue is {issue.state}; remediation not started", "dispatch")
            return
        active = [
            a
            for a in await self.session.scalars(
                select(Attempt).where(
                    Attempt.case_id == case.id,
                    Attempt.kind == AttemptKind.REMEDIATION,
                    Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                )
            )
        ]
        if active:
            await self._human_block(
                f"remediation attempt {active[0].operation_key} is still active", "dispatch"
            )
            return
        valid_pr = await self.session.scalar(
            select(PullRequestEvidence.pr_url).where(
                PullRequestEvidence.case_id == case.id, PullRequestEvidence.valid.is_(True)
            )
        )
        if valid_pr is not None:
            await self._human_block(
                f"a verified PR already exists for this case: {valid_pr}", "dispatch"
            )
            return
        try:
            probe = await load_approved_probe(
                settings.probe_root_path, case.repository, case.issue_number
            )
        except ProbeRegistryError as exc:
            await self._human_block(f"approved probe unavailable: {exc}", "dispatch")
            return
        snapshot = ProbeSnapshot(
            case_id=case.id,
            repository=probe.repository,
            issue_number=probe.issue_number,
            probe_identifier=probe.identifier,
            base_sha=probe.base_sha,
            manifest_path=probe.manifest_path,
            script_path=probe.script_path,
            manifest=probe.manifest,
            manifest_hash=probe.manifest_hash,
            script_hash=probe.script_hash,
            script_content=probe.script_content,
            registry_commit=probe.registry_commit,
            expected_base_exit_code=probe.expected_base_exit_code,
            expected_head_exit_code=probe.expected_head_exit_code,
            timeout_seconds=probe.timeout_seconds,
            runtime=probe.runtime,
        )
        self.session.add(snapshot)
        await self._transition(
            CaseState.PROBE_VALIDATING_BASE,
            f"dispatch preconditions satisfied; probe {probe.identifier} "
            f"({probe.script_hash[:12]}) snapshotted; reproducing at base {probe.base_sha[:12]}",
        )
        await self.session.commit()

    async def _matched_base_execution(self, snapshot: ProbeSnapshot) -> ProbeExecution | None:
        """The persisted BASE run that authorises spending on this snapshot, if any."""
        execution: ProbeExecution | None = await self.session.scalar(
            select(ProbeExecution)
            .where(
                ProbeExecution.probe_snapshot_id == snapshot.id,
                ProbeExecution.target == ProbeTarget.BASE,
                ProbeExecution.verdict == ProbeVerdict.MATCHED,
                ProbeExecution.commit_sha == snapshot.base_sha,
                ProbeExecution.script_hash == snapshot.script_hash,
                ProbeExecution.attempt_id.is_(None),
            )
            .order_by(ProbeExecution.started_at.desc(), ProbeExecution.id.desc())
            .limit(1)
        )
        return execution

    async def _load_context(self) -> RemediationContext | str:
        request = await self._approved_request()
        if isinstance(request, str):
            return request
        triage = await self._validated_triage(request)
        if isinstance(triage, str):
            return triage
        probe = await latest_probe_snapshot(self.session, self.case.id)
        if probe is None:
            return "no probe snapshot was persisted at dispatch"
        base_execution = await self._matched_base_execution(probe)
        if base_execution is None:
            return (
                f"no persisted BASE probe execution reproduces the defect at "
                f"{probe.base_sha[:12]} with hash {probe.script_hash[:12]}"
            )
        self._request = request
        return RemediationContext(
            approval=request,
            triage_output=triage,
            probe=probe,
            base_execution=base_execution,
            base_ref=self.settings.github_base_ref,
        )

    # -- Devin session ---------------------------------------------------------------

    async def _run_session(self) -> None:
        case = self.case
        context = await self._load_context()
        if isinstance(context, str):
            # Without an approved dispatch context no new session may be created, but an
            # already-persisted active attempt may own a paid session: it must still be
            # reconciled by exact tag, polled, or terminated rather than abandoned.
            active = await self.session.scalar(
                select(Attempt)
                .where(
                    Attempt.case_id == case.id,
                    Attempt.kind == AttemptKind.REMEDIATION,
                    Attempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                )
                .limit(1)
            )
            terminating = CaseState(case.state) == CaseState.REMEDIATION_TERMINATION_PENDING
            if active is None and not terminating:
                await self._fail(context, "dispatch", failure_class=FAILURE_CLASS_POLICY)
                return
            runner_context = None
        else:
            runner_context = context
        runner = DevinRunner(
            self.session,
            case,
            self.devin,
            self.settings,
            self.resolver,
            claimed_by=self.claimed_by,
            clock=self.clock,
            remediation=runner_context,
            capacity=self.capacity,
            **({"sleep": self.sleep} if self.sleep is not None else {}),
        )
        if CaseState(case.state) == CaseState.REMEDIATION_TERMINATION_PENDING:
            pending = await self.session.scalar(
                select(Attempt)
                .where(
                    Attempt.case_id == case.id,
                    Attempt.status == AttemptStatus.TERMINATION_PENDING,
                )
                .limit(1)
            )
            if pending is None:
                if not await terminate_running_attempts(self.session, case, self.devin):
                    case.failure_reason = (
                        "operator requested cancel; Devin session termination pending"
                    )
                    await self.session.commit()
                    return
                if self.capacity is not None:
                    await self.capacity.release_all_for_case(
                        self.session, case.id, "remote termination confirmed"
                    )
                await self._transition(CaseState.REMEDIATION_CANCELLED, CANCEL_TERMINATION_REASON)
                await self.session.commit()
                return
        if runner_context is not None:
            await self._acquire_resource_keys(runner_context.probe)
        outcome = await runner.run(AttemptKind.REMEDIATION)
        if outcome.result not in {RunResult.TERMINATION_PENDING, RunResult.WAITING_FOR_CAPACITY}:
            await self._release_resource_keys(f"remediation {outcome.result.value}")
        if outcome.result != RunResult.FINISHED:
            return
        attempt = outcome.attempt
        attempt.status = AttemptStatus.SUCCEEDED
        if outcome.snapshot is not None:
            attempt.devin_pull_requests = [asdict(pr) for pr in outcome.snapshot.pull_requests]
            attempt.devin_acus_consumed = outcome.snapshot.acus_consumed
        await self._transition(CaseState.OUTPUT_VALIDATING, "Devin session finished")
        await self.session.commit()

    # -- structured output -----------------------------------------------------------

    async def _validate_output(self, attempt: Attempt) -> None:
        case = self.case
        snapshot = await self.session.get(ProbeSnapshot, attempt.probe_snapshot_id)
        if snapshot is None:
            await self._fail("attempt has no probe snapshot", "output", attempt=attempt)
            return
        try:
            result = validate_remediation_output(attempt.structured_output)
        except RemediationValidationError as exc:
            await self._fail(f"structured output invalid: {exc}", "output", attempt=attempt)
            return
        if attempt.base_sha and result.base_sha != attempt.base_sha:
            await self._fail(
                f"structured output base_sha {result.base_sha[:12]} != pinned "
                f"{attempt.base_sha[:12]}",
                "output",
                attempt=attempt,
            )
            return
        if result.probe_hash != snapshot.script_hash:
            await self._fail(
                "structured output names a different probe hash", "output", attempt=attempt
            )
            return
        if result.probe_identifier != snapshot.probe_identifier:
            await self._fail("structured output names a different probe", "output", attempt=attempt)
            return
        parts = result.issue_reference_parts()
        if parts != (case.repository.lower(), case.issue_number):
            await self._fail(
                f"structured output issue_reference {result.issue_reference!r} is not "
                f"{case.repository}#{case.issue_number}",
                "output",
                attempt=attempt,
            )
            return
        claimed = tuple(str(pr.get("pr_url") or "") for pr in (attempt.devin_pull_requests or []))
        if result.outcome == "needs_human":
            questions = "; ".join(result.blocking_questions) or result.summary
            await self._human_block(f"Devin needs a human: {questions}", "output", attempt)
            return
        if result.outcome == "failed":
            await self._fail(
                f"Devin reported failure: {result.summary}",
                "output",
                failure_class=FAILURE_CLASS_SESSION,
                attempt=attempt,
            )
            return
        if result.outcome == "no_change_needed":
            if claimed:
                await self._human_block(
                    "Devin reported no_change_needed but opened a PR: " + ", ".join(claimed),
                    "output",
                    attempt,
                )
                return
            # The session only exists because the persisted BASE run reproduced the defect
            # at this exact SHA and hash; that evidence refutes the claim without spending
            # another probe execution.
            base_run = await self._attempt_base_run(attempt, snapshot)
            exit_code = base_run.exit_code if base_run is not None else None
            await self._fail(
                "Devin reported no_change_needed but the approved probe still fails at base "
                f"{(attempt.base_sha or result.base_sha)[:12]} (persisted BASE exit {exit_code})",
                "output",
                attempt=attempt,
            )
            return
        # pr_created
        pr_url = result.pr_url or ""
        if not claimed:
            await self._fail(
                "structured output claims a PR but Devin's pull_requests[] is empty",
                "output",
                attempt=attempt,
            )
            return
        if len(claimed) != 1:
            await self._human_block(
                f"Devin reports {len(claimed)} pull requests; expected exactly one",
                "output",
                attempt,
            )
            return
        if claimed[0].rstrip("/") != pr_url.rstrip("/"):
            await self._fail(
                f"structured output pr_url {pr_url} contradicts Devin pull_requests[] {claimed[0]}",
                "output",
                attempt=attempt,
            )
            return
        parsed = parse_pr_url(pr_url)
        if parsed is None:
            await self._fail(
                f"pr_url {pr_url!r} is not a github.com pull request URL", "output", attempt=attempt
            )
            return
        repo, number = parsed
        if repo != case.repository.lower():
            await self._fail(
                f"PR {pr_url} is in {repo}, not the allowlisted repository {case.repository}",
                "output",
                failure_class=FAILURE_CLASS_POLICY,
                attempt=attempt,
            )
            return
        attempt.pr_url = pr_url
        attempt.pr_number = number
        attempt.branch = result.branch
        attempt.head_sha = result.head_sha
        case.pr_url = pr_url
        case.pr_number = number
        await self._transition(CaseState.PR_DISCOVERED, f"Devin reported PR #{number}")

    async def _pr_discovered(self, attempt: Attempt) -> None:
        await self._transition(CaseState.PR_VALIDATING, "verifying PR with GitHub")

    # -- GitHub corroboration --------------------------------------------------------

    async def _gh(self, call: Awaitable[Any], what: str) -> Any:
        try:
            return await call
        except PullRequestNotFound:
            raise
        except GitHubApiError as exc:
            if exc.retryable:
                raise TransientVerificationError(f"{what}: {exc}") from exc
            raise

    async def _validate_pull_request(self, attempt: Attempt) -> None:
        case = self.case
        settings = self.settings
        result = validate_remediation_output(attempt.structured_output)
        number = attempt.pr_number
        if number is None or attempt.head_sha is None:
            await self._fail("attempt lost its PR identity", "pr", attempt=attempt)
            return
        try:
            pull: PullRequestSnapshot = await self._gh(
                self.github.get_pull_request(case.repository, number), "get_pull_request"
            )
        except PullRequestNotFound:
            await self._fail(
                f"PR #{number} does not exist in {case.repository}", "pr", attempt=attempt
            )
            return
        except GitHubApiError as exc:
            await self._fail(
                f"GitHub refused PR lookup: {exc}",
                "pr",
                failure_class=FAILURE_CLASS_INFRASTRUCTURE,
                attempt=attempt,
            )
            return
        checks: list[str] = []
        problems: list[str] = []
        human: list[str] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append(f"{'ok' if ok else 'FAIL'} {name}: {detail}")
            if not ok:
                problems.append(f"{name}: {detail}")

        check("repository", pull.repository.lower() == case.repository.lower(), pull.repository)
        check(
            "head_repository",
            pull.head_repository.lower() == case.repository.lower(),
            f"{pull.head_repository} (forks are not accepted)",
        )
        check("url", pull.html_url.rstrip("/") == (attempt.pr_url or "").rstrip("/"), pull.html_url)
        check("not_merged", not pull.merged, "merged" if pull.merged else "not merged")
        check("open", pull.state == "open", f"state={pull.state} draft={pull.draft}")
        check(
            "base_ref",
            pull.base_ref == settings.github_base_ref,
            f"{pull.base_ref} (expected {settings.github_base_ref})",
        )
        prefix = settings.devin_remediation_branch_prefix
        check(
            "head_ref_prefix",
            pull.head_ref.startswith(prefix),
            f"{pull.head_ref} (expected prefix {prefix})",
        )
        check(
            "head_ref_matches_output",
            pull.head_ref == result.branch,
            f"github={pull.head_ref} devin={result.branch}",
        )
        check(
            "head_sha_matches_output",
            pull.head_sha == attempt.head_sha,
            f"github={pull.head_sha[:12]} devin={attempt.head_sha[:12]}",
        )
        check(
            "author",
            pull.author_login.lower() in settings.pr_author_logins,
            f"{pull.author_login} ({pull.author_type})",
        )

        compare = None
        if _SHA_RE.match(attempt.base_sha or ""):
            try:
                compare = await self._gh(
                    self.github.compare_commits(
                        case.repository, attempt.base_sha or "", pull.head_sha
                    ),
                    "compare_commits",
                )
            except GitHubApiError as exc:
                check("ancestry", False, f"compare failed: {exc}")
            if compare is not None:
                if compare.status == "ahead":
                    check("ancestry", True, f"ahead by {compare.ahead_by}")
                elif compare.status == "diverged":
                    checks.append(f"WARN ancestry: diverged (behind {compare.behind_by})")
                    human.append(
                        f"PR head diverged from pinned base {(attempt.base_sha or '')[:12]} "
                        f"(behind by {compare.behind_by}); needs a human rebase decision"
                    )
                else:
                    check("ancestry", False, f"compare status {compare.status}")
        else:
            check("ancestry", False, "pinned base SHA missing")

        closing_source: str | None = None
        closing_numbers: list[int] = []
        try:
            refs = await self._gh(
                self.github.closing_issue_references(case.repository, number),
                "closing_issue_references",
            )
        except GitHubApiError:
            refs = None
        if refs is not None:
            closing_source = "closing_issues_references"
            closing_numbers = list(refs)
            linked = case.issue_number in refs
        else:
            try:
                timeline = await self._gh(
                    self.github.pull_requests_referencing_issue(case.repository, case.issue_number),
                    "pull_requests_referencing_issue",
                )
            except GitHubApiError:
                timeline = None
            if timeline is not None:
                closing_source = "issue_timeline"
                linked = number in timeline
                closing_numbers = [case.issue_number] if linked else []
            else:
                closing_source = "body_regex"
                linked = issue_reference_in_body(pull.body, case.repository, case.issue_number)
                closing_numbers = [case.issue_number] if linked else []
        check("closes_this_issue", linked, f"via {closing_source}")
        if refs is not None and len(refs) > 1:
            human.append(f"PR closes several issues: {', '.join(f'#{n}' for n in refs)}")

        try:
            files = tuple(
                await self._gh(
                    self.github.list_pull_request_files(case.repository, number),
                    "list_pull_request_files",
                )
            )
        except GitHubApiError as exc:
            files = ()
            check("changed_files", False, f"listing failed: {exc}")
        else:
            probe_root = settings.probe_root.strip("/") + "/"
            forbidden = [
                f
                for f in files
                if f.startswith(FORBIDDEN_PATH_PREFIXES)
                or f.startswith(probe_root)
                or f in FORBIDDEN_PATHS
            ]
            check("no_forbidden_paths", not forbidden, ", ".join(forbidden) or "none")
            if len(files) > settings.remediation_max_changed_files:
                human.append(
                    f"PR changes {len(files)} files (> {settings.remediation_max_changed_files});"
                    " scope expansion needs a human"
                )
            undeclared = sorted(set(files) - set(result.changed_files))
            if undeclared and not forbidden:
                human.append(
                    f"PR changes files Devin did not declare: {', '.join(undeclared[:10])}"
                )
            checks.append(
                f"{'ok' if not undeclared else 'WARN'} changed_files_declared: "
                f"{len(files)} files, {len(undeclared)} undeclared"
            )

        valid = not problems and not human
        verdict = "verified" if valid else "; ".join(problems + human)
        self.session.add(
            PullRequestEvidence(
                case_id=case.id,
                attempt_id=attempt.id,
                repository=pull.repository,
                pr_number=pull.number,
                pr_url=pull.html_url,
                state=pull.state,
                draft=pull.draft,
                merged=pull.merged,
                base_ref=pull.base_ref,
                base_sha=pull.base_sha,
                head_ref=pull.head_ref,
                head_sha=pull.head_sha,
                head_repository=pull.head_repository,
                author_login=pull.author_login,
                author_type=pull.author_type,
                compare_status=compare.status if compare else None,
                ahead_by=compare.ahead_by if compare else None,
                behind_by=compare.behind_by if compare else None,
                changed_files=list(files),
                closing_reference_source=closing_source,
                closing_issue_numbers=closing_numbers,
                checks=checks,
                valid=valid,
                verdict=verdict,
            )
        )
        if problems:
            await self._fail(
                f"PR #{number} failed verification: {'; '.join(problems)}", "pr", attempt=attempt
            )
            return
        if human:
            await self._human_block(
                f"PR #{number} needs a human: {'; '.join(human)}", "pr", attempt
            )
            return
        await self._transition(
            CaseState.PROBE_VALIDATING_HEAD,
            f"PR #{number} corroborated by GitHub at {pull.head_sha[:12]}",
        )

    # -- capacity ----------------------------------------------------------------------

    async def _acquire_resource_keys(self, probe: ProbeSnapshot) -> None:
        """Resource keys are mutual-exclusion leases (limit 1) taken *before* the session
        slot so a conflicting job queues without a create intent."""
        if self.capacity is None:
            return
        for key in manifest_resource_keys(probe.manifest):
            outcome = await self.capacity.acquire(
                self.session,
                kind=CapacityLeaseKind.RESOURCE,
                case_id=self.case.id,
                scope=key,
                per_scope_limit=1,
            )
            if isinstance(outcome, CapacityDenied):
                await mark_waiting(self.session, self.case, outcome)
                raise CapacityWait(outcome)
        await clear_waiting(self.session, self.case)
        await self.session.commit()

    async def _release_resource_keys(self, reason: str) -> None:
        if self.capacity is None:
            return
        await self.capacity.release(
            self.session, kind=CapacityLeaseKind.RESOURCE, case_id=self.case.id, reason=reason
        )
        await self.session.commit()

    async def _acquire_probe_slot(self) -> None:
        if self.capacity is None:
            return
        outcome = await self.capacity.acquire(
            self.session, kind=CapacityLeaseKind.PROBE, case_id=self.case.id
        )
        if isinstance(outcome, CapacityDenied):
            await mark_waiting(self.session, self.case, outcome)
            raise CapacityWait(outcome)
        await clear_waiting(self.session, self.case)
        await self.session.commit()

    async def _release_probe_slot(self) -> None:
        if self.capacity is None:
            return
        await self.capacity.release(
            self.session, kind=CapacityLeaseKind.PROBE, case_id=self.case.id, reason="probe done"
        )

    # -- independent probe execution -------------------------------------------------

    async def _execute_probe(
        self,
        attempt: Attempt | None,
        snapshot: ProbeSnapshot,
        target: ProbeTarget,
        commit_sha: str,
    ) -> ProbeExecution:
        expected = (
            snapshot.expected_base_exit_code
            if target == ProbeTarget.BASE
            else snapshot.expected_head_exit_code
        )
        prior = await self.session.scalar(
            select(func.count())
            .select_from(ProbeExecution)
            .where(
                ProbeExecution.case_id == self.case.id,
                ProbeExecution.probe_snapshot_id == snapshot.id,
                ProbeExecution.target == target,
                ProbeExecution.commit_sha == commit_sha,
            )
        )
        # Deterministic per (case, snapshot, target, commit, ordinal): a worker that crashes
        # after the verifier ran but before this row was written re-asks with the same id
        # and gets the stored verdict instead of a second execution.
        request_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"probe:{self.case.id}:{snapshot.id}:{target.value}:{commit_sha}:{prior or 0}",
        ).hex
        spec = ProbeRunSpec(
            repository=snapshot.repository,
            issue_number=snapshot.issue_number,
            commit_sha=commit_sha,
            target=target,
            probe_identifier=snapshot.probe_identifier,
            script_hash=snapshot.script_hash,
            script_content=snapshot.script_content,
            timeout_seconds=min(snapshot.timeout_seconds, self.settings.probe_timeout_seconds),
            max_output_bytes=self.settings.probe_max_output_bytes,
            required_tools=tuple(str(t) for t in (snapshot.runtime.get("tools") or [])),
            setup_steps=manifest_setup_steps(snapshot.manifest),
            setup_timeout_seconds=manifest_setup_timeout(snapshot.manifest),
            cache_inputs=manifest_cache_inputs(snapshot.manifest),
            manifest_hash=snapshot.manifest_hash,
            request_id=request_id,
        )
        await self._acquire_probe_slot()
        started = self.clock()
        try:
            result: ProbeRunResult = await self.probes.run(spec)
        except VerifierBusyError as exc:
            raise TransientVerificationError(str(exc)) from exc
        finally:
            await self._release_probe_slot()
            await self.session.commit()
        if result.infrastructure_failed:
            verdict = ProbeVerdict.INFRASTRUCTURE
        elif result.exit_code == expected and not result.timed_out:
            verdict = ProbeVerdict.MATCHED
        else:
            verdict = ProbeVerdict.MISMATCHED
        execution = ProbeExecution(
            case_id=self.case.id,
            attempt_id=attempt.id if attempt is not None else None,
            probe_snapshot_id=snapshot.id,
            target=target,
            commit_sha=commit_sha,
            script_hash=snapshot.script_hash,
            runner_mode=result.runner_mode,
            command_identity=result.command_identity,
            expected_exit_code=expected,
            exit_code=result.exit_code,
            verdict=verdict,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
            stdout=result.stdout,
            stderr=result.stderr,
            output_truncated=result.output_truncated,
            error=result.infrastructure_error,
            request_id=request_id,
            failure_stage=result.failure_stage,
            tool_versions=dict(result.tool_versions) or None,
            started_at=started,
            finished_at=self.clock(),
        )
        self.session.add(execution)
        await self.session.flush()
        return execution

    async def _snapshot_for(self, attempt: Attempt) -> ProbeSnapshot | None:
        if attempt.probe_snapshot_id is None:
            return None
        return await self.session.get(ProbeSnapshot, attempt.probe_snapshot_id)

    async def _probe_base(self) -> None:
        """Pre-session gate: the immutable probe must reproduce the defect at the pinned base
        SHA before any create intent exists. Every exit here except MATCHED spends zero
        ACUs and never creates an attempt."""
        snapshot = await latest_probe_snapshot(self.session, self.case.id)
        if snapshot is None:
            await self._fail("no probe snapshot was persisted at dispatch", "probe_base")
            return
        base_sha = snapshot.base_sha
        execution = await self._execute_probe(None, snapshot, ProbeTarget.BASE, base_sha)
        await self.session.commit()
        if execution.verdict == ProbeVerdict.INFRASTRUCTURE:
            reason = (
                f"probe infrastructure unavailable at base {base_sha[:12]}: {execution.error}; "
                "no Devin session created"
            )
            self.case.failure_reason = reason
            await self._transition(CaseState.PROBE_INFRASTRUCTURE_BLOCKED, reason)
            await self.session.commit()
            return
        if execution.verdict == ProbeVerdict.MATCHED:
            await self._transition(
                CaseState.REMEDIATION_CREATE_INTENT,
                f"probe {snapshot.script_hash[:12]} reproduces the defect at base "
                f"{base_sha[:12]} (exit {execution.exit_code}); remediation dispatch permitted",
            )
            await self.session.commit()
            return
        if execution.exit_code == snapshot.expected_head_exit_code and not execution.timed_out:
            await self._human_block(
                f"no_change_needed: approved probe already passes at base {base_sha[:12]} "
                f"(exit {execution.exit_code}); no Devin session created",
                "probe_base",
            )
            return
        await self._fail(
            f"probe at base {base_sha[:12]} exited {execution.exit_code}"
            f"{' (timed out)' if execution.timed_out else ''}, expected "
            f"{snapshot.expected_base_exit_code}; no Devin session created",
            "probe_base",
        )

    async def _attempt_base_run(
        self, attempt: Attempt, snapshot: ProbeSnapshot
    ) -> ProbeExecution | None:
        """The pre-session BASE run (same snapshot and hash) that authorised `attempt`."""
        run: ProbeExecution | None = await self.session.scalar(
            select(ProbeExecution)
            .where(
                ProbeExecution.attempt_id == attempt.id,
                ProbeExecution.probe_snapshot_id == snapshot.id,
                ProbeExecution.target == ProbeTarget.BASE,
                ProbeExecution.verdict == ProbeVerdict.MATCHED,
                ProbeExecution.script_hash == snapshot.script_hash,
            )
            .limit(1)
        )
        return run

    async def _probe_head(self, attempt: Attempt) -> None:
        """Run the *same* snapshot (same script content and hash) that reproduced the defect
        at base, now against the GitHub-corroborated head SHA."""
        snapshot = await self._snapshot_for(attempt)
        if snapshot is None or not attempt.head_sha:
            await self._fail(
                "attempt has no probe snapshot or head SHA", "probe_head", attempt=attempt
            )
            return
        base_run = await self._attempt_base_run(attempt, snapshot)
        if base_run is None or base_run.commit_sha != attempt.base_sha:
            await self._fail(
                "no persisted BASE probe execution authorises this attempt",
                "probe_head",
                attempt=attempt,
            )
            return
        execution = await self._execute_probe(attempt, snapshot, ProbeTarget.HEAD, attempt.head_sha)
        if execution.verdict == ProbeVerdict.INFRASTRUCTURE:
            await self._fail(
                f"probe infrastructure failure at head: {execution.error}",
                "probe_head",
                failure_class=FAILURE_CLASS_INFRASTRUCTURE,
                attempt=attempt,
            )
            return
        if execution.verdict == ProbeVerdict.MATCHED:
            await self._transition(
                CaseState.PR_VALIDATED,
                f"probe passes at head {attempt.head_sha[:12]} (exit {execution.exit_code}); "
                "PR independently verified",
            )
            return
        await self._fail(
            f"probe at head {attempt.head_sha[:12]} exited {execution.exit_code}"
            f"{' (timed out)' if execution.timed_out else ''}, expected "
            f"{snapshot.expected_head_exit_code}; not asking Devin to fix it",
            "probe_head",
            attempt=attempt,
        )

    # -- CI tracking -----------------------------------------------------------------

    async def _enter_ci(self, attempt: Attempt) -> None:
        attempt.ci_deadline_at = self.clock() + timedelta(seconds=self.settings.ci_timeout_seconds)
        self.case.ci_status = CI_PENDING
        await self._transition(CaseState.CI_PENDING, "tracking GitHub checks for verified head")

    async def _poll_ci(self, attempt: Attempt) -> None:
        case = self.case
        if attempt.head_sha is None:
            await self._fail("attempt has no verified head SHA", "ci", attempt=attempt)
            return
        now = self.clock()
        if attempt.ci_deadline_at is None:
            attempt.ci_deadline_at = now + timedelta(seconds=self.settings.ci_timeout_seconds)
        try:
            runs = await self._gh(
                self.github.list_check_runs(case.repository, attempt.head_sha), "list_check_runs"
            )
        except GitHubApiError as exc:
            await self._fail(
                f"GitHub refused check listing: {exc}",
                "ci",
                failure_class=FAILURE_CLASS_INFRASTRUCTURE,
                attempt=attempt,
            )
            return
        required = self.settings.required_checks
        overall, missing, summary = evaluate_checks(runs, required)
        timed_out = overall in {CI_PENDING, CI_ABSENT} and now >= attempt.ci_deadline_at
        if timed_out:
            overall = CI_TIMED_OUT
            summary = f"CI did not complete before {attempt.ci_deadline_at.isoformat()}; {summary}"
        self.session.add(
            CiSnapshot(
                case_id=case.id,
                attempt_id=attempt.id,
                head_sha=attempt.head_sha,
                overall=overall,
                checks=[asdict(run) for run in runs],
                required_checks=list(required) if required else sorted({r.name for r in runs}),
                missing_required=missing,
                summary=summary,
                observed_at=now,
            )
        )
        case.ci_status = overall
        if overall == CI_PASSED:
            await self._transition(
                CaseState.CI_PASSED,
                f"required checks passed for {attempt.head_sha[:12]}; human PR review required",
            )
            self.session.add(
                NotificationOutbox(
                    case_id=case.id,
                    channel=OutboxChannel.GITHUB,
                    kind="remediation_ready_for_review",
                    payload={"issue_number": case.issue_number, "pr_url": attempt.pr_url},
                )
            )
            return
        if overall in {CI_FAILED, CI_TIMED_OUT}:
            attempt.failure_stage = "ci"
            attempt.failure_class = FAILURE_CLASS_VERIFICATION
            attempt.error = summary
            case.failure_reason = summary
            await self._transition(CaseState.CI_FAILED, summary)
            return
        # pending / absent: stay in CI_PENDING; the worker re-claims after the poll interval.
