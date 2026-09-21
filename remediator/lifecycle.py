from dataclasses import dataclass
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
    APPROVAL_DELIVERY_FAILED = "APPROVAL_DELIVERY_FAILED"
    REMEDIATION_APPROVED = "REMEDIATION_APPROVED"
    REMEDIATION_REJECTED = "REMEDIATION_REJECTED"
    REMEDIATION_CREATE_INTENT = "REMEDIATION_CREATE_INTENT"
    REMEDIATION_RECONCILING_CREATE = "REMEDIATION_RECONCILING_CREATE"
    REMEDIATING = "REMEDIATING"
    REMEDIATION_HUMAN_BLOCKED = "REMEDIATION_HUMAN_BLOCKED"
    OUTPUT_VALIDATING = "OUTPUT_VALIDATING"
    PR_DISCOVERED = "PR_DISCOVERED"
    PR_VALIDATING = "PR_VALIDATING"
    PROBE_VALIDATING_BASE = "PROBE_VALIDATING_BASE"
    PROBE_INFRASTRUCTURE_BLOCKED = "PROBE_INFRASTRUCTURE_BLOCKED"
    PROBE_VALIDATING_HEAD = "PROBE_VALIDATING_HEAD"
    PR_VALIDATED = "PR_VALIDATED"
    CI_PENDING = "CI_PENDING"
    CI_PASSED = "CI_PASSED"
    CI_FAILED = "CI_FAILED"
    REMEDIATION_FAILED = "REMEDIATION_FAILED"
    REMEDIATION_TERMINATION_PENDING = "REMEDIATION_TERMINATION_PENDING"
    REMEDIATION_TIMED_OUT = "REMEDIATION_TIMED_OUT"
    REMEDIATION_CANCELLED = "REMEDIATION_CANCELLED"
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
        CaseState.CI_FAILED,
        CaseState.TIMED_OUT,
        CaseState.POLICY_REJECTED,
        CaseState.REMEDIATION_REJECTED,
        CaseState.REMEDIATION_FAILED,
        CaseState.REMEDIATION_TIMED_OUT,
        CaseState.REMEDIATION_CANCELLED,
        CaseState.FAILED,
        CaseState.CANCELLED,
    }
)

