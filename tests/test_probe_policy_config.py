"""Configuration, readiness, prompt/schema and lifecycle rules of `PROBE_POLICY`, and the
readiness migration off the removed `LIVE_CANARY_ALLOW_MISSING_PROBE`. No provider is
contacted."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from remediator.config import ProbePolicy
from remediator.devin.prompt import RemediationPromptInput, render_remediation_prompt
from remediator.devin.remediation import (
    REMEDIATION_OUTPUT_SCHEMA,
    REMEDIATION_OUTPUT_SCHEMA_WITHOUT_PROBE,
    RemediationValidationError,
    validate_remediation_output,
)
from remediator.lifecycle import (
    PROBE_NOT_CONFIGURED_TRANSITIONS,
    TRANSITIONS,
    CaseState,
)
from remediator.probe_policy import (
    PROBE_NOT_CONFIGURED_NOTE,
    is_missing_probe_block,
    is_missing_probe_error,
)
from remediator.readiness import Probes, run_checks
from remediator.worker.devin_runner import RemediationContext
from tests.test_phase6_canary import _canary_kwargs, _settings
from tests.test_readiness import _by_name, _happy_handler, _live_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- configuration


def test_probe_policy_defaults_to_required() -> None:
    settings = _settings()
    assert settings.probe_policy is ProbePolicy.REQUIRED
    assert settings.probe_required is True
    assert settings.live_canary_allow_missing_probe is None
    assert settings.obsolete_probe_override_problem is None


def test_if_available_is_a_normal_setting_with_no_extra_safeguards() -> None:
    settings = _settings(**_canary_kwargs(probe_policy="if_available"))
    assert settings.probe_policy is ProbePolicy.IF_AVAILABLE
    assert settings.probe_required is False
    assert settings.canary_violations == []


def test_invalid_probe_policy_fails_startup() -> None:
    with pytest.raises(ValidationError, match="probe_policy"):
        _settings(probe_policy="whenever")


@pytest.mark.parametrize(
    ("configured", "replacement"),
    [(False, "required"), (True, "if_available")],
)
def test_obsolete_override_is_reported_with_its_replacement(
    configured: bool, replacement: str
) -> None:
    settings = _settings(live_canary_allow_missing_probe=configured)
    problem = settings.obsolete_probe_override_problem
    assert problem is not None
    assert "LIVE_CANARY_ALLOW_MISSING_PROBE" in problem
    assert f"PROBE_POLICY={replacement}" in problem


def test_the_obsolete_variable_is_detected_from_the_real_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`extra="ignore"` must not hide a deployment that still exports the removed variable."""
    monkeypatch.setenv("LIVE_CANARY_ALLOW_MISSING_PROBE", "true")
    settings = _settings()
    assert settings.live_canary_allow_missing_probe is True
    assert settings.obsolete_probe_override_problem is not None


def test_invalid_probe_policy_from_the_environment_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROBE_POLICY", "whenever")
    with pytest.raises(ValidationError, match="probe_policy"):
        _settings()


def test_only_a_missing_probe_registration_is_recognised() -> None:
    assert is_missing_probe_error("no approved probe registered at /app/probes/x/y/6/probe.yaml")
    assert not is_missing_probe_error("probe script hash does not match the manifest")
    assert is_missing_probe_block(
        "approved probe unavailable: no approved probe registered at /app/probes/x/y/6/probe.yaml"
    )
    for reason in (
        None,
        "",
        "approved probe unavailable: probe script hash does not match",
        "no approved probe registered",  # not the dispatch block reason
        "base probe failed: no approved probe registered",
    ):
        assert not is_missing_probe_block(reason)


# --------------------------------------------------------------------------- readiness


def _probes() -> Probes:
    return Probes(
        devin=_happy_handler,
        github=_happy_handler,
        slack=_happy_handler,
        verifier=_happy_handler,
        public=_happy_handler,
    )


@pytest.mark.asyncio
async def test_readiness_reports_the_configured_policy() -> None:
    probes = _probes()
    required = _by_name(await run_checks(_live_settings(**_canary_kwargs()), probes=probes))
    assert required["probe.policy"].status == "pass"
    assert "PROBE_POLICY=required" in required["probe.policy"].detail
    assert "probe.policy_migration" not in required

    relaxed = _by_name(
        await run_checks(
            _live_settings(**_canary_kwargs(probe_policy="if_available")), probes=probes
        )
    )
    assert relaxed["probe.policy"].status == "warn"
    assert PROBE_NOT_CONFIGURED_NOTE in relaxed["probe.policy"].detail


@pytest.mark.asyncio
async def test_readiness_fails_while_the_obsolete_variable_is_still_configured() -> None:
    results = _by_name(
        await run_checks(
            _live_settings(**_canary_kwargs(live_canary_allow_missing_probe=True)),
            probes=_probes(),
        )
    )
    assert results["probe.policy_migration"].status == "fail"
    assert "PROBE_POLICY=if_available" in results["probe.policy_migration"].detail


