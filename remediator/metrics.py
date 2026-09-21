"""Prometheus metrics.

Every label here is a small closed set (state names, provider names, outcome enums,
`mode` = live|simulated). Issue numbers, session IDs, PR URLs and user IDs are never labels.
Milestones (session completed, output accepted, PR discovered, PR validated, probe passed,
CI passed) are separate counters/states and are never folded into one "success" number.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .lifecycle import CaseState
from .models import Case, NotificationOutbox, OutboxStatus

if TYPE_CHECKING:
    from .capacity import CapacityManager

# Set once at process start from Settings.metrics_mode; "simulated" until then so a
# misconfigured process can never present fake-provider data as live.
_MODE = "simulated"


def configure(mode: str) -> None:
    global _MODE
    _MODE = mode


def mode() -> str:
    return _MODE


# --- request-edge counters ------------------------------------------------------------

webhook_requests_total = Counter(
    "webhook_requests_total",
    "GitHub webhook requests by outcome",
    ["result"],
)

slack_action_requests_total = Counter(
    "slack_action_requests_total",
    "Slack interaction requests by outcome",
    ["result"],
)

operator_requests_rejected_total = Counter(
    "operator_requests_rejected_total",
    "Operator/API requests rejected before handling",
    ["reason"],  # rate_limited | csrf | body_too_large | unauthenticated
)

outbox_deliveries_total = Counter(
    "outbox_deliveries_total",
    "Outbox dispatch attempts by channel and outcome",
    ["channel", "result"],
)

worker_transient_db_errors_total = Counter(
    "worker_transient_db_errors_total",
    "Worker jobs released for retry after a deadlock, serialization or connection error",
)

# --- lifecycle --------------------------------------------------------------------------

cases_received_total = Counter(
    "cases_received_total", "Issues accepted into the case table", ["mode"]
)

eligibility_outcomes_total = Counter(
    "eligibility_outcomes_total",
    "Eligibility decisions",
    ["mode", "outcome"],  # eligible | rejected
)

case_transitions_total = Counter(
    "case_transitions_total",
    "State transitions by destination state and actor",
    ["mode", "to_state", "actor"],
)

# Milestones the user must be able to tell apart. Each is incremented exactly once per
# case entering the corresponding state; none implies any of the others.
MILESTONE_STATES: dict[CaseState, str] = {
    CaseState.TRIAGED: "triage_session_completed",
    CaseState.OUTPUT_VALIDATING: "remediation_session_completed",
    CaseState.PR_DISCOVERED: "structured_output_accepted_pr_discovered",
    CaseState.PR_VALIDATED: "pr_validated",
    CaseState.CI_PASSED: "ci_passed",
}

case_milestones_total = Counter(
    "case_milestones_total", "Distinct lifecycle milestones reached", ["mode", "milestone"]
)

_LATENCY_BUCKETS = (60, 300, 600, 1200, 1800, 3600, 7200, 14400, 28800, 86400)

time_to_milestone_seconds = Histogram(
    "case_time_to_milestone_seconds",
    "Seconds from case creation to reaching a milestone",
    ["mode", "milestone"],  # triaged | first_pr | validated_pr
    buckets=_LATENCY_BUCKETS,
)

_TIMED_MILESTONES: dict[CaseState, str] = {
    CaseState.TRIAGED: "triaged",
    CaseState.PR_DISCOVERED: "first_pr",
    CaseState.PR_VALIDATED: "validated_pr",
}

session_outcomes_total = Counter(
    "devin_session_outcomes_total",
    "Terminal outcome of Devin sessions as observed by the worker",
    ["mode", "phase", "outcome"],  # phase: triage|remediation
)

probe_outcomes_total = Counter(
    "probe_outcomes_total",
    "Probe executions by target and verdict",
    ["mode", "target", "outcome"],  # outcome: expected|unexpected|infrastructure|busy
)

# Canary-only: a remediation was authorised with no acceptance probe registered. This is
# a disclosure of *skipped* verification; it is never a probe outcome.
canary_probe_overrides_total = Counter(
    "canary_probe_overrides_total",
    "Remediations dispatched without base/head acceptance-probe verification",
    ["mode"],
)

ci_outcomes_total = Counter(
    "ci_outcomes_total", "CI verdicts recorded on validated PRs", ["mode", "outcome"]
)

retries_total = Counter(
    "remediator_retries_total",
    "Retries by kind",
    ["mode", "kind"],  # operator_retry | provider_backoff | outbox | worker_requeue
)

reconciliations_total = Counter(
    "remediator_reconciliations_total",
    "Reconciliation passes by kind and result",
    ["mode", "kind", "result"],
)

capacity_denied_total = Counter(
    "capacity_denied_total",
    "Capacity acquisition attempts that had to wait",
    ["mode", "kind"],
)

# --- providers ----------------------------------------------------------------------------

provider_requests_total = Counter(
    "provider_requests_total",
    "Outbound provider HTTP requests",
    ["provider", "method", "status_class"],  # 2xx|3xx|4xx|429|5xx|error
)

provider_request_seconds = Histogram(
    "provider_request_seconds",
    "Outbound provider HTTP latency",
    ["provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

# --- gauges refreshed from the database on scrape -------------------------------------------

queue_depth = Gauge("case_queue_depth", "Open cases by state", ["mode", "state"])
active_jobs = Gauge(
    "active_jobs", "Held capacity leases", ["mode", "kind"]
)  # triage|remediation|probe
capacity_limit = Gauge("capacity_limit", "Configured capacity limit", ["mode", "kind"])
capacity_utilisation = Gauge("capacity_utilisation_ratio", "active / limit", ["mode", "kind"])
waiting_for_capacity = Gauge(
    "cases_waiting_for_capacity", "Cases parked because a limit is full", ["mode"]
)
human_blocked = Gauge("cases_human_blocked", "Cases needing a human right now", ["mode"])
outbox_backlog = Gauge(
    "outbox_backlog", "Undelivered outbox rows", ["mode", "status"]
)  # pending|failed

HUMAN_BLOCKED_STATES = frozenset(
    {
        CaseState.HUMAN_BLOCKED,
        CaseState.REMEDIATION_HUMAN_BLOCKED,
        CaseState.APPROVAL_DELIVERY_FAILED,
        CaseState.AWAITING_REMEDIATION_APPROVAL,
        CaseState.PROBE_INFRASTRUCTURE_BLOCKED,
    }
)


_OPERATOR_RETRY_TARGETS = frozenset(
    {
        CaseState.RECEIVED,
        CaseState.CI_PENDING,
        CaseState.PROBE_VALIDATING_BASE,
        CaseState.PROBE_VALIDATING_HEAD,
    }
)


def observe_transition(case: Case, to_state: CaseState, actor: str) -> None:
    case_transitions_total.labels(_MODE, to_state.value, actor).inc()
    if actor == "operator" and to_state in _OPERATOR_RETRY_TARGETS:
        retries_total.labels(_MODE, "operator_retry").inc()
    milestone = MILESTONE_STATES.get(to_state)
    if milestone:
        case_milestones_total.labels(_MODE, milestone).inc()
    timed = _TIMED_MILESTONES.get(to_state)
    if timed and case.created_at is not None:
        elapsed = (datetime.now(UTC) - case.created_at).total_seconds()
        time_to_milestone_seconds.labels(_MODE, timed).observe(max(0.0, elapsed))


def _status_class(status: int) -> str:
    if status == 429:
        return "429"
    return f"{status // 100}xx"


def instrument_http_client(client: httpx.AsyncClient, provider: str) -> None:
    """Latency/status hooks. Only the provider name, method and status class are recorded;
    URLs (which may carry issue numbers / session ids) never reach a label."""

    async def on_request(request: httpx.Request) -> None:
        request.extensions["metrics_started"] = time.monotonic()

    async def on_response(response: httpx.Response) -> None:
        started = response.request.extensions.get("metrics_started")
        if started is not None:
            provider_request_seconds.labels(provider).observe(time.monotonic() - started)
        provider_requests_total.labels(
            provider, response.request.method, _status_class(response.status_code)
        ).inc()

    client.event_hooks["request"].append(on_request)
    client.event_hooks["response"].append(on_response)


def record_provider_transport_error(provider: str, method: str) -> None:
    provider_requests_total.labels(provider, method, "error").inc()


async def refresh_gauges(session: AsyncSession, capacity: CapacityManager | None) -> None:
    open_rows = await session.execute(
        select(Case.state, func.count()).where(Case.completed_at.is_(None)).group_by(Case.state)
    )
    counts = {CaseState(state): int(n) for state, n in open_rows.all()}
    for state in CaseState:
        queue_depth.labels(_MODE, state.value).set(counts.get(state, 0))
    human_blocked.labels(_MODE).set(sum(counts.get(s, 0) for s in HUMAN_BLOCKED_STATES))

    waiting = await session.scalar(
        select(func.count()).select_from(Case).where(Case.waiting_for.is_not(None))
    )
    waiting_for_capacity.labels(_MODE).set(int(waiting or 0))

    outbox_rows = await session.execute(
        select(NotificationOutbox.status, func.count())
        .where(NotificationOutbox.status != OutboxStatus.SENT)
        .group_by(NotificationOutbox.status)
    )
    backlog = {status: int(n) for status, n in outbox_rows.all()}
    for status in (OutboxStatus.PENDING, OutboxStatus.FAILED):
        outbox_backlog.labels(_MODE, status.value.lower()).set(backlog.get(status, 0))

    if capacity is not None:
        for kind, (used, limit) in (await capacity.utilisation(session)).items():
            active_jobs.labels(_MODE, kind).set(used)
            capacity_limit.labels(_MODE, kind).set(limit)
            capacity_utilisation.labels(_MODE, kind).set(used / limit if limit else 0.0)