# States owned by the Phase 4 remediation pipeline. Failures, cancellations and
# terminations inside this set always use the REMEDIATION_* variants so the audit trail
# never conflates a lost triage session with a lost remediation session.
REMEDIATION_PHASE_STATES = frozenset(
    {
        CaseState.REMEDIATION_APPROVED,
        CaseState.REMEDIATION_CREATE_INTENT,
        CaseState.REMEDIATION_RECONCILING_CREATE,
        CaseState.REMEDIATING,
        CaseState.REMEDIATION_HUMAN_BLOCKED,
        CaseState.OUTPUT_VALIDATING,
        CaseState.PR_DISCOVERED,
        CaseState.PR_VALIDATING,
        CaseState.PROBE_VALIDATING_BASE,
        CaseState.PROBE_INFRASTRUCTURE_BLOCKED,
        CaseState.PROBE_VALIDATING_HEAD,
        CaseState.PR_VALIDATED,
        CaseState.CI_PENDING,
        CaseState.CI_PASSED,
        CaseState.CI_FAILED,
        CaseState.REMEDIATION_FAILED,
        CaseState.REMEDIATION_TERMINATION_PENDING,
        CaseState.REMEDIATION_TIMED_OUT,
        CaseState.REMEDIATION_CANCELLED,
    }
)
# Remediation states in which a Devin session may exist for the current attempt.
REMEDIATION_SESSION_STATES = frozenset(
    {
        CaseState.REMEDIATION_CREATE_INTENT,
        CaseState.REMEDIATION_RECONCILING_CREATE,
        CaseState.REMEDIATING,
        CaseState.REMEDIATION_TERMINATION_PENDING,
    }
)
# Remediation states from which an authenticated operator may start a *new* attempt.
REMEDIATION_RETRYABLE_STATES = frozenset(
    {
        CaseState.REMEDIATION_FAILED,
        CaseState.REMEDIATION_TIMED_OUT,
        CaseState.REMEDIATION_HUMAN_BLOCKED,
    }
)
_ACTIVE = frozenset(CaseState) - TERMINAL_STATES
_REMEDIATION_EXITS = frozenset({CaseState.REMEDIATION_FAILED, CaseState.REMEDIATION_CANCELLED})
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
    # Approval is recorded on the approval request; the case only advances once the
    # signed GitHub `labeled` webhook confirms `devin:remediate` was applied.
    CaseState.AWAITING_REMEDIATION_APPROVAL: frozenset(
        {
            CaseState.REMEDIATION_APPROVED,
            CaseState.REMEDIATION_REJECTED,
            CaseState.APPROVAL_DELIVERY_FAILED,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.APPROVAL_DELIVERY_FAILED: frozenset(
        {
            CaseState.REMEDIATION_APPROVED,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.REMEDIATION_REJECTED: frozenset(),
    # ----------------------------------------------------------- Phase 4 pipeline
    # Dispatch preconditions are evaluated here, then the immutable probe must reproduce
    # the defect at the pinned base SHA; nothing paid happens until the durable create
    # intent is committed in REMEDIATION_CREATE_INTENT.
    CaseState.REMEDIATION_APPROVED: frozenset(
        {CaseState.PROBE_VALIDATING_BASE, CaseState.REMEDIATION_HUMAN_BLOCKED} | _REMEDIATION_EXITS
    ),
    CaseState.PROBE_VALIDATING_BASE: frozenset(
        {
            CaseState.REMEDIATION_CREATE_INTENT,
            CaseState.PROBE_INFRASTRUCTURE_BLOCKED,
            CaseState.REMEDIATION_HUMAN_BLOCKED,
        }
        | _REMEDIATION_EXITS
    ),
    # Missing runtime/tools for the probe: zero ACUs were spent; an operator re-runs the
    # base probe once the verifier is repaired.
    CaseState.PROBE_INFRASTRUCTURE_BLOCKED: frozenset(
        {CaseState.PROBE_VALIDATING_BASE, CaseState.REMEDIATION_CANCELLED}
    ),
    CaseState.REMEDIATION_CREATE_INTENT: frozenset(
        {
            CaseState.REMEDIATION_RECONCILING_CREATE,
            CaseState.REMEDIATING,
            CaseState.REMEDIATION_TERMINATION_PENDING,
        }
        | _REMEDIATION_EXITS
    ),
    CaseState.REMEDIATION_RECONCILING_CREATE: frozenset(
        {
            CaseState.REMEDIATING,
            CaseState.REMEDIATION_HUMAN_BLOCKED,
            CaseState.REMEDIATION_TERMINATION_PENDING,
        }
        | _REMEDIATION_EXITS
    ),
    CaseState.REMEDIATING: frozenset(
        {
            CaseState.OUTPUT_VALIDATING,
            CaseState.REMEDIATION_HUMAN_BLOCKED,
            CaseState.REMEDIATION_TIMED_OUT,
            CaseState.REMEDIATION_TERMINATION_PENDING,
        }
        | _REMEDIATION_EXITS
    ),
    CaseState.OUTPUT_VALIDATING: frozenset(
        {CaseState.PR_DISCOVERED, CaseState.REMEDIATION_HUMAN_BLOCKED} | _REMEDIATION_EXITS
    ),
    CaseState.PR_DISCOVERED: frozenset(
        {CaseState.PR_VALIDATING, CaseState.REMEDIATION_HUMAN_BLOCKED} | _REMEDIATION_EXITS
    ),
    CaseState.PR_VALIDATING: frozenset(
        {CaseState.PROBE_VALIDATING_HEAD, CaseState.REMEDIATION_HUMAN_BLOCKED} | _REMEDIATION_EXITS
    ),
    CaseState.PROBE_VALIDATING_HEAD: frozenset(
        {CaseState.PR_VALIDATED, CaseState.REMEDIATION_HUMAN_BLOCKED} | _REMEDIATION_EXITS
    ),
    CaseState.PR_VALIDATED: frozenset({CaseState.CI_PENDING} | _REMEDIATION_EXITS),
    CaseState.CI_PENDING: frozenset(
        {CaseState.CI_PASSED, CaseState.CI_FAILED, CaseState.REMEDIATION_HUMAN_BLOCKED}
        | _REMEDIATION_EXITS
    ),
    # Terminal for the automation: required checks passed for the verified head SHA.
    # Merge and issue closure remain human decisions.
    CaseState.CI_PASSED: frozenset(),
    # An operator may re-synchronise CI (e.g. after a manual re-run on GitHub).
    CaseState.CI_FAILED: frozenset({CaseState.CI_PENDING, CaseState.REMEDIATION_CANCELLED}),
    CaseState.REMEDIATION_HUMAN_BLOCKED: frozenset(
        {CaseState.REMEDIATION_APPROVED, CaseState.REMEDIATION_TERMINATION_PENDING}
        | _REMEDIATION_EXITS
    ),
    CaseState.REMEDIATION_TERMINATION_PENDING: frozenset(
        {
            CaseState.OUTPUT_VALIDATING,
            CaseState.REMEDIATION_HUMAN_BLOCKED,
            CaseState.REMEDIATION_TIMED_OUT,
        }
        | _REMEDIATION_EXITS
    ),
    # Retries always create a new attempt/operation key; PROBE_VALIDATING_HEAD / CI_PENDING
    # are only reachable again for infrastructure failures (operator "retry probe
    # verification" / "retry CI synchronisation") on the already-verified attempt.
    CaseState.REMEDIATION_FAILED: frozenset(
        {
            CaseState.REMEDIATION_APPROVED,
            CaseState.PROBE_VALIDATING_HEAD,
            CaseState.CI_PENDING,
            CaseState.REMEDIATION_CANCELLED,
        }
    ),
    CaseState.REMEDIATION_TIMED_OUT: frozenset(
        {CaseState.REMEDIATION_APPROVED, CaseState.REMEDIATION_CANCELLED}
    ),
    CaseState.REMEDIATION_CANCELLED: frozenset(),
    # ------------------------------------------------------- generic (Phase 1-3)
    CaseState.HUMAN_BLOCKED: frozenset(
        {
            CaseState.CANCELLED,
            CaseState.FAILED,
            CaseState.RECEIVED,
            CaseState.TERMINATION_PENDING,
            # Retained session re-read and found to carry structured output.
            CaseState.TRIAGING,
        }
    ),
    CaseState.RECONCILING_CREATE: frozenset(
        {
            CaseState.TRIAGING,
            CaseState.HUMAN_BLOCKED,
            CaseState.FAILED,
            CaseState.CANCELLED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    CaseState.TERMINATION_PENDING: frozenset(
        {
            CaseState.TIMED_OUT,
            CaseState.TRIAGED,
            CaseState.HUMAN_BLOCKED,
            CaseState.CANCELLED,
            CaseState.FAILED,
        }
    ),
    CaseState.TIMED_OUT: frozenset({CaseState.RECEIVED}),
    CaseState.POLICY_REJECTED: frozenset(),
    CaseState.FAILED: frozenset({CaseState.RECEIVED}),
    CaseState.CANCELLED: frozenset(),
}
for _state in _ACTIVE - REMEDIATION_PHASE_STATES:
    TRANSITIONS[_state] = TRANSITIONS[_state] | frozenset(
        {CaseState.FAILED, CaseState.CANCELLED, CaseState.TERMINATION_PENDING}
    )

# Edges that exist *only* when no acceptance probe is configured under
# PROBE_POLICY=if_available: the two probe gates that have nothing to run. They are
# rejected on every case that does have a probe.
PROBE_NOT_CONFIGURED_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    # No probe is registered, so there is no BASE gate to run before the create intent.
    CaseState.REMEDIATION_APPROVED: frozenset({CaseState.REMEDIATION_CREATE_INTENT}),
    # The PR was corroborated by GitHub, but no HEAD probe exists to run against it.
    CaseState.PR_VALIDATING: frozenset({CaseState.PR_VALIDATED}),
}


@dataclass(frozen=True)
class PhaseStates:
    """The state vocabulary a Devin-session-running phase uses for one attempt kind."""

    intent: CaseState
    running: CaseState
    reconciling: CaseState
    human_blocked: CaseState
    termination_pending: CaseState
    timed_out: CaseState
    failed: CaseState
    cancelled: CaseState


TRIAGE_PHASE = PhaseStates(
    intent=CaseState.TRIAGE_CREATE_INTENT,
    running=CaseState.TRIAGING,
    reconciling=CaseState.RECONCILING_CREATE,
    human_blocked=CaseState.HUMAN_BLOCKED,
    termination_pending=CaseState.TERMINATION_PENDING,
    timed_out=CaseState.TIMED_OUT,
    failed=CaseState.FAILED,
    cancelled=CaseState.CANCELLED,
)
REMEDIATION_PHASE = PhaseStates(
    intent=CaseState.REMEDIATION_CREATE_INTENT,
    running=CaseState.REMEDIATING,
    reconciling=CaseState.REMEDIATION_RECONCILING_CREATE,
    human_blocked=CaseState.REMEDIATION_HUMAN_BLOCKED,
    termination_pending=CaseState.REMEDIATION_TERMINATION_PENDING,
    timed_out=CaseState.REMEDIATION_TIMED_OUT,
    failed=CaseState.REMEDIATION_FAILED,
    cancelled=CaseState.REMEDIATION_CANCELLED,
)
TERMINATION_PENDING_STATES = frozenset(
    {TRIAGE_PHASE.termination_pending, REMEDIATION_PHASE.termination_pending}
)


def phase_for_state(state: CaseState | str) -> PhaseStates:
    return REMEDIATION_PHASE if CaseState(state) in REMEDIATION_PHASE_STATES else TRIAGE_PHASE


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
    probe_not_configured: bool = False,
) -> None:
    from_state = CaseState(case.state)
    allowed = TRANSITIONS[from_state]
    if probe_not_configured:
        allowed = allowed | PROBE_NOT_CONFIGURED_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidTransition(f"{from_state} cannot transition to {to_state}")
    from . import metrics
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
    metrics.observe_transition(case, to_state, actor)
    session.add(
        StateTransition(
            case_id=case.id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            actor=actor,
        )
    )
