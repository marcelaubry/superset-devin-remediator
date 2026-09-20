"""Presentation-only mappings for the operator dashboard.

Every enum the templates render goes through one of these tables so tone, glyph and
label stay consistent. Nothing here touches lifecycle semantics: the tables are a
read-only view over the enums defined in `lifecycle` and `models`.
"""

from dataclasses import dataclass
from enum import Enum

from ..lifecycle import TERMINAL_STATES, CaseState

TONES = frozenset({"ok", "info", "warn", "err", "neutral"})

GLYPHS = {
    "progress": ("●", "In progress"),
    "waiting": ("◔", "Waiting"),
    "blocked": ("⛔", "Blocked"),
    "ok": ("✓", "Succeeded"),
    "err": ("✗", "Failed"),
    "closed": ("⊘", "Closed"),
}


@dataclass(frozen=True)
class Presentation:
    tone: str
    glyph: str
    word: str
    label: str

    @property
    def raw(self) -> str:
        return self.label


def _p(tone: str, glyph_key: str, value: str) -> Presentation:
    glyph, word = GLYPHS[glyph_key]
    return Presentation(tone=tone, glyph=glyph, word=word, label=value)


_PROGRESS = {
    CaseState.RECEIVED,
    CaseState.ELIGIBILITY_EVALUATED,
    CaseState.TRIAGE_CREATE_INTENT,
    CaseState.TRIAGING,
    CaseState.TRIAGED,
    CaseState.RECONCILING_CREATE,
    CaseState.REMEDIATION_APPROVED,
    CaseState.REMEDIATION_CREATE_INTENT,
    CaseState.REMEDIATION_RECONCILING_CREATE,
    CaseState.REMEDIATING,
    CaseState.OUTPUT_VALIDATING,
    CaseState.PR_DISCOVERED,
    CaseState.PR_VALIDATING,
    CaseState.PROBE_VALIDATING_BASE,
    CaseState.PROBE_VALIDATING_HEAD,
    CaseState.PR_VALIDATED,
    CaseState.CI_PENDING,
}
_WAITING = {
    CaseState.AWAITING_REMEDIATION_APPROVAL,
    CaseState.APPROVAL_DELIVERY_FAILED,
    CaseState.TERMINATION_PENDING,
    CaseState.REMEDIATION_TERMINATION_PENDING,
}
_BLOCKED = {
    CaseState.HUMAN_BLOCKED,
    CaseState.REMEDIATION_HUMAN_BLOCKED,
    CaseState.PROBE_INFRASTRUCTURE_BLOCKED,
}
_FAILED = {
    CaseState.FAILED,
    CaseState.TIMED_OUT,
    CaseState.REMEDIATION_FAILED,
    CaseState.REMEDIATION_TIMED_OUT,
    CaseState.CI_FAILED,
}
_CLOSED = {
    CaseState.POLICY_REJECTED,
    CaseState.REMEDIATION_REJECTED,
    CaseState.CANCELLED,
    CaseState.REMEDIATION_CANCELLED,
}


def _state_presentation(state: CaseState) -> Presentation:
    if state in _PROGRESS:
        return _p("info", "progress", state.value)
    if state in _WAITING:
        return _p("warn", "waiting", state.value)
    if state in _BLOCKED:
        return _p("warn", "blocked", state.value)
    if state == CaseState.CI_PASSED:
        return _p("ok", "ok", state.value)
    if state in _FAILED:
        return _p("err", "err", state.value)
    if state in _CLOSED:
        return _p("neutral", "closed", state.value)
    raise ValueError(f"no presentation for {state}")  # pragma: no cover


STATE_PRESENTATION: dict[str, Presentation] = {
    state.value: _state_presentation(state) for state in CaseState
}

