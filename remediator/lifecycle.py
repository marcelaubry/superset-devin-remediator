from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import update
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
    CaseState.TIMED_OUT: frozenset({CaseState.RECEIVED, CaseState.REMEDIATION_CREATE_INTENT}),
    CaseState.POLICY_REJECTED: frozenset(),
    CaseState.FAILED: frozenset({CaseState.RECEIVED, CaseState.REMEDIATION_CREATE_INTENT}),
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
    *,
    expected_claimed_by: str | None = None,
) -> None:
    from_state = CaseState(case.state)
    if to_state not in TRANSITIONS[from_state]:
        raise InvalidTransition(f"{from_state} cannot transition to {to_state}")
    from .models import Case, StateTransition

    now = datetime.now(UTC)
    completed_at = now if to_state in TERMINAL_STATES else None
    conditions = [Case.id == case.id, Case.state == from_state]
    if expected_claimed_by is not None:
        conditions.append(Case.claimed_by == expected_claimed_by)
    result = cast(
        Any,
        await session.execute(
            update(Case)
            .where(*conditions)
            .values(
                state=to_state,
                state_entered_at=now,
                completed_at=completed_at,
                version=Case.version + 1,
            )
        ),
    )
    if result.rowcount == 0:
        suffix = " or lease lost" if expected_claimed_by is not None else ""
        raise InvalidTransition(f"case {case.id} is no longer in {from_state}{suffix}")
    case.state = to_state
    case.state_entered_at = now
    case.completed_at = completed_at
    case.version += 1
    session.add(
        StateTransition(
            case_id=case.id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            actor=actor,
        )
    )
