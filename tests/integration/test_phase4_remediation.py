"""Phase 4: approved remediation -> Devin session -> PR/probe/CI verification.

Every scenario starts from the real Phase 3 approval flow (Slack click + signed GitHub label
webhook) and then drives `process_case` the way the worker does, with the fake Devin,
GitHub, Slack and probe adapters. Deterministic failure modes are selected by fixture issue
number (see `remediator.fixtures`). No test contacts Devin, GitHub or Slack, and no probe
script is ever executed.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from remediator.approvals import triage_result_hash
from remediator.config import Settings
from remediator.devin.fake import FakeDevinClient
from remediator.fixtures import REMEDIATION_FIXTURES, RemediationFixture, fake_pr_number
from remediator.lifecycle import CaseState
from remediator.models import (
    OUTBOX_KIND_SLACK_REMEDIATION_UPDATE,
    ApprovalDecision,
    ApprovalEvent,
    ApprovalRequest,
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    CiSnapshot,
    OutboxStatus,
    ProbeExecution,
    ProbeSnapshot,
    ProbeTarget,
    ProbeVerdict,
    PullRequestEvidence,
    StateTransition,
    WebhookEvent,
)
from remediator.probes.runner import FakeProbeRunner
from remediator.worker.processor import process_case, process_event
from tests.integration.test_phase3_approval import (  # noqa: F401
    Harness,
    harness,
    harness_factory,
)

FIXTURE_FOR = {fixture: number for number, fixture in REMEDIATION_FIXTURES.items()}


class RemediationHarness:
    def __init__(self, base: Harness) -> None:
        self.base = base
        self.probes = FakeProbeRunner()

    @property
    def settings(self) -> Settings:
        return self.base.settings

    async def approve(self, number: int) -> Case:
        """Phase 3 in full: triage, Slack approve, label outbox, signed label webhook."""
        case, _request, _token = await self.base.notify_and_approve(number)
        await self.base.drain()
        response = await self.base.label_webhook(number, delivery=f"label-{number}")
        assert response.status_code in {200, 202}
        async with self.base.factory() as session:
            event = await session.scalar(
                select(WebhookEvent).where(WebhookEvent.delivery_id == f"label-{number}")
            )
            assert event is not None
            await process_event(session, event, self.base.devin, self.settings)
        case = await self.base.case(case.id)
        assert case.state == CaseState.REMEDIATION_APPROVED
        return case

    async def step(self, case_id: Any, *, devin: FakeDevinClient | None = None) -> Case:
        """One worker claim: exactly what `Worker._process_one` does for a claimed case."""
        async with self.base.factory() as session:
            case = await session.get(Case, case_id)
            assert case is not None
            case.claimed_by = "test-worker"
            case.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            await session.commit()
            await process_case(
                session,
                case,
                devin or self.base.devin,
                self.settings,
                claimed_by="test-worker",
                github=self.base.github,
                probes=self.probes,
            )
            await session.commit()
            return case

    async def run(self, case_id: Any, *, limit: int = 40) -> Case:
        """Drive the case until its state stops changing (a terminal or waiting state)."""
        previous: CaseState | None = None
        repeats = 0
        for _ in range(limit):
            case = await self.step(case_id)
            state = CaseState(case.state)
            repeats = repeats + 1 if state == previous else 0
            previous = state
            if state in _RESTING:
                return case
            # CI_PENDING legitimately persists across polls; give it a few observations.
            if repeats >= (3 if state == CaseState.CI_PENDING else 1):
                return case
        raise AssertionError(f"case did not settle within {limit} steps: {case.state}")

    async def attempts(self, case_id: Any) -> list[Attempt]:
        async with self.base.factory() as session:
            rows = await session.scalars(
                select(Attempt)
                .where(Attempt.case_id == case_id, Attempt.kind == AttemptKind.REMEDIATION)
                .order_by(Attempt.started_at, Attempt.idempotency_key)
            )
            return list(rows.all())

    async def probe_runs(self, case_id: Any) -> list[ProbeExecution]:
        async with self.base.factory() as session:
            rows = await session.scalars(
                select(ProbeExecution)
                .join(ProbeSnapshot, ProbeSnapshot.id == ProbeExecution.probe_snapshot_id)
                .where(ProbeSnapshot.case_id == case_id)
                .order_by(ProbeExecution.started_at, ProbeExecution.id)
            )
            return list(rows.all())

    async def ci(self, case_id: Any) -> list[CiSnapshot]:
        async with self.base.factory() as session:
            rows = await session.scalars(
                select(CiSnapshot)
                .join(Attempt, Attempt.id == CiSnapshot.attempt_id)
                .where(Attempt.case_id == case_id)
                .order_by(CiSnapshot.observed_at)
            )
            return list(rows.all())

    async def evidence(self, case_id: Any) -> list[PullRequestEvidence]:
        async with self.base.factory() as session:
            rows = await session.scalars(
                select(PullRequestEvidence)
                .join(Attempt, Attempt.id == PullRequestEvidence.attempt_id)
                .where(Attempt.case_id == case_id)
            )
            return list(rows.all())

    async def transitions(self, case_id: Any) -> list[tuple[str, str]]:
        async with self.base.factory() as session:
            rows = await session.scalars(
                select(StateTransition)
                .where(StateTransition.case_id == case_id)
                .order_by(StateTransition.seq)
            )
            return [(row.from_state, row.to_state) for row in rows.all()]


_RESTING = frozenset(
    {
        CaseState.CI_PASSED,
        CaseState.CI_FAILED,
        CaseState.REMEDIATION_FAILED,
        CaseState.REMEDIATION_HUMAN_BLOCKED,
        CaseState.REMEDIATION_TIMED_OUT,
        CaseState.REMEDIATION_CANCELLED,
        CaseState.REMEDIATION_TERMINATION_PENDING,
        CaseState.REMEDIATION_RECONCILING_CREATE,
    }
)


@pytest_asyncio.fixture
async def rem(harness: Harness) -> RemediationHarness:  # noqa: F811
    return RemediationHarness(harness)


def number_for(fixture: RemediationFixture) -> int:
    return FIXTURE_FOR[fixture]


# --------------------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_successful_remediation_reaches_ci_passed_with_full_evidence(
    rem: RemediationHarness,
) -> None:
    number = number_for(RemediationFixture.SUCCESS)
    case = await rem.approve(number)
    triage_sessions = rem.base.devin.create_calls

    case = await rem.run(case.id)
    assert case.state == CaseState.CI_PASSED
    assert rem.base.devin.create_calls == triage_sessions + 1

    attempts = await rem.attempts(case.id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.status == AttemptStatus.SUCCEEDED
    assert attempt.pr_number == fake_pr_number(number)
    assert attempt.pr_url == f"https://github.com/apache/superset/pull/{fake_pr_number(number)}"
    assert attempt.head_sha and attempt.branch == f"devin/fix-issue-{number}"
    assert attempt.probe_snapshot_id is not None
    assert attempt.triage_result_hash and attempt.approval_request_id is not None
    async with rem.base.factory() as session:
        snapshot = await session.get(ProbeSnapshot, attempt.probe_snapshot_id)
    assert snapshot is not None
    assert snapshot.script_hash == snapshot.manifest["script_sha256"]
    assert snapshot.base_sha == attempt.base_sha

    runs = await rem.probe_runs(case.id)
    assert [(r.target, r.verdict) for r in runs] == [
        (ProbeTarget.BASE, ProbeVerdict.MATCHED),
        (ProbeTarget.HEAD, ProbeVerdict.MATCHED),
    ]
    assert runs[0].commit_sha == attempt.base_sha and runs[1].commit_sha == attempt.head_sha
    assert runs[0].script_hash == runs[1].script_hash == snapshot.script_hash
    assert runs[0].probe_snapshot_id == runs[1].probe_snapshot_id == snapshot.id
    assert all(r.runner_mode == "fake" and r.exit_code is not None for r in runs)
    # BASE ran (and was persisted) before the create intent; the evidence row was then
    # attached to the attempt it authorised. HEAD ran only after the PR was verified.
    assert runs[0].attempt_id == runs[1].attempt_id == attempt.id
    assert attempt.create_sent_at is not None
    assert runs[0].finished_at is not None and runs[0].finished_at <= attempt.create_sent_at
    assert runs[1].started_at >= attempt.create_sent_at

    snapshots = await rem.ci(case.id)
    assert snapshots and snapshots[-1].overall == "success"
    assert snapshots[-1].head_sha == attempt.head_sha

    evidence = await rem.evidence(case.id)
    assert len(evidence) == 1 and evidence[0].head_sha == attempt.head_sha

    # Append-only history covers the whole remediation phase in order.
    history = [to for _, to in await rem.transitions(case.id)]
    idx = history.index(CaseState.REMEDIATION_APPROVED)
    assert history[idx:] == [
        CaseState.REMEDIATION_APPROVED,
        CaseState.PROBE_VALIDATING_BASE,
        CaseState.REMEDIATION_CREATE_INTENT,
        CaseState.REMEDIATING,
        CaseState.OUTPUT_VALIDATING,
        CaseState.PR_DISCOVERED,
        CaseState.PR_VALIDATING,
        CaseState.PROBE_VALIDATING_HEAD,
        CaseState.PR_VALIDATED,
        CaseState.CI_PENDING,
        CaseState.CI_PASSED,
    ]

    # Nothing was merged or closed: the only GitHub writes are the Phase 3 label/comment.
    assert rem.base.github.label_calls == [("apache/superset", number, "devin:remediate")]
    assert len(rem.base.github.comment_calls) == 1

    # Slack progress updates were queued through the outbox (Devin link, PR, probes, CI).
    rows = await rem.base.outbox(case.id, OUTBOX_KIND_SLACK_REMEDIATION_UPDATE)
    assert len(rows) >= 4
    await rem.base.drain()
    messages = await rem.base.fake_messages()
    assert len(messages) == 1
    text = json.dumps(messages[0].blocks)
    assert "Ready for human review" in text and attempt.pr_url in text
    assert "Probe base: matched" in text and "head: matched" in text
    assert "nothing is merged or closed automatically" in text


# --------------------------------------------------------------------------- helpers


async def _fail_stage(rem: RemediationHarness, case_id: Any) -> tuple[str | None, str | None]:
    attempts = await rem.attempts(case_id)
    assert len(attempts) == 1
    return attempts[0].failure_stage, attempts[0].failure_class


async def _run_fixture(rem: RemediationHarness, fixture: RemediationFixture) -> Case:
    number = number_for(fixture)
    case = await rem.approve(number)
    before = rem.base.devin.create_calls
    case = await rem.run(case.id)
    # Exactly one paid session per approved dispatch, whatever happens afterwards, and it
    # was only created because BASE reproduced the defect first.
    assert rem.base.devin.create_calls == before + 1
    runs = await rem.probe_runs(case.id)
    assert runs and (runs[0].target, runs[0].verdict) == (ProbeTarget.BASE, ProbeVerdict.MATCHED)
    return case


async def _assert_only_base_reproduced(rem: RemediationHarness, case_id: Any) -> None:
    """The single BASE run that authorised the session exists; HEAD never ran."""
    runs = await rem.probe_runs(case_id)
    assert [(r.target, r.verdict) for r in runs] == [(ProbeTarget.BASE, ProbeVerdict.MATCHED)]
    attempts = await rem.attempts(case_id)
    assert len(attempts) == 1 and runs[0].attempt_id == attempts[0].id
    assert runs[0].commit_sha == attempts[0].base_sha


async def _set_ci_deadline_past(rem: RemediationHarness, case_id: Any) -> None:
    async with rem.base.factory() as session:
        attempt = await session.scalar(
            select(Attempt).where(
                Attempt.case_id == case_id, Attempt.kind == AttemptKind.REMEDIATION
            )
        )
        assert attempt is not None
        attempt.ci_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


# --------------------------------------------------------------------------- dispatch


@pytest.mark.asyncio
async def test_duplicate_label_webhook_is_recorded_and_spends_nothing(
    rem: RemediationHarness,
) -> None:
    number = number_for(RemediationFixture.SUCCESS)
    case = await rem.approve(number)
    before = rem.base.devin.create_calls

    response = await rem.base.label_webhook(number, delivery=f"label-{number}-dup")
    assert response.status_code in {200, 202}
    async with rem.base.factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(WebhookEvent.delivery_id == f"label-{number}-dup")
        )
        assert event is not None
        await process_event(session, event, rem.base.devin, rem.settings)
    case = await rem.base.case(case.id)
    assert case.state == CaseState.REMEDIATION_APPROVED
    assert rem.base.devin.create_calls == before
    async with rem.base.factory() as session:
        kinds = list(
            await session.scalars(
                select(ApprovalEvent.kind)
                .join(ApprovalRequest)
                .where(ApprovalRequest.case_id == case.id)
            )
        )
    assert "label_webhook_duplicate" in kinds
    assert await rem.attempts(case.id) == []


@pytest.mark.asyncio
async def test_stale_triage_hash_blocks_dispatch_without_a_session(
    rem: RemediationHarness,
) -> None:
    number = number_for(RemediationFixture.SUCCESS)
    case = await rem.approve(number)
    async with rem.base.factory() as session:
        request = await session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.case_id == case.id)
        )
        assert request is not None
        request.triage_result_hash = "0" * 64
        await session.commit()
    before = rem.base.devin.create_calls
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_FAILED
    assert rem.base.devin.create_calls == before
    assert await rem.attempts(case.id) == []
    assert "triage" in (case.failure_reason or "").lower()


@pytest.mark.asyncio
async def test_missing_approval_blocks_dispatch_without_a_session(
    rem: RemediationHarness,
) -> None:
    number = number_for(RemediationFixture.SUCCESS)
    case = await rem.approve(number)
    async with rem.base.factory() as session:
        request = await session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.case_id == case.id)
        )
        assert request is not None
        request.decision = ApprovalDecision.REJECTED
        await session.commit()
    before = rem.base.devin.create_calls
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_FAILED
    assert rem.base.devin.create_calls == before
    assert await rem.attempts(case.id) == []


@pytest.mark.asyncio
async def test_missing_probe_blocks_dispatch_without_a_session(rem: RemediationHarness) -> None:
    number = 4762  # triage succeeds (not divisible by 3/5/7); no probe is registered
    assert number not in REMEDIATION_FIXTURES
    case = await rem.approve(number)
    before = rem.base.devin.create_calls
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED
    assert rem.base.devin.create_calls == before
    assert await rem.attempts(case.id) == []
    assert "probe" in (case.failure_reason or "").lower()


@pytest.mark.asyncio
async def test_non_candidate_approval_keeps_every_remediation_safeguard(
    rem: RemediationHarness,
) -> None:
    """Approving a `needs_human` triage (with the Slack warning) buys a bounded attempt, not a
    shortcut: the exact triage hash, the registered immutable probe and the label webhook are
    still mandatory and nothing is dispatched to Devin without them."""
    number = 4761  # % 3 -> the fake Devin returns needs_human; no probe is registered
    assert number not in REMEDIATION_FIXTURES
    case = await rem.approve(number)
    async with rem.base.factory() as session:
        attempt = await session.scalar(select(Attempt).where(Attempt.case_id == case.id))
        request = await session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.case_id == case.id)
        )
        assert attempt is not None and attempt.structured_output is not None
        assert request is not None
        assert attempt.structured_output["outcome"] == "needs_human"
        assert request.triage_result_hash == triage_result_hash(attempt.structured_output)
    before = rem.base.devin.create_calls
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED
    assert "probe" in (case.failure_reason or "").lower()
    assert rem.base.devin.create_calls == before
    assert await rem.attempts(case.id) == []

    # Tamper with the bound hash: dispatch fails closed before any probe or session.
    async with rem.base.factory() as session:
        request = await session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.case_id == case.id)
        )
        assert request is not None
        request.triage_result_hash = "0" * 64
        row = await session.get(Case, case.id)
        assert row is not None
        row.state = CaseState.REMEDIATION_APPROVED
        await session.commit()
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_FAILED
    assert "triage" in (case.failure_reason or "").lower()
    assert rem.base.devin.create_calls == before
    assert await rem.attempts(case.id) == []


@pytest.mark.asyncio
async def test_altered_probe_script_blocks_dispatch_without_a_session(
    rem: RemediationHarness, tmp_path: Any
) -> None:
    import shutil

    number = number_for(RemediationFixture.SUCCESS)
    source = rem.settings.probe_root_path / "apache" / "superset" / str(number)
    target = tmp_path / "probes" / "apache" / "superset" / str(number)
    shutil.copytree(source, target)
    (target / "probe.sh").write_text((target / "probe.sh").read_text() + "\necho tampered\n")
    tampered = rem.settings.model_copy(update={"probe_root": str(tmp_path / "probes")})
    rem.base.settings = tampered

    case = await rem.approve(number)
    before = rem.base.devin.create_calls
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED
    assert rem.base.devin.create_calls == before
    assert await rem.attempts(case.id) == []
    assert "hash" in (case.failure_reason or "").lower()


# --------------------------------------------------------------------------- session


@pytest.mark.asyncio
async def test_uncertain_create_is_reconciled_by_exact_tag_without_second_post(
    rem: RemediationHarness,
) -> None:
    number = number_for(RemediationFixture.UNCERTAIN_CREATE)
    case = await rem.approve(number)
    before = rem.base.devin.create_calls

    case = await rem.run(case.id)
    assert case.state == CaseState.CI_PASSED
    assert rem.base.devin.create_calls == before + 1
    attempts = await rem.attempts(case.id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.create_sent_at is not None and attempt.devin_session_id is not None
    assert attempt.operation_key in attempt.devin_tags
    assert attempt.reconciliation_reason is None
    history = [to for _, to in await rem.transitions(case.id)]
    idx = history.index(CaseState.REMEDIATION_CREATE_INTENT)
    assert history[idx : idx + 3] == [
        CaseState.REMEDIATION_CREATE_INTENT,
        CaseState.REMEDIATION_RECONCILING_CREATE,
        CaseState.REMEDIATING,
    ]


@pytest.mark.asyncio
async def test_malformed_structured_output_fails_closed(rem: RemediationHarness) -> None:
    case = await _run_fixture(rem, RemediationFixture.MALFORMED_OUTPUT)
    assert case.state == CaseState.REMEDIATION_FAILED
    stage, _ = await _fail_stage(rem, case.id)
    assert stage == "output"
    await _assert_only_base_reproduced(rem, case.id)
    assert await rem.evidence(case.id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture", "fragment"),
    [
        (RemediationFixture.CONTRADICTORY_PR_URL, "contradicts"),
        (RemediationFixture.PR_CLAIMED_BUT_ABSENT, "does not exist"),
    ],
)
async def test_devin_metadata_and_output_must_agree(
    rem: RemediationHarness, fixture: RemediationFixture, fragment: str
) -> None:
    case = await _run_fixture(rem, fixture)
    assert case.state == CaseState.REMEDIATION_FAILED
    assert fragment in (case.failure_reason or "")
    await _assert_only_base_reproduced(rem, case.id)


@pytest.mark.asyncio
async def test_contradictory_head_sha_fails_pr_validation(rem: RemediationHarness) -> None:
    case = await _run_fixture(rem, RemediationFixture.CONTRADICTORY_HEAD_SHA)
    assert case.state == CaseState.REMEDIATION_FAILED
    stage, _ = await _fail_stage(rem, case.id)
    assert stage == "pr"
    evidence = await rem.evidence(case.id)
    assert len(evidence) == 1 and evidence[0].valid is False
    assert "head_sha" in (case.failure_reason or "")
    await _assert_only_base_reproduced(rem, case.id)


@pytest.mark.asyncio
async def test_needs_human_outcome_blocks_for_a_human(rem: RemediationHarness) -> None:
    case = await _run_fixture(rem, RemediationFixture.NEEDS_HUMAN)
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED
    assert "needs a human" in (case.failure_reason or "")


@pytest.mark.asyncio
async def test_failed_outcome_is_a_session_failure(rem: RemediationHarness) -> None:
    case = await _run_fixture(rem, RemediationFixture.OUTCOME_FAILED)
    assert case.state == CaseState.REMEDIATION_FAILED
    assert await _fail_stage(rem, case.id) == ("output", "session")


@pytest.mark.asyncio
async def test_no_change_needed_is_verified_by_the_probe_not_trusted(
    rem: RemediationHarness,
) -> None:
    # BASE independently reproduced the defect before the session was created, so Devin's
    # claim is refuted by persisted evidence — no second BASE run, no HEAD run.
    case = await _run_fixture(rem, RemediationFixture.NO_CHANGE_NEEDED)
    assert case.state == CaseState.REMEDIATION_FAILED
    await _assert_only_base_reproduced(rem, case.id)
    assert "still fails at base" in (case.failure_reason or "")


# --------------------------------------------------------------------------- GitHub PR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture", "fragment"),
    [
        (RemediationFixture.PR_WRONG_BASE_BRANCH, "base_ref"),
        (RemediationFixture.PR_WRONG_BRANCH_PREFIX, "head_ref_prefix"),
        (RemediationFixture.PR_MERGED, "not_merged"),
        (RemediationFixture.PR_WRONG_AUTHOR, "author"),
        (RemediationFixture.PR_ISSUE_SUBSTRING, "closes_this_issue"),
        (RemediationFixture.PR_FORBIDDEN_FILES, "no_forbidden_paths"),
    ],
)
async def test_github_pr_validation_failures(
    rem: RemediationHarness, fixture: RemediationFixture, fragment: str
) -> None:
    case = await _run_fixture(rem, fixture)
    assert case.state == CaseState.REMEDIATION_FAILED
    stage, _ = await _fail_stage(rem, case.id)
    assert stage == "pr"
    assert fragment in (case.failure_reason or "")
    evidence = await rem.evidence(case.id)
    assert len(evidence) == 1 and evidence[0].valid is False
    # The probe never runs against an unverified PR and CI is never consulted.
    await _assert_only_base_reproduced(rem, case.id)
    assert await rem.ci(case.id) == []


@pytest.mark.asyncio
async def test_issue_ten_does_not_satisfy_issue_one_linkage(rem: RemediationHarness) -> None:
    number = number_for(RemediationFixture.PR_ISSUE_SUBSTRING)
    case = await _run_fixture(rem, RemediationFixture.PR_ISSUE_SUBSTRING)
    evidence = (await rem.evidence(case.id))[0]
    assert evidence.closing_issue_numbers == [number * 10]
    assert number not in evidence.closing_issue_numbers


@pytest.mark.asyncio
async def test_pr_in_wrong_repository_is_a_policy_failure(rem: RemediationHarness) -> None:
    case = await _run_fixture(rem, RemediationFixture.PR_WRONG_REPOSITORY)
    assert case.state == CaseState.REMEDIATION_FAILED
    assert await _fail_stage(rem, case.id) == ("output", "policy")
    assert await rem.evidence(case.id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture", "fragment"),
    [
        (RemediationFixture.PR_SCOPE_EXPANSION, "scope expansion"),
        (RemediationFixture.PR_DIVERGED_BASE, "diverged"),
    ],
)
async def test_scope_expansion_and_divergence_need_a_human(
    rem: RemediationHarness, fixture: RemediationFixture, fragment: str
) -> None:
    case = await _run_fixture(rem, fixture)
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED
    assert fragment in (case.failure_reason or "")
    await _assert_only_base_reproduced(rem, case.id)


# --------------------------------------------------------------------------- probes


@pytest.mark.asyncio
async def test_probe_passing_at_base_becomes_no_change_needed_for_a_human(
    rem: RemediationHarness,
) -> None:
    number = number_for(RemediationFixture.PROBE_BASE_PASSES)
    case = await rem.approve(number)
    before = rem.base.devin.create_calls
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED
    assert "no_change_needed" in (case.failure_reason or "")
    # Zero ACUs: BASE already passed, so no attempt and no Devin session were created.
    assert rem.base.devin.create_calls == before
    assert await rem.attempts(case.id) == []
    runs = await rem.probe_runs(case.id)
    assert [(r.target, r.verdict) for r in runs] == [(ProbeTarget.BASE, ProbeVerdict.MISMATCHED)]
    async with rem.base.factory() as session:
        snapshot = await session.get(ProbeSnapshot, runs[0].probe_snapshot_id)
    assert snapshot is not None and snapshot.case_id == case.id
    assert runs[0].attempt_id is None and runs[0].commit_sha == snapshot.base_sha
    assert await rem.ci(case.id) == []
    history = [to for _, to in await rem.transitions(case.id)]
    assert history[history.index(CaseState.REMEDIATION_APPROVED) :] == [
        CaseState.REMEDIATION_APPROVED,
        CaseState.PROBE_VALIDATING_BASE,
        CaseState.REMEDIATION_HUMAN_BLOCKED,
    ]


@pytest.mark.asyncio
async def test_probe_failing_at_head_fails_verification_without_a_fix_chain(
    rem: RemediationHarness,
) -> None:
    case = await _run_fixture(rem, RemediationFixture.PROBE_HEAD_FAILS)
    assert case.state == CaseState.REMEDIATION_FAILED
    assert await _fail_stage(rem, case.id) == ("probe_head", "verification")
    runs = await rem.probe_runs(case.id)
    assert [(r.target, r.verdict) for r in runs] == [
        (ProbeTarget.BASE, ProbeVerdict.MATCHED),
        (ProbeTarget.HEAD, ProbeVerdict.MISMATCHED),
    ]
    assert await rem.ci(case.id) == []
    # No second Devin session was started to "fix" the failing head.
    assert len(await rem.attempts(case.id)) == 1


@pytest.mark.asyncio
async def test_probe_infrastructure_failure_is_never_success_and_is_retryable(
    rem: RemediationHarness,
) -> None:
    number = number_for(RemediationFixture.PROBE_INFRASTRUCTURE)
    case = await rem.approve(number)
    sessions = rem.base.devin.create_calls
    case = await rem.run(case.id)
    assert case.state == CaseState.PROBE_INFRASTRUCTURE_BLOCKED
    # Zero ACUs: the runtime was unavailable before any attempt existed.
    assert rem.base.devin.create_calls == sessions
    assert await rem.attempts(case.id) == []
    runs = await rem.probe_runs(case.id)
    assert [r.verdict for r in runs] == [ProbeVerdict.INFRASTRUCTURE]
    assert runs[0].exit_code is None and runs[0].error and runs[0].attempt_id is None

    response = await rem.base.client.post(
        f"/operator/cases/{case.id}/retry-probe", headers=rem.base.operator
    )
    assert response.status_code == 200, response.text
    case = await rem.base.case(case.id)
    assert case.state == CaseState.PROBE_VALIDATING_BASE

    rem.probes.overrides[(number, ProbeTarget.BASE)] = 1
    rem.probes.overrides[(number, ProbeTarget.HEAD)] = 0
    case = await rem.run(case.id)
    assert case.state == CaseState.CI_PASSED
    # Exactly one paid session, created only after BASE finally reproduced the defect.
    assert rem.base.devin.create_calls == sessions + 1
    attempts = await rem.attempts(case.id)
    assert len(attempts) == 1
    runs = await rem.probe_runs(case.id)
    assert [(r.target, r.verdict) for r in runs] == [
        (ProbeTarget.BASE, ProbeVerdict.INFRASTRUCTURE),
        (ProbeTarget.BASE, ProbeVerdict.MATCHED),
        (ProbeTarget.HEAD, ProbeVerdict.MATCHED),
    ]
    assert runs[0].attempt_id is None
    assert runs[1].attempt_id == runs[2].attempt_id == attempts[0].id


@pytest.mark.asyncio
async def test_head_infrastructure_failure_retries_head_only_on_the_same_attempt(
    rem: RemediationHarness,
) -> None:
    number = number_for(RemediationFixture.SUCCESS)
    rem.probes.overrides[(number, ProbeTarget.HEAD)] = None
    case = await _run_fixture(rem, RemediationFixture.SUCCESS)
    assert case.state == CaseState.REMEDIATION_FAILED
    assert await _fail_stage(rem, case.id) == ("probe_head", "infrastructure")
    sessions = rem.base.devin.create_calls

    response = await rem.base.client.post(
        f"/operator/cases/{case.id}/retry-probe", headers=rem.base.operator
    )
    assert response.status_code == 200, response.text
    assert (await rem.base.case(case.id)).state == CaseState.PROBE_VALIDATING_HEAD

    del rem.probes.overrides[(number, ProbeTarget.HEAD)]
    case = await rem.run(case.id)
    assert case.state == CaseState.CI_PASSED
    assert rem.base.devin.create_calls == sessions
    attempts = await rem.attempts(case.id)
    assert len(attempts) == 1
    runs = await rem.probe_runs(case.id)
    assert [(r.target, r.verdict) for r in runs] == [
        (ProbeTarget.BASE, ProbeVerdict.MATCHED),
        (ProbeTarget.HEAD, ProbeVerdict.INFRASTRUCTURE),
        (ProbeTarget.HEAD, ProbeVerdict.MATCHED),
    ]
    assert all(r.attempt_id == attempts[0].id for r in runs)


@pytest.mark.asyncio
async def test_probe_retry_is_refused_for_genuine_probe_failures(
    rem: RemediationHarness,
) -> None:
    case = await _run_fixture(rem, RemediationFixture.PROBE_HEAD_FAILS)
    response = await rem.base.client.post(
        f"/operator/cases/{case.id}/retry-probe", headers=rem.base.operator
    )
    assert response.status_code == 409
    assert "infrastructure" in response.json()["detail"]
    assert (await rem.base.case(case.id)).state == CaseState.REMEDIATION_FAILED


# --------------------------------------------------------------------------- CI


@pytest.mark.asyncio
async def test_ci_failed_for_verified_head(rem: RemediationHarness) -> None:
    case = await _run_fixture(rem, RemediationFixture.CI_FAILED)
    assert case.state == CaseState.CI_FAILED
    assert await _fail_stage(rem, case.id) == ("ci", "verification")
    snapshots = await rem.ci(case.id)
    assert snapshots[-1].overall == "failure"
    attempt = (await rem.attempts(case.id))[0]
    assert all(s.head_sha == attempt.head_sha for s in snapshots)
    assert len(await rem.attempts(case.id)) == 1  # no automatic fix chain


@pytest.mark.asyncio
async def test_ci_pending_then_timed_out(rem: RemediationHarness) -> None:
    case = await _run_fixture(rem, RemediationFixture.CI_PENDING_FOREVER)
    assert case.state == CaseState.CI_PENDING
    assert all(s.overall == "pending" for s in await rem.ci(case.id))
    await _set_ci_deadline_past(rem, case.id)
    case = await rem.step(case.id)
    assert case.state == CaseState.CI_FAILED
    assert "did not complete" in (case.failure_reason or "")
    assert (await rem.ci(case.id))[-1].overall == "timed_out"


@pytest.mark.asyncio
async def test_absent_checks_are_not_treated_as_passing(rem: RemediationHarness) -> None:
    case = await _run_fixture(rem, RemediationFixture.CI_ABSENT)
    assert case.state == CaseState.CI_PENDING
    assert all(s.overall == "absent" for s in await rem.ci(case.id))
    await _set_ci_deadline_past(rem, case.id)
    case = await rem.step(case.id)
    assert case.state == CaseState.CI_FAILED


@pytest.mark.asyncio
async def test_retry_ci_resynchronises_without_a_new_session(rem: RemediationHarness) -> None:
    case = await _run_fixture(rem, RemediationFixture.CI_PENDING_FOREVER)
    await _set_ci_deadline_past(rem, case.id)
    case = await rem.step(case.id)
    assert case.state == CaseState.CI_FAILED
    sessions = rem.base.devin.create_calls

    response = await rem.base.client.post(
        f"/operator/cases/{case.id}/retry-ci", headers=rem.base.operator
    )
    assert response.status_code == 200, response.text
    case = await rem.base.case(case.id)
    assert case.state == CaseState.CI_PENDING
    case = await rem.run(case.id)
    assert case.state == CaseState.CI_PENDING  # fixture never completes; still no new session
    assert rem.base.devin.create_calls == sessions
    assert len(await rem.attempts(case.id)) == 1


# --------------------------------------------------------------------------- timeout / cancel


@pytest.mark.asyncio
async def test_session_timeout_terminates_remotely_after_final_get(
    rem: RemediationHarness,
) -> None:
    rem.base.settings = rem.settings.model_copy(
        update={"devin_remediation_timeout_seconds": 0.05, "devin_poll_interval_seconds": 0}
    )
    number = number_for(RemediationFixture.SESSION_TIMEOUT)
    case = await rem.approve(number)
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_TIMED_OUT
    attempts = await rem.attempts(case.id)
    assert len(attempts) == 1 and attempts[0].status == AttemptStatus.TIMED_OUT
    assert rem.base.devin.terminate_calls == [attempts[0].devin_session_id]
    await _assert_only_base_reproduced(rem, case.id)


@pytest.mark.asyncio
async def test_failed_termination_stays_termination_pending(rem: RemediationHarness) -> None:
    rem.base.settings = rem.settings.model_copy(
        update={"devin_remediation_timeout_seconds": 0.05, "devin_poll_interval_seconds": 0}
    )
    number = number_for(RemediationFixture.SESSION_TIMEOUT)
    case = await rem.approve(number)
    rem.base.devin.fail_terminate = True
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_TERMINATION_PENDING
    attempts = await rem.attempts(case.id)
    assert attempts[0].status == AttemptStatus.TERMINATION_PENDING

    # Once DELETE succeeds the case leaves termination-pending; nothing new is created.
    rem.base.devin.fail_terminate = False
    sessions = rem.base.devin.create_calls
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_TIMED_OUT
    assert rem.base.devin.create_calls == sessions


@pytest.mark.asyncio
async def test_operator_cancel_of_active_remediation_terminates_the_session(
    rem: RemediationHarness,
) -> None:
    """Cancel lands while the worker is polling a live session: the poll loop observes
    REMEDIATION_TERMINATION_PENDING, sends DELETE and only then settles the case."""
    rem.base.settings = rem.settings.model_copy(update={"devin_poll_interval_seconds": 0.02})
    number = number_for(RemediationFixture.SESSION_TIMEOUT)
    case = await rem.approve(number)
    worker = asyncio.create_task(rem.step(case.id))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if (await rem.base.case(case.id)).state == CaseState.REMEDIATING:
            break
    assert (await rem.base.case(case.id)).state == CaseState.REMEDIATING

    response = await rem.base.client.post(
        f"/operator/cases/{case.id}/cancel", headers=rem.base.operator
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == CaseState.REMEDIATION_TERMINATION_PENDING
    case = await asyncio.wait_for(worker, timeout=10)
    assert case.state == CaseState.REMEDIATION_CANCELLED
    attempts = await rem.attempts(case.id)
    assert len(attempts) == 1 and attempts[0].status == AttemptStatus.CANCELLED
    assert rem.base.devin.terminate_calls == [attempts[0].devin_session_id]
    await _assert_only_base_reproduced(rem, case.id)

    # Cancel is idempotent-safe: a second cancel on a terminal case is refused.
    again = await rem.base.client.post(
        f"/operator/cases/{case.id}/cancel", headers=rem.base.operator
    )
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_operator_cancel_before_dispatch_spends_nothing(rem: RemediationHarness) -> None:
    number = number_for(RemediationFixture.SUCCESS)
    case = await rem.approve(number)
    before = rem.base.devin.create_calls
    response = await rem.base.client.post(
        f"/operator/cases/{case.id}/cancel", headers=rem.base.operator
    )
    assert response.status_code == 200, response.text
    case = await rem.base.case(case.id)
    assert case.state == CaseState.REMEDIATION_CANCELLED
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_CANCELLED
    assert rem.base.devin.create_calls == before
    assert await rem.attempts(case.id) == []


# --------------------------------------------------------------------------- retry


@pytest.mark.asyncio
async def test_manual_retry_creates_a_new_attempt_and_operation_key(
    rem: RemediationHarness,
) -> None:
    case = await _run_fixture(rem, RemediationFixture.CONTRADICTORY_HEAD_SHA)
    assert case.state == CaseState.REMEDIATION_FAILED
    first = (await rem.attempts(case.id))[0]

    unauth = await rem.base.client.post(f"/operator/cases/{case.id}/retry")
    assert unauth.status_code in {401, 303}  # unauthenticated: refused or sent to login
    assert (await rem.base.case(case.id)).state == CaseState.REMEDIATION_FAILED
    response = await rem.base.client.post(
        f"/operator/cases/{case.id}/retry", headers=rem.base.operator
    )
    assert response.status_code == 200, response.text
    case = await rem.base.case(case.id)
    assert case.state == CaseState.REMEDIATION_APPROVED

    # A second, concurrent retry must be refused: the case is no longer retryable.
    again = await rem.base.client.post(
        f"/operator/cases/{case.id}/retry", headers=rem.base.operator
    )
    assert again.status_code == 409
    assert (await rem.base.case(case.id)).state == CaseState.REMEDIATION_APPROVED

    sessions = rem.base.devin.create_calls
    case = await rem.run(case.id)
    assert rem.base.devin.create_calls == sessions + 1
    attempts = await rem.attempts(case.id)
    assert len(attempts) == 2
    assert attempts[1].operation_key != first.operation_key
    assert attempts[1].idempotency_key != first.idempotency_key
    assert attempts[1].devin_session_id != first.devin_session_id
    # Same deterministic fixture: the second attempt fails the same way, and no third
    # session is started automatically.
    assert case.state == CaseState.REMEDIATION_FAILED
    assert rem.base.devin.create_calls == sessions + 1


@pytest.mark.asyncio
async def test_retry_is_refused_while_a_verified_pr_exists(rem: RemediationHarness) -> None:
    """A PR that GitHub corroborated but whose head failed the probe is a human decision:
    retrying would open a second PR for the same issue, so dispatch refuses to spend."""
    case = await _run_fixture(rem, RemediationFixture.PROBE_HEAD_FAILS)
    sessions = rem.base.devin.create_calls
    response = await rem.base.client.post(
        f"/operator/cases/{case.id}/retry", headers=rem.base.operator
    )
    assert response.status_code == 200, response.text
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED
    assert "verified PR already exists" in (case.failure_reason or "")
    assert rem.base.devin.create_calls == sessions
    assert len(await rem.attempts(case.id)) == 1


@pytest.mark.asyncio
async def test_retry_is_refused_while_an_attempt_is_active(rem: RemediationHarness) -> None:
    rem.base.settings = rem.settings.model_copy(
        update={"devin_remediation_timeout_seconds": 0.05, "devin_poll_interval_seconds": 0}
    )
    number = number_for(RemediationFixture.SESSION_TIMEOUT)
    case = await rem.approve(number)
    rem.base.devin.fail_terminate = True
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_TERMINATION_PENDING
    response = await rem.base.client.post(
        f"/operator/cases/{case.id}/retry", headers=rem.base.operator
    )
    assert response.status_code == 409
    assert len(await rem.attempts(case.id)) == 1


@pytest.mark.asyncio
async def test_retry_after_timeout_needs_a_human_and_mints_a_new_key(
    rem: RemediationHarness,
) -> None:
    rem.base.settings = rem.settings.model_copy(
        update={"devin_remediation_timeout_seconds": 0.05, "devin_poll_interval_seconds": 0}
    )
    number = number_for(RemediationFixture.SESSION_TIMEOUT)
    case = await rem.approve(number)
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_TIMED_OUT
    # The worker never retries by itself.
    case = await rem.step(case.id)
    assert case.state == CaseState.REMEDIATION_TIMED_OUT
    response = await rem.base.client.post(
        f"/operator/cases/{case.id}/retry", headers=rem.base.operator
    )
    assert response.status_code == 200, response.text
    case = await rem.run(case.id)
    attempts = await rem.attempts(case.id)
    assert len(attempts) == 2
    assert len({a.operation_key for a in attempts}) == 2


# --------------------------------------------------------------------------- restart / evidence


@pytest.mark.asyncio
async def test_worker_restart_at_every_state_resumes_from_persisted_truth(
    rem: RemediationHarness,
) -> None:
    """Each `step` is a fresh claim with a fresh session/pipeline (exactly a worker restart
    between states). Nothing is re-done: one session, one probe run per target, and the
    append-only history has no duplicated states."""
    number = number_for(RemediationFixture.SUCCESS)
    case = await rem.approve(number)
    before = rem.base.devin.create_calls
    seen: list[CaseState] = []
    for _ in range(30):
        # Simulate a crash mid-claim: the lease is held by another worker id and expired.
        async with rem.base.factory() as session:
            row = await session.get(Case, case.id)
            assert row is not None
            row.claimed_by = "crashed-worker"
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        case = await rem.step(case.id)
        seen.append(CaseState(case.state))
        if CaseState(case.state) == CaseState.CI_PASSED:
            break
    assert seen[-1] == CaseState.CI_PASSED
    assert rem.base.devin.create_calls == before + 1
    assert len(await rem.attempts(case.id)) == 1
    runs = await rem.probe_runs(case.id)
    assert [r.target for r in runs] == [ProbeTarget.BASE, ProbeTarget.HEAD]
    history = [to for _, to in await rem.transitions(case.id)]
    tail = history[history.index(CaseState.REMEDIATION_APPROVED) :]
    assert len(tail) == len(set(tail))


@pytest.mark.asyncio
async def test_operator_json_exposes_full_remediation_evidence(rem: RemediationHarness) -> None:
    number = number_for(RemediationFixture.SUCCESS)
    case = await _run_fixture(rem, RemediationFixture.SUCCESS)
    response = await rem.base.client.get(
        f"/api/cases/apache/superset/{number}", headers=rem.base.operator
    )
    assert response.status_code == 200
    body = response.json()
    remediation = body["remediation"]
    assert remediation["state"] == "CI_PASSED" and remediation["ready_for_human_review"]
    assert remediation["actions"] == {
        "cancel": False,
        "retry": False,
        "retry_ci": False,
        "retry_probe": False,
    }
    [attempt] = remediation["attempts"]
    assert attempt["pr_url"] == f"https://github.com/apache/superset/pull/{fake_pr_number(number)}"
    assert attempt["probe_snapshot"]["script_hash"]
    assert [run["target"] for run in attempt["probe_executions"]] == ["BASE", "HEAD"]
    assert attempt["ci_snapshots"][-1]["overall"] == "success"
    assert attempt["pull_request_evidence"][0]["valid"] is True
    assert attempt["duration_seconds"] is not None
    assert attempt["devin_pull_requests"][0]["pr_url"] == attempt["pr_url"]

    page = await rem.base.client.get(f"/cases/{case.id}", headers=rem.base.operator)
    assert page.status_code == 200
    assert "Remediation (Phase 4)" in page.text
    assert "Independent probe executions" in page.text
    assert "ready for human PR review" in page.text


@pytest.mark.asyncio
async def test_slack_update_failure_does_not_change_remediation_truth(
    rem: RemediationHarness,
) -> None:
    number = number_for(RemediationFixture.SUCCESS)
    case = await rem.approve(number)
    rem.base.slack.fail_posts = True
    case = await rem.run(case.id)
    assert case.state == CaseState.CI_PASSED
    await rem.base.drain()
    rows = await rem.base.outbox(case.id, OUTBOX_KIND_SLACK_REMEDIATION_UPDATE)
    assert rows and all(row.status != OutboxStatus.SENT for row in rows)
    assert (await rem.base.case(case.id)).state == CaseState.CI_PASSED
    assert (await rem.attempts(case.id))[0].status == AttemptStatus.SUCCEEDED