# Workflow funnel: the ordered stages a case passes through. Together with the terminal
# outcomes these partition every CaseState exactly once (enforced by tests).
FUNNEL_STAGES: dict[str, frozenset[CaseState]] = {
    "intake": frozenset({CaseState.RECEIVED, CaseState.ELIGIBILITY_EVALUATED}),
    "triage": frozenset(
        {
            CaseState.TRIAGE_CREATE_INTENT,
            CaseState.RECONCILING_CREATE,
            CaseState.TRIAGING,
            CaseState.TRIAGED,
            CaseState.HUMAN_BLOCKED,
            CaseState.TERMINATION_PENDING,
        }
    ),
    "approval": frozenset(
        {
            CaseState.AWAITING_REMEDIATION_APPROVAL,
            CaseState.APPROVAL_DELIVERY_FAILED,
            CaseState.REMEDIATION_APPROVED,
        }
    ),
    "remediation": frozenset(
        {
            CaseState.REMEDIATION_CREATE_INTENT,
            CaseState.REMEDIATION_RECONCILING_CREATE,
            CaseState.REMEDIATING,
            CaseState.REMEDIATION_HUMAN_BLOCKED,
            CaseState.REMEDIATION_TERMINATION_PENDING,
            CaseState.OUTPUT_VALIDATING,
        }
    ),
    "pr": frozenset({CaseState.PR_DISCOVERED, CaseState.PR_VALIDATING, CaseState.PR_VALIDATED}),
    "probe": frozenset(
        {
            CaseState.PROBE_VALIDATING_BASE,
            CaseState.PROBE_INFRASTRUCTURE_BLOCKED,
            CaseState.PROBE_VALIDATING_HEAD,
        }
    ),
    "ci": frozenset({CaseState.CI_PENDING}),
}
FUNNEL_LABELS = {
    "intake": "Intake",
    "triage": "Triage",
    "approval": "Approval",
    "remediation": "Remediation",
    "pr": "PR",
    "probe": "Probe",
    "ci": "CI",
}
OUTCOME_STATES: tuple[CaseState, ...] = (
    CaseState.CI_PASSED,
    CaseState.CI_FAILED,
    CaseState.POLICY_REJECTED,
    CaseState.FAILED,
    CaseState.TIMED_OUT,
    CaseState.REMEDIATION_REJECTED,
    CaseState.REMEDIATION_FAILED,
    CaseState.REMEDIATION_TIMED_OUT,
    CaseState.CANCELLED,
    CaseState.REMEDIATION_CANCELLED,
)
assert frozenset(OUTCOME_STATES) == TERMINAL_STATES


def stage_for_state(state: CaseState | str) -> str | None:
    """Funnel stage of a non-terminal state; None for terminal outcomes."""
    value = CaseState(state)
    for name, members in FUNNEL_STAGES.items():
        if value in members:
            return name
    return None


@dataclass(frozen=True)
class FunnelStage:
    key: str
    label: str
    count: int
    states: dict[str, int]
    width: int  # percentage of the busiest stage, 0 when empty, >= 2 when > 0


@dataclass(frozen=True)
class FunnelOutcome:
    state: str
    count: int
    presentation: Presentation


@dataclass(frozen=True)
class Funnel:
    stages: list[FunnelStage]
    outcomes: list[FunnelOutcome]
    in_flight: int
    terminal: int


def build_funnel(state_counts: dict[str, int]) -> Funnel:
    granular_by_stage = {
        name: {s.value: state_counts.get(s.value, 0) for s in sorted(members, key=str)}
        for name, members in FUNNEL_STAGES.items()
    }
    totals = {name: sum(states.values()) for name, states in granular_by_stage.items()}
    peak = max(totals.values(), default=0)
    stages = [
        FunnelStage(
            key=name,
            label=FUNNEL_LABELS[name],
            count=totals[name],
            states=states,
            width=0 if not peak or not totals[name] else max(2, round(100 * totals[name] / peak)),
        )
        for name, states in granular_by_stage.items()
    ]
    outcomes = [
        FunnelOutcome(
            state=s.value,
            count=state_counts.get(s.value, 0),
            presentation=STATE_PRESENTATION[s.value],
        )
        for s in OUTCOME_STATES
    ]
    return Funnel(
        stages=stages,
        outcomes=outcomes,
        in_flight=sum(totals.values()),
        terminal=sum(o.count for o in outcomes),
    )


