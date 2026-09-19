from remediator.lifecycle import TERMINAL_STATES, TRANSITIONS, CaseState


def test_happy_path() -> None:
    state = CaseState.RECEIVED
    for next_state in [
        CaseState.ELIGIBILITY_EVALUATED,
        CaseState.AWAITING_TRIAGE_APPROVAL,
        CaseState.TRIAGE_CREATE_INTENT,
        CaseState.TRIAGING,
        CaseState.TRIAGED,
        CaseState.AWAITING_REMEDIATION_APPROVAL,
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
    for state in TERMINAL_STATES:
        assert set(TRANSITIONS[state]) <= {CaseState.RECEIVED}
