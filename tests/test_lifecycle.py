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
        CaseState.REMEDIATION_CREATE_INTENT,
        CaseState.REMEDIATING,
        CaseState.OUTPUT_VALIDATING,
        CaseState.PR_VALIDATED,
        CaseState.CI_PENDING,
        CaseState.CI_PASSED,
    ]:
        assert next_state in TRANSITIONS[state]
        state = next_state


def test_illegal_transition() -> None:
    assert CaseState.CI_PASSED not in TRANSITIONS[CaseState.RECEIVED]


def test_terminal_states() -> None:
    assert set(TRANSITIONS) == set(CaseState)
    assert not hasattr(CaseState, "AWAITING_TRIAGE_APPROVAL")
    assert CaseState.TRIAGE_CREATE_INTENT in TRANSITIONS[CaseState.ELIGIBILITY_EVALUATED]
    for state in TERMINAL_STATES:
        assert set(TRANSITIONS[state]) <= {
            CaseState.RECEIVED,
            CaseState.REMEDIATION_CREATE_INTENT,
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