# Secondary enums. Keys are the raw enum values (or lower-case strings for ci_status).
CI_STATUS_TONE = {
    "success": ("ok", "ok"),
    "failure": ("err", "err"),
    "timed_out": ("err", "err"),
    "pending": ("info", "progress"),
    "absent": ("neutral", "closed"),
}
VERDICT_TONE = {
    "MATCHED": ("ok", "ok"),
    "MISMATCHED": ("err", "err"),
    "INFRASTRUCTURE": ("warn", "blocked"),
}
DECISION_TONE = {
    "APPROVED": ("ok", "ok"),
    "REJECTED": ("neutral", "closed"),
    "EXPIRED": ("neutral", "closed"),
    "SUPERSEDED": ("neutral", "closed"),
    "PENDING": ("warn", "waiting"),
}
DELIVERY_TONE = {
    "CONFIRMED": ("ok", "ok"),
    "LABEL_APPLIED": ("info", "progress"),
    "PENDING": ("info", "progress"),
    "FAILED": ("err", "err"),
    "NOT_REQUESTED": ("neutral", "closed"),
}
NOTIFICATION_TONE = {
    "PENDING": ("info", "progress"),
    "SENDING": ("info", "progress"),
    "SENT": ("ok", "ok"),
    "FAILED": ("err", "err"),
}
OUTBOX_TONE = {
    "PENDING": ("info", "progress"),
    "SENT": ("ok", "ok"),
    "FAILED": ("err", "err"),
}
EVENT_TONE = {
    "PENDING": ("info", "waiting"),
    "PROCESSING": ("info", "progress"),
    "PROCESSED": ("ok", "ok"),
    "FAILED": ("err", "err"),
}
ATTEMPT_TONE = {
    "RUNNING": ("info", "progress"),
    "SUCCEEDED": ("ok", "ok"),
    "FAILED": ("err", "err"),
    "BLOCKED": ("warn", "blocked"),
    "CANCELLED": ("neutral", "closed"),
    "RECONCILING": ("warn", "waiting"),
    "TERMINATION_PENDING": ("warn", "waiting"),
    "TIMED_OUT": ("err", "err"),
}
CREATE_STATE_TONE = {
    "PENDING": ("info", "waiting"),
    "NOT_SENT": ("neutral", "closed"),
    "CREATED": ("ok", "ok"),
    "UNCERTAIN": ("warn", "blocked"),
    "RECONCILED": ("ok", "ok"),
    "API_ERROR": ("err", "err"),
    "UNRESOLVED": ("warn", "blocked"),
}
PR_STATE_TONE = {
    "open": ("info", "progress"),
    "closed": ("neutral", "closed"),
    "merged": ("ok", "ok"),
}
CHECK_TONE = {
    "success": ("ok", "ok"),
    "failure": ("err", "err"),
    "timed_out": ("err", "err"),
    "cancelled": ("neutral", "closed"),
    "neutral": ("neutral", "closed"),
    "skipped": ("neutral", "closed"),
    "action_required": ("warn", "blocked"),
    "in_progress": ("info", "progress"),
    "queued": ("info", "waiting"),
    "pending": ("info", "waiting"),
}

FAMILIES: dict[str, dict[str, tuple[str, str]]] = {
    "ci": CI_STATUS_TONE,
    "verdict": VERDICT_TONE,
    "decision": DECISION_TONE,
    "delivery": DELIVERY_TONE,
    "notification": NOTIFICATION_TONE,
    "outbox": OUTBOX_TONE,
    "event": EVENT_TONE,
    "attempt": ATTEMPT_TONE,
    "create": CREATE_STATE_TONE,
    "pr": PR_STATE_TONE,
    "check": CHECK_TONE,
}


def present(family: str, value: object) -> Presentation:
    """Presentation for a secondary enum value; unknown values fall back to neutral."""
    raw = value.value if isinstance(value, Enum) else str(value if value is not None else "-")
    tone, glyph_key = FAMILIES.get(family, {}).get(raw, ("neutral", "closed"))
    return _p(tone, glyph_key, raw)


def present_state(value: object) -> Presentation:
    raw = value.value if isinstance(value, Enum) else str(value)
    return STATE_PRESENTATION.get(raw) or _p("neutral", "closed", raw)


def mode_tone(mode: str) -> str:
    return "live" if mode == "live" else "neutral"
