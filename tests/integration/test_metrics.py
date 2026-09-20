"""Phase 5 metrics: low-cardinality labels, explicit mode, DB-refreshed gauges, distinct
milestones."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_operator import add_case

from remediator import metrics
from remediator.lifecycle import CaseState, transition
from remediator.models import Case, NotificationOutbox, OutboxChannel, OutboxStatus

FORBIDDEN_LABEL_NAMES = {
    "issue",
    "issue_number",
    "session_id",
    "devin_session_id",
    "pr_url",
    "pr_number",
    "user",
    "user_id",
    "case_id",
    "url",
    "path",
    "repository",
}


def test_no_metric_uses_high_cardinality_labels() -> None:
    for collector in list(REGISTRY._collector_to_names):  # noqa: SLF001
        labelnames = getattr(collector, "_labelnames", ())
        assert not (set(labelnames) & FORBIDDEN_LABEL_NAMES), (collector, labelnames)


def test_milestones_are_distinct_and_never_imply_each_other() -> None:
    names = set(metrics.MILESTONE_STATES.values())
    assert len(names) == len(metrics.MILESTONE_STATES)
    assert {
        "triage_session_completed",
        "remediation_session_completed",
        "structured_output_accepted_pr_discovered",
        "pr_validated",
        "ci_passed",
    } <= names
    # A session finishing (OUTPUT_VALIDATING) is not a PR, and a PR is not validation.
    assert (
        metrics.MILESTONE_STATES[CaseState.OUTPUT_VALIDATING]
        != metrics.MILESTONE_STATES[CaseState.PR_DISCOVERED]
    )
    assert CaseState.CI_PASSED in metrics.MILESTONE_STATES
    assert CaseState.REMEDIATING not in metrics.MILESTONE_STATES


def test_mode_defaults_to_simulated_until_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "_MODE", "simulated")
    assert metrics.mode() == "simulated"
    metrics.configure("live")
    assert metrics.mode() == "live"


def _sample(text: str, name: str, **labels: str) -> float | None:
    for line in text.splitlines():
        if not line.startswith(name + "{") and not line.startswith(name + " "):
            continue
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return None


@pytest.mark.asyncio
async def test_metrics_endpoint_refreshes_gauges_and_counts_transitions(
    test_app, integration_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case_id = await add_case(integration_session_factory, 5300, CaseState.PROBE_VALIDATING_HEAD)
    await add_case(integration_session_factory, 5301, CaseState.HUMAN_BLOCKED)
    async with integration_session_factory() as session:
        case = await session.get(Case, case_id)
        assert case is not None
        case.waiting_for = "remediation"
        case.waiting_since = datetime.now(UTC)
        session.add(
            NotificationOutbox(
                case_id=case_id,
                channel=OutboxChannel.SLACK,
                kind="x",
                payload={},
                status=OutboxStatus.PENDING,
            )
        )
        before = metrics.case_milestones_total.labels(metrics.mode(), "pr_validated")._value.get()  # noqa: SLF001
        await transition(session, case, CaseState.PR_VALIDATED, "head probe passed", "worker")
        await session.commit()
    after = metrics.case_milestones_total.labels(metrics.mode(), "pr_validated")._value.get()  # noqa: SLF001
    assert after == before + 1

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app), base_url="http://test"
    ) as client:
        response = await client.get("/metrics", headers={"Authorization": "Bearer operator"})
    assert response.status_code == 200
    body = response.text
    mode = metrics.mode()
    assert _sample(body, "case_queue_depth", mode=mode, state="PR_VALIDATED") == 1
    assert _sample(body, "case_queue_depth", mode=mode, state="HUMAN_BLOCKED") == 1
    assert _sample(body, "cases_human_blocked", mode=mode) == 1
    assert _sample(body, "cases_waiting_for_capacity", mode=mode) == 1
    assert _sample(body, "outbox_backlog", mode=mode, status="pending") == 1
    assert _sample(body, "capacity_limit", mode=mode, kind="remediation") == 1
    assert _sample(body, "active_jobs", mode=mode, kind="probe") == 0
    # Nothing identifying leaks into the exposition.
    assert "5300" not in re.sub(r"\d+\.\d+e[+-]\d+|\b\d+(\.\d+)?$", "", body, flags=re.M)
    assert str(case_id) not in body
