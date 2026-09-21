"""Acceptance-probe policy: `PROBE_POLICY=required` (default) and `if_available`.

Every scenario runs the real Phase 3 approval (Slack click + signed label webhook) and then
drives the worker with the fake Devin, GitHub, Slack and probe adapters. The no-probe
scenarios point `PROBE_ROOT` at an empty directory so the registry reports "no approved
probe registered" exactly as the live case does.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from remediator import metrics
from remediator.config import ProbePolicy, Settings
from remediator.fixtures import RemediationFixture, fake_pr_number
from remediator.lifecycle import CaseState
from remediator.models import (
    OUTBOX_KIND_SLACK_REMEDIATION_UPDATE,
    ApprovalRequest,
    Attempt,
    AttemptKind,
    AttemptStatus,
    CanaryProbeOverride,
    Case,
    ProbeExecution,
    ProbePolicyDecision,
    ProbeSnapshot,
)
from remediator.probe_policy import (
    LEGACY_CANARY_OVERRIDE_WARNING,
    LEGACY_CANARY_PROBE_OVERRIDE,
    PROBE_NOT_CONFIGURED_NOTE,
    PROBE_NOT_CONFIGURED_REASON,
    PROBE_POLICY_ACTOR,
    ProbeStatus,
)
from remediator.worker import Worker
from tests.integration.test_phase3_approval import (  # noqa: F401
    Harness,
    harness,
    harness_factory,
)
from tests.integration.test_phase4_remediation import (  # noqa: F401
    RemediationHarness,
    number_for,
    rem,
)

SUCCESS = number_for(RemediationFixture.SUCCESS)


def _no_probes(settings: Settings, tmp_path: Any) -> Settings:
    """The live failure mode: a probe root with nothing registered for the issue."""
    root = tmp_path / "empty-probes"
    root.mkdir(exist_ok=True)
    return settings.model_copy(update={"probe_root": str(root)})


def _if_available(settings: Settings, **overrides: Any) -> Settings:
    return settings.model_copy(update={"probe_policy": ProbePolicy.IF_AVAILABLE, **overrides})


async def _decisions(h: RemediationHarness, case_id: Any) -> list[ProbePolicyDecision]:
    async with h.base.factory() as session:
        rows = await session.scalars(
            select(ProbePolicyDecision)
            .where(ProbePolicyDecision.case_id == case_id)
            .order_by(ProbePolicyDecision.created_at)
        )
        return list(rows.all())


def _probe_outcomes() -> dict[tuple[str, str], float]:
    return {
        (sample.labels["target"], sample.labels["outcome"]): sample.value
        for family in metrics.probe_outcomes_total.collect()
        for sample in family.samples
        if sample.name.endswith("_total")
    }


async def _probe_rows(h: RemediationHarness, case_id: Any) -> tuple[int, int]:
    async with h.base.factory() as session:
        snapshots = await session.scalar(
            select(func.count()).select_from(ProbeSnapshot).where(ProbeSnapshot.case_id == case_id)
        )
        executions = await session.scalar(select(func.count()).select_from(ProbeExecution))
    return int(snapshots or 0), int(executions or 0)


@pytest_asyncio.fixture
async def blocked(rem: RemediationHarness, tmp_path: Any) -> Case:  # noqa: F811
    """An approved case blocked exactly the way the live one is: no probe is registered."""
    rem.base.settings = _no_probes(rem.settings, tmp_path)
    case = await rem.approve(SUCCESS)
    before = rem.base.devin.create_calls
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED
    assert (case.failure_reason or "").startswith("approved probe unavailable:")
    assert rem.base.devin.create_calls == before
    return case


# --------------------------------------------------------------------------- required (default)


@pytest.mark.asyncio
async def test_missing_probe_blocks_under_the_default_required_policy(
    rem: RemediationHarness,  # noqa: F811
    blocked: Case,
) -> None:
    assert rem.settings.probe_required is True
    assert await rem.attempts(blocked.id) == []
    assert await _decisions(rem, blocked.id) == []
    assert await _probe_rows(rem, blocked.id) == (0, 0)
    # Re-processing keeps it blocked: nothing resumes while the policy is `required`.
    case = await rem.step(blocked.id)
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED


@pytest.mark.asyncio
async def test_registered_probe_is_verified_normally_under_if_available(
    rem: RemediationHarness,  # noqa: F811
) -> None:
    """`if_available` changes nothing when a probe exists: BASE and HEAD still run."""
    rem.base.settings = _if_available(rem.settings)
    case = await rem.approve(SUCCESS)
    case = await rem.run(case.id)
    assert case.state == CaseState.CI_PASSED

    attempts = await rem.attempts(case.id)
    assert len(attempts) == 1
    assert attempts[0].probe_status == ProbeStatus.VERIFIED.value
    assert attempts[0].probe_snapshot_id is not None
    snapshots, executions = await _probe_rows(rem, case.id)
    assert snapshots == 1 and executions == 2
    assert await _decisions(rem, case.id) == []


@pytest.mark.asyncio
async def test_registry_errors_other_than_a_missing_probe_never_proceed(
    rem: RemediationHarness,  # noqa: F811
    tmp_path: Any,
) -> None:
    import shutil

    source = rem.settings.probe_root_path / "apache" / "superset" / str(SUCCESS)
    target = tmp_path / "probes" / "apache" / "superset" / str(SUCCESS)
    shutil.copytree(source, target)
    (target / "probe.sh").write_text((target / "probe.sh").read_text() + "\necho tampered\n")
    rem.base.settings = _if_available(rem.settings, probe_root=str(tmp_path / "probes"))

    case = await rem.approve(SUCCESS)
    before = rem.base.devin.create_calls
    case = await rem.run(case.id)
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED
    assert "hash" in (case.failure_reason or "").lower()
    assert rem.base.devin.create_calls == before
    assert await rem.attempts(case.id) == []
    assert await _decisions(rem, case.id) == []


# --------------------------------------------------------------------------- resume


@pytest.mark.asyncio
async def test_blocked_case_resumes_to_ci_passed_without_any_probe_evidence(
    rem: RemediationHarness,  # noqa: F811
    blocked: Case,
    tmp_path: Any,
) -> None:
    triage_sessions = rem.base.devin.create_calls
    approvals_before = await rem.base.outbox(blocked.id)
    outcomes_before = _probe_outcomes()
    rem.base.settings = _if_available(_no_probes(rem.settings, tmp_path))

    case = await rem.run(blocked.id)
    assert case.state == CaseState.CI_PASSED
    # Exactly one remediation session; no second triage session, approval or issue.
    assert rem.base.devin.create_calls == triage_sessions + 1
    attempts = await rem.attempts(case.id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.status == AttemptStatus.SUCCEEDED
    assert attempt.probe_status == ProbeStatus.NOT_CONFIGURED.value
    assert attempt.canary_probe_override is False
    assert attempt.probe_snapshot_id is None
    assert attempt.pr_number == fake_pr_number(SUCCESS)
    async with rem.base.factory() as session:
        requests = (
            await session.scalars(select(ApprovalRequest).where(ApprovalRequest.case_id == case.id))
        ).all()
    assert len(requests) == 1
    assert attempt.triage_result_hash == requests[0].triage_result_hash

    # The only probe metric movement is the distinct `not_configured` outcome.
    outcomes = _probe_outcomes()
    moved = {
        key: value - outcomes_before.get(key, 0.0)
        for key, value in outcomes.items()
        if value != outcomes_before.get(key, 0.0)
    }
    assert moved == {("none", ProbeStatus.NOT_CONFIGURED.value): 1.0}

    # No probe evidence of any kind was invented, and no legacy override row was written.
    assert await _probe_rows(rem, case.id) == (0, 0)
    async with rem.base.factory() as session:
        legacy = await session.scalar(
            select(func.count()).select_from(CanaryProbeOverride)  # noqa: F821
        )
    assert int(legacy or 0) == 0

    # One append-only policy decision with the full provenance of what was not verified.
    rows = await _decisions(rem, case.id)
    assert len(rows) == 1
    decision = rows[0]
    assert decision.repository == "apache/superset" and decision.issue_number == SUCCESS
    assert decision.triage_result_hash == requests[0].triage_result_hash
    assert decision.base_sha == attempt.base_sha
    assert decision.policy == "if_available"
    assert decision.probe_status == ProbeStatus.NOT_CONFIGURED.value
    assert decision.reason == PROBE_NOT_CONFIGURED_REASON
    assert decision.note == PROBE_NOT_CONFIGURED_NOTE
    assert decision.approved_by == requests[0].decided_by_slack_user_id
    assert decision.actor == PROBE_POLICY_ACTOR

    # The two probe gates had nothing to run; PR and exact-head CI still ran.
    history = [to for _, to in await rem.transitions(case.id)]
    idx = len(history) - 1 - history[::-1].index(CaseState.REMEDIATION_APPROVED)
    assert history[idx:] == [
        CaseState.REMEDIATION_APPROVED,
        CaseState.REMEDIATION_CREATE_INTENT,
        CaseState.REMEDIATING,
        CaseState.OUTPUT_VALIDATING,
        CaseState.PR_DISCOVERED,
        CaseState.PR_VALIDATING,
        CaseState.PR_VALIDATED,
        CaseState.CI_PENDING,
        CaseState.CI_PASSED,
    ]
    snapshots = await rem.ci(case.id)
    assert snapshots and snapshots[-1].overall == "success"
    assert snapshots[-1].head_sha == attempt.head_sha
    evidence = await rem.evidence(case.id)
    assert len(evidence) == 1 and evidence[0].head_sha == attempt.head_sha

    # Nothing merged, nothing closed: the only GitHub writes remain the Phase 3 label/comment.
    assert rem.base.github.label_calls == [("apache/superset", SUCCESS, "devin:remediate")]
    assert len(rem.base.github.comment_calls) == 1
    assert not hasattr(rem.base.github, "merge_pull_request")
    assert not hasattr(rem.base.github, "close_issue")

    # Slack states the neutral status once and never claims a probe passed.
    assert len(await rem.base.outbox(blocked.id, "slack_approval_request")) == len(
        [row for row in approvals_before if row.kind == "slack_approval_request"]
    )
    assert await rem.base.outbox(case.id, OUTBOX_KIND_SLACK_REMEDIATION_UPDATE)
    await rem.base.drain()
    messages = await rem.base.fake_messages()
    assert len(messages) == 1
    text = json.dumps(messages[0].blocks)
    assert PROBE_NOT_CONFIGURED_NOTE in text
    assert "CANARY OVERRIDE" not in text
    assert ":warning:" not in text
    assert "Probe base" not in text and "probe passed" not in text.lower()


@pytest.mark.asyncio
async def test_duplicate_processing_creates_no_second_decision_session_or_attempt(
    rem: RemediationHarness,  # noqa: F811
    blocked: Case,
    tmp_path: Any,
) -> None:
    rem.base.settings = _if_available(_no_probes(rem.settings, tmp_path))
    case = await rem.run(blocked.id)
    assert case.state == CaseState.CI_PASSED
    sessions = rem.base.devin.create_calls

    for _ in range(3):
        case = await rem.step(case.id)
    assert case.state == CaseState.CI_PASSED
    assert rem.base.devin.create_calls == sessions
    assert len(await rem.attempts(case.id)) == 1
    assert len(await _decisions(rem, case.id)) == 1
    assert await _probe_rows(rem, case.id) == (0, 0)
    await rem.base.drain()
    assert len(await rem.base.fake_messages()) == 1


@pytest.mark.asyncio
async def test_dashboard_detail_shows_not_configured_as_a_normal_status(
    rem: RemediationHarness,  # noqa: F811
    blocked: Case,
    tmp_path: Any,
) -> None:
    rem.base.settings = _if_available(_no_probes(rem.settings, tmp_path))
    case = await rem.run(blocked.id)
    assert case.state == CaseState.CI_PASSED

    response = await rem.base.client.get(f"/cases/{case.id}", headers=rem.base.operator)
    assert response.status_code == 200
    body = response.text
    assert "Not configured" in body
    assert PROBE_NOT_CONFIGURED_NOTE in body
    assert LEGACY_CANARY_PROBE_OVERRIDE not in body
    assert "probe passed" not in body.lower()


@pytest.mark.asyncio
async def test_historical_override_rows_are_still_rendered(
    rem: RemediationHarness,  # noqa: F811
    blocked: Case,
) -> None:
    """Cases that ran under the removed override keep their audit row and warning."""
    async with rem.base.factory() as session:
        session.add(
            CanaryProbeOverride(
                case_id=blocked.id,
                repository="apache/superset",
                issue_number=SUCCESS,
                triage_result_hash="a" * 64,
                base_sha="b" * 40,
                actor="config:LIVE_CANARY_ALLOW_MISSING_PROBE",
                approved_by="U_APPROVER_ONE",
                reason="missing probe accepted for controlled canary",
                warning=LEGACY_CANARY_OVERRIDE_WARNING,
            )
        )
        await session.commit()

    response = await rem.base.client.get(f"/cases/{blocked.id}", headers=rem.base.operator)
    assert response.status_code == 200
    assert LEGACY_CANARY_PROBE_OVERRIDE in response.text
    assert LEGACY_CANARY_OVERRIDE_WARNING in response.text


# --------------------------------------------------------------------------- still blocked


@pytest.mark.asyncio
async def test_superseded_approval_hash_still_blocks_under_if_available(
    rem: RemediationHarness,  # noqa: F811
    blocked: Case,
    tmp_path: Any,
) -> None:
    rem.base.settings = _if_available(_no_probes(rem.settings, tmp_path))
    async with rem.base.factory() as session:
        request = await session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.case_id == blocked.id)
        )
        assert request is not None
        request.triage_result_hash = "0" * 64
        await session.commit()
    before = rem.base.devin.create_calls

    case = await rem.run(blocked.id)
    assert case.state == CaseState.REMEDIATION_FAILED
    assert "triage" in (case.failure_reason or "").lower()
    assert rem.base.devin.create_calls == before
    assert await rem.attempts(case.id) == []
    assert await _decisions(rem, case.id) == []


@pytest.mark.asyncio
async def test_unconfirmed_label_webhook_still_blocks_under_if_available(
    rem: RemediationHarness,  # noqa: F811
    blocked: Case,
    tmp_path: Any,
) -> None:
    rem.base.settings = _if_available(_no_probes(rem.settings, tmp_path))
    async with rem.base.factory() as session:
        request = await session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.case_id == blocked.id)
        )
        assert request is not None
        request.label_confirmed_at = None
        await session.commit()
    before = rem.base.devin.create_calls

    case = await rem.run(blocked.id)
    assert case.state == CaseState.REMEDIATION_FAILED
    assert "label" in (case.failure_reason or "").lower()
    assert rem.base.devin.create_calls == before
    assert await _decisions(rem, case.id) == []


@pytest.mark.asyncio
async def test_repository_outside_the_allowlist_still_blocks_under_if_available(
    rem: RemediationHarness,  # noqa: F811
    blocked: Case,
    tmp_path: Any,
) -> None:
    rem.base.settings = _if_available(
        _no_probes(rem.settings, tmp_path), github_repository="marcelaubry/superset"
    )
    before = rem.base.devin.create_calls

    case = await rem.run(blocked.id)
    assert case.state == CaseState.REMEDIATION_FAILED
    assert "allowlist" in (case.failure_reason or "").lower()
    assert rem.base.devin.create_calls == before
    assert await _decisions(rem, case.id) == []


# --------------------------------------------------------------------------- PR / CI gates


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture", "fragment"),
    [
        (RemediationFixture.CONTRADICTORY_PR_URL, "contradicts"),
        (RemediationFixture.PR_WRONG_REPOSITORY, "repository"),
        (RemediationFixture.PR_MERGED, "merged"),
    ],
)
async def test_pr_validation_remains_mandatory_without_a_probe(
    rem: RemediationHarness,  # noqa: F811
    tmp_path: Any,
    fixture: RemediationFixture,
    fragment: str,
) -> None:
    number = number_for(fixture)
    rem.base.settings = _if_available(_no_probes(rem.settings, tmp_path))
    case = await rem.approve(number)
    case = await rem.run(case.id)
    assert case.state in {CaseState.REMEDIATION_FAILED, CaseState.REMEDIATION_HUMAN_BLOCKED}
    assert fragment in (case.failure_reason or "").lower()
    assert not [row for row in await rem.evidence(case.id) if row.valid]
    assert await _probe_rows(rem, case.id) == (0, 0)


@pytest.mark.asyncio
async def test_exact_head_ci_remains_mandatory_without_a_probe(
    rem: RemediationHarness,  # noqa: F811
    tmp_path: Any,
) -> None:
    number = number_for(RemediationFixture.CI_FAILED)
    rem.base.settings = _if_available(_no_probes(rem.settings, tmp_path))
    case = await rem.approve(number)
    case = await rem.run(case.id)
    assert case.state == CaseState.CI_FAILED

    attempts = await rem.attempts(case.id)
    assert len(attempts) == 1
    assert attempts[0].probe_status == ProbeStatus.NOT_CONFIGURED.value
    snapshots = await rem.ci(case.id)
    assert snapshots and snapshots[-1].head_sha == attempts[0].head_sha
    assert snapshots[-1].overall != "success"
    assert await _probe_rows(rem, case.id) == (0, 0)


# --------------------------------------------------------------------------- worker claim


def _canary_settings(database_url: str, **overrides: Any) -> Settings:
    """A real (validated) canary configuration, not a `model_copy` shortcut."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=database_url,
        live_canary=True,
        github_repository="apache/superset",
        github_required_label="devin:triage",
        max_concurrent_triage=1,
        max_concurrent_remediation=1,
        max_concurrent_probes=1,
        max_concurrent_remediation_per_repository=1,
        probe_runner_mode="remote",
        probe_verifier_url="http://verifier:8100",
        probe_verifier_shared_secret="v" * 48,
        cookie_secure=True,
        worker_poll_interval_seconds=0.01,
        worker_concurrency=1,
        **overrides,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["required", "if_available"])