# --------------------------------------------------------------------------- prompt & schema


def _output(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": "remediation.v1",
        "outcome": "pr_created",
        "summary": "fixed",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "branch": "devin/fix-issue-6",
        "pr_url": "https://github.com/apache/superset/pull/6",
        "issue_reference": "apache/superset#6",
        "changed_files": ["superset/views/core.py"],
        "commits": ["b" * 40],
        "tests_run": ["pytest -q"],
        "risks": [],
        "blocking_questions": [],
    }
    base.update(overrides)
    return base


def test_no_probe_schema_drops_probe_identity_and_refuses_invented_evidence() -> None:
    assert "probe_identifier" in REMEDIATION_OUTPUT_SCHEMA["properties"]
    assert "probe_identifier" not in REMEDIATION_OUTPUT_SCHEMA_WITHOUT_PROBE["properties"]
    assert REMEDIATION_OUTPUT_SCHEMA_WITHOUT_PROBE["additionalProperties"] is False

    result = validate_remediation_output(_output(), probe=False)
    assert result.probe_identifier is None and result.probe_hash is None

    with pytest.raises(RemediationValidationError):
        validate_remediation_output(
            _output(probe_identifier="invented", probe_hash="c" * 64), probe=False
        )
    # Normal mode is unchanged: probe identity is still required.
    with pytest.raises(RemediationValidationError):
        validate_remediation_output(_output())


def test_no_probe_prompt_states_no_probe_runs_and_never_asks_for_probe_metadata() -> None:
    common: dict[str, object] = {
        "repository": "apache/superset",
        "base_ref": "master",
        "base_sha": "a" * 40,
        "branch_prefix": "devin/",
        "issue_number": 6,
        "issue_title": "Broken",
        "issue_body": "Details",
        "issue_url": "https://github.com/apache/superset/issues/6",
        "triage_output": {"summary": "bounded fix"},
        "triage_result_hash": "e" * 64,
        "probe_registry_path": "/app/probes/apache/superset/6/probe.yaml",
        "approved_by": "U_APPROVER_ONE",
        "approved_at": "2026-01-01T00:00:00Z",
        "operation_key": "op-key",
        "case_id": "case-id",
        "attempt_id": "attempt-id",
    }
    with_probe = render_remediation_prompt(
        RemediationPromptInput(
            **common,
            probe_identifier="probe-6",
            probe_hash="d" * 64,
            probe_script="pytest -q",
            probe_expected_base_exit=1,
            probe_expected_head_exit=0,
        )
    )
    without = render_remediation_prompt(RemediationPromptInput(**common))

    assert "probe-6" in with_probe and "probe_identifier" in with_probe
    assert "probe-6" not in without
    assert "probe_identifier" not in without and "probe_hash" not in without
    assert "No immutable acceptance probe is registered" in without
    assert "Do not claim probe evidence." in without


# --------------------------------------------------------------------------- lifecycle


def test_probe_gates_are_bypassable_only_through_the_explicit_policy_edges() -> None:
    assert CaseState.REMEDIATION_CREATE_INTENT not in TRANSITIONS[CaseState.REMEDIATION_APPROVED]
    assert CaseState.PR_VALIDATED not in TRANSITIONS[CaseState.PR_VALIDATING]
    assert PROBE_NOT_CONFIGURED_TRANSITIONS == {
        CaseState.REMEDIATION_APPROVED: frozenset({CaseState.REMEDIATION_CREATE_INTENT}),
        CaseState.PR_VALIDATING: frozenset({CaseState.PR_VALIDATED}),
    }


def test_remediation_context_needs_explicit_authorisation_to_run_without_a_probe() -> None:
    approval = object()
    triage: dict[str, object] = {}
    with pytest.raises(ValueError, match="probe snapshot"):
        RemediationContext(
            approval=approval,  # type: ignore[arg-type]
            triage_output=triage,
            base_ref="master",
        )
    with pytest.raises(ValueError, match="pinned base SHA"):
        RemediationContext(
            approval=approval,  # type: ignore[arg-type]
            triage_output=triage,
            base_ref="master",
            probe_not_configured=True,
        )
    context = RemediationContext(
        approval=approval,  # type: ignore[arg-type]
        triage_output=triage,
        base_ref="master",
        probe_not_configured=True,
        pinned_base_sha="a" * 40,
    )
    assert context.base_sha == "a" * 40


# --------------------------------------------------------------------------- documentation


def test_env_example_documents_the_policy_and_retires_the_override() -> None:
    text = (REPO_ROOT / ".env.example").read_text()
    assert "PROBE_POLICY=required" in text
    assert "LIVE_CANARY_ALLOW_MISSING_PROBE=" not in text


def test_runbook_documents_the_policy_and_its_neutral_status() -> None:
    text = (REPO_ROOT / "docs" / "canary-runbook.md").read_text()
    assert "PROBE_POLICY=if_available" in text
    assert PROBE_NOT_CONFIGURED_NOTE.rstrip(".") in text.replace("\n", " ")
