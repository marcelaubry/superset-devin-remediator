from remediator.lifecycle import TERMINAL_STATES, TRANSITIONS, CaseState


def test_happy_path() -> None:
    state = CaseState.RECEIVED
    for next_state in [
        CaseState.ELIGIBILITY_EVALUATED,
        CaseState.TRIAGE_CREATE_INTENT,
        CaseState.TRIAGING,
        CaseState.TRIAGED,
        CaseState.AWAITING_REMEDIATION_APPROVAL,
        CaseState.REMEDIATION_APPROVED,
        # BASE must reproduce the defect before any paid session exists.
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
    ]:
        assert next_state in TRANSITIONS[state]
        state = next_state


def test_illegal_transition() -> None:
    assert CaseState.CI_PASSED not in TRANSITIONS[CaseState.RECEIVED]


def test_probe_base_gates_session_creation() -> None:
    # The only way into the create intent is through a BASE probe run; BASE-pass and
    # infrastructure outcomes leave without ever reaching a session.
    assert CaseState.REMEDIATION_CREATE_INTENT not in TRANSITIONS[CaseState.REMEDIATION_APPROVED]
    entries = {
        s for s, targets in TRANSITIONS.items() if CaseState.REMEDIATION_CREATE_INTENT in targets
    }
    assert entries == {CaseState.PROBE_VALIDATING_BASE}
    assert {
        CaseState.REMEDIATION_HUMAN_BLOCKED,
        CaseState.PROBE_INFRASTRUCTURE_BLOCKED,
    } <= set(TRANSITIONS[CaseState.PROBE_VALIDATING_BASE])
    assert set(TRANSITIONS[CaseState.PROBE_INFRASTRUCTURE_BLOCKED]) <= {
        CaseState.PROBE_VALIDATING_BASE,
        CaseState.REMEDIATION_CANCELLED,
    }
    # After the PR exists only HEAD runs; BASE is never re-entered post-session.
    assert CaseState.PROBE_VALIDATING_BASE not in TRANSITIONS[CaseState.PR_VALIDATING]
    assert CaseState.PROBE_VALIDATING_HEAD in TRANSITIONS[CaseState.PR_VALIDATING]


def test_terminal_states() -> None:
    assert set(TRANSITIONS) == set(CaseState)
    assert not hasattr(CaseState, "AWAITING_TRIAGE_APPROVAL")
    assert CaseState.TRIAGE_CREATE_INTENT in TRANSITIONS[CaseState.ELIGIBILITY_EVALUATED]
    # Terminal states may only be left by an explicit, authenticated operator retry; every
    # remediation retry re-enters at REMEDIATION_APPROVED so dispatch preconditions re-run.
    for state in TERMINAL_STATES:
        assert set(TRANSITIONS[state]) <= {
            CaseState.RECEIVED,
            CaseState.REMEDIATION_APPROVED,
            CaseState.PROBE_VALIDATING_BASE,
            CaseState.PROBE_VALIDATING_HEAD,
            CaseState.CI_PENDING,
            CaseState.REMEDIATION_CANCELLED,
        }


def test_docker_build_context_excludes_local_env_files() -> None:
    from pathlib import Path

    lines = (Path(__file__).resolve().parents[1] / ".dockerignore").read_text().splitlines()
    assert ".env" in lines and ".env.*" in lines and "!.env.example" in lines


def test_phase3_approval_states() -> None:
    awaiting = TRANSITIONS[CaseState.AWAITING_REMEDIATION_APPROVAL]
    # Slack approval alone never moves the case; only the GitHub label webhook does, and
    # nothing in Phase 3 enters REMEDIATION_CREATE_INTENT directly from the approval gate.
    assert CaseState.REMEDIATION_CREATE_INTENT not in awaiting
    assert {
        CaseState.REMEDIATION_APPROVED,
        CaseState.REMEDIATION_REJECTED,
        CaseState.APPROVAL_DELIVERY_FAILED,
    } <= awaiting
    assert CaseState.REMEDIATION_APPROVED in TRANSITIONS[CaseState.APPROVAL_DELIVERY_FAILED]
    assert TRANSITIONS[CaseState.REMEDIATION_REJECTED] == frozenset()