async def test_blocked_case_is_claimable_only_under_if_available(
    integration_session_factory: Any,
    test_database_url: str,
    policy: str,
) -> None:
    async with integration_session_factory() as session:
        case = Case(
            issue_number=SUCCESS,
            repository="apache/superset",
            issue_title="Fix issue",
            issue_url=f"https://github.com/apache/superset/issues/{SUCCESS}",
            state=CaseState.REMEDIATION_HUMAN_BLOCKED,
            state_entered_at=datetime.now(UTC) - timedelta(minutes=5),
            failure_reason=(
                "approved probe unavailable: no approved probe registered at "
                f"/app/probes/apache/superset/{SUCCESS}/probe.yaml"
            ),
        )
        session.add(case)
        await session.commit()

    worker = Worker(_canary_settings(test_database_url, probe_policy=policy))
    try:
        claimed = await worker._claim_case()
        assert (claimed is not None) is (policy == "if_available")
    finally:
        await worker.devin.aclose()
        await worker.engine.dispose()


@pytest.mark.asyncio
async def test_other_blocked_reasons_are_never_claimed_under_if_available(
    integration_session_factory: Any,
    test_database_url: str,
) -> None:
    async with integration_session_factory() as session:
        session.add(
            Case(
                issue_number=SUCCESS + 1,
                repository="apache/superset",
                issue_title="Fix issue",
                issue_url=f"https://github.com/apache/superset/issues/{SUCCESS + 1}",
                state=CaseState.REMEDIATION_HUMAN_BLOCKED,
                state_entered_at=datetime.now(UTC) - timedelta(minutes=5),
                failure_reason="approved probe unavailable: script hash does not match",
            )
        )
        await session.commit()

    worker = Worker(_canary_settings(test_database_url, probe_policy="if_available"))
    try:
        assert await worker._claim_case() is None
    finally:
        await worker.devin.aclose()
        await worker.engine.dispose()


@pytest.mark.asyncio
async def test_no_change_needed_is_refused_without_base_probe_evidence(
    rem: RemediationHarness,  # noqa: F811
    tmp_path: Any,
) -> None:
    number = number_for(RemediationFixture.NO_CHANGE_NEEDED)
    rem.base.settings = _if_available(_no_probes(rem.settings, tmp_path))
    case = await rem.approve(number)
    case = await rem.run(case.id)
    assert case.state in {CaseState.REMEDIATION_FAILED, CaseState.REMEDIATION_HUMAN_BLOCKED}
    async with rem.base.factory() as session:
        attempt = await session.scalar(
            select(Attempt).where(
                Attempt.case_id == case.id, Attempt.kind == AttemptKind.REMEDIATION
            )
        )
    assert attempt is not None
    assert attempt.probe_status == ProbeStatus.NOT_CONFIGURED.value
    assert await _probe_rows(rem, case.id) == (0, 0)
