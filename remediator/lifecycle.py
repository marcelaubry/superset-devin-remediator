from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from .models import Case


class CaseState(StrEnum):
    RECEIVED = "RECEIVED"
    ELIGIBILITY_EVALUATED = "ELIGIBILITY_EVALUATED"
    TRIAGE_CREATE_INTENT = "TRIAGE_CREATE_INTENT"
    TRIAGING = "TRIAGING"
    TRIAGED = "TRIAGED"
    AWAITING_REMEDIATION_APPROVAL = "AWAITING_REMEDIATION_APPROVAL"
    REMEDIATION_CREATE_INTENT = "REMEDIATION_CREATE_INTENT"
    REMEDIATING = "REMEDIATING"
    OUTPUT_VALIDATING = "OUTPUT_VALIDATING"
    PR_VALIDATED = "PR_VALIDATED"
    CI_PENDING = "CI_PENDING"
    CI_PASSED = "CI_PASSED"
    HUMAN_BLOCKED = "HUMAN_BLOCKED"
    RECONCILING_CREATE = "RECONCILING_CREATE"
    TERMINATION_PENDING = "TERMINATION_PENDING"
    TIMED_OUT = "TIMED_OUT"
    POLICY_REJECTED = "POLICY_REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset(
    {
        CaseState.CI_PASSED,
        CaseState.TIMED_OUT,
        CaseState.POLICY_REJECTED,
        CaseState.FAILED,
        CaseState.CANCELLED,
    }
)
_ACTIVE = frozenset(CaseState) - TERMINAL_STATES
TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.RECEIVED: frozenset(
        {
            CaseState.ELIGIBILITY_EVALUATED,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.ELIGIBILITY_EVALUATED: frozenset(
        {
            CaseState.TRIAGE_CREATE_INTENT,
            CaseState.POLICY_REJECTED,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.TRIAGE_CREATE_INTENT: frozenset(
        {
            CaseState.RECONCILING_CREATE,
            CaseState.TRIAGING,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.TRIAGING: frozenset(
        {
            CaseState.TRIAGED,
            CaseState.HUMAN_BLOCKED,
            CaseState.TIMED_OUT,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.TRIAGED: frozenset(
        {
            CaseState.AWAITING_REMEDIATION_APPROVAL,
            CaseState.POLICY_REJECTED,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.AWAITING_REMEDIATION_APPROVAL: frozenset(
        {
            CaseState.REMEDIATION_CREATE_INTENT,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.REMEDIATION_CREATE_INTENT: frozenset(
        {
            CaseState.RECONCILING_CREATE,
            CaseState.REMEDIATING,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.REMEDIATING: frozenset(
        {
            CaseState.OUTPUT_VALIDATING,
            CaseState.HUMAN_BLOCKED,
            CaseState.TIMED_OUT,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.OUTPUT_VALIDATING: frozenset(
        {
            CaseState.PR_VALIDATED,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.PR_VALIDATED: frozenset(
        {CaseState.CI_PENDING, CaseState.FAILED, CaseState.CANCELLED, CaseState.TERMINATION_PENDING}
    ),
    CaseState.CI_PENDING: frozenset(
        {CaseState.CI_PASSED, CaseState.FAILED, CaseState.CANCELLED, CaseState.TERMINATION_PENDING}
    ),
    CaseState.CI_PASSED: frozenset(),
    CaseState.HUMAN_BLOCKED: frozenset(
        {
            CaseState.REMEDIATING,
            CaseState.TRIAGING,
            CaseState.CANCELLED,
            CaseState.FAILED,
            CaseState.RECEIVED,
        }
    ),
    CaseState.RECONCILING_CREATE: frozenset(
        {
            CaseState.TRIAGING,
            CaseState.REMEDIATING,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.TERMINATION_PENDING: frozenset({CaseState.CANCELLED, CaseState.FAILED}),
    CaseState.TIMED_OUT: frozenset({CaseState.RECEIVED}),
    CaseState.POLICY_REJECTED: frozenset(),
    CaseState.FAILED: frozenset({CaseState.RECEIVED}),
    CaseState.CANCELLED: frozenset(),
}
for _state in _ACTIVE:
    TRANSITIONS[_state] = TRANSITIONS[_state] | frozenset(
        {CaseState.FAILED, CaseState.CANCELLED, CaseState.TERMINATION_PENDING}
    )


class InvalidTransition(Exception):
    pass


async def transition(
    session: AsyncSession,
    case: "Case",
    to_state: CaseState,
    reason: str,
    actor: str,
) -> None:
    from_state = CaseState(case.state)
    if to_state not in TRANSITIONS[from_state]:
        raise InvalidTransition(f"{from_state} cannot transition to {to_state}")
    from .models import StateTransition

    now = datetime.now(UTC)
    case.state = to_state
    case.state_entered_at = now
    if to_state in TERMINAL_STATES:
        case.completed_at = now
    session.add(
        StateTransition(
            case_id=case.id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            actor=actor,
        )
    )
