# ruff: noqa: F811
"""Dashboard redesign: functional + accessibility contract of the server-rendered UI.

Everything here goes through the real FastAPI app with the fake adapters; nothing is
mocked at the template layer. Assertions are on HTML structure and preserved strings, not
pixels, so the suite is platform independent.
"""

import re
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from remediator.api.presentation import (
    FUNNEL_STAGES,
    STATE_PRESENTATION,
    TONES,
    build_funnel,
    present,
    present_state,
    stage_for_state,
)
from remediator.fixtures import RemediationFixture
from remediator.lifecycle import TERMINAL_STATES, CaseState
from remediator.models import Attempt, AttemptStatus, Case, Recommendation
from tests.integration.test_phase3_approval import Harness, harness, harness_factory  # noqa: F401
from tests.integration.test_phase4_remediation import (  # noqa: F401
    RemediationHarness,
    _run_fixture,
    number_for,
    rem,
)

POLL_TARGETS = ("overview", "throughput", "active", "completed", "failures", "timeline", "cases")
NEW_PARTIALS = ("kpis", "funnel", "topbar_status")
HX = {"HX-Request": "true"}


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text))


# ------------------------------------------------------------------ presentation mappings


def test_every_case_state_has_a_presentation() -> None:
    assert {CaseState(s) for s in STATE_PRESENTATION} == set(CaseState)
    for state, p in STATE_PRESENTATION.items():
        assert p.tone in TONES, state
        assert p.glyph and p.word and p.label == CaseState(state).value


def test_unknown_values_fall_back_to_neutral() -> None:
    assert present("ci", "nonsense").tone == "neutral"
    assert present("no-such-family", "x").tone == "neutral"
    assert present_state("BOGUS").tone == "neutral"


def test_funnel_stages_partition_non_terminal_states() -> None:
    covered: set[CaseState] = set()
    for members in FUNNEL_STAGES.values():
        assert not covered & members
        covered |= members
    assert covered == set(CaseState) - set(TERMINAL_STATES)
    assert all(stage_for_state(s) is None for s in TERMINAL_STATES)


def test_build_funnel_counts_and_widths() -> None:
    counts = {
        CaseState.RECEIVED.value: 1,
        CaseState.TRIAGING.value: 3,
        CaseState.REMEDIATING.value: 1,
        CaseState.CI_PASSED.value: 5,
        CaseState.CANCELLED.value: 2,
    }
    funnel = build_funnel(counts)
    by_key = {s.key: s for s in funnel.stages}
    assert by_key["intake"].count == 1 and by_key["triage"].count == 3
    assert by_key["triage"].width == 100 and 0 < by_key["intake"].width < 100
    assert by_key["ci"].count == 0 and by_key["ci"].width == 0
    assert funnel.in_flight == 5 and funnel.terminal == 7
    assert {o.state for o in funnel.outcomes} == {s.value for s in TERMINAL_STATES}
    assert {o.state: o.count for o in funnel.outcomes if o.count} == {
        "CI_PASSED": 5,
        "CANCELLED": 2,
    }


# ------------------------------------------------------------------------- dashboard page


@pytest.mark.asyncio
async def test_dashboard_landmarks_polling_and_modes(harness: Harness) -> None:
    await harness.triage(4213)
    page = await harness.client.get("/", headers=harness.operator)
    assert page.status_code == 200
    html = page.text
    assert '<html lang="en">' in html
    assert 'name="viewport"' in html
    assert 'class="skip-link" href="#main"' in html
    assert '<main id="main"' in html and "<header" in html
    assert _count(r"<h1[\s>]", html) == 1
    assert 'href="/static/style.css"' in html and 'src="/static/htmx.min.js"' in html
    for name in POLL_TARGETS:
        assert f'id="poll-{name}"' in html, name
        assert f'hx-get="/partials/{name}"' in html, name
    assert _count(r'hx-trigger="every 3s"', html) >= len(POLL_TARGETS)
    # Provider mode chips: one per adapter, values from settings, never secrets.
    for provider in ("devin", "slack", "github", "probe"):
        assert f'data-provider="{provider}"' in html, provider
    assert 'data-mode="fake"' in html
    assert 'id="rendered-at"' in html and "<time datetime=" in html
    assert 'id="stale-banner"' in html and 'role="status"' in html
    # No CDN / external assets.
    assert not re.search(r'<(link|script)[^>]*(src|href)="https?://', html)


@pytest.mark.asyncio
async def test_partials_render_and_unknown_is_404(harness: Harness) -> None:
    await harness.triage(4213)
    for name in POLL_TARGETS + NEW_PARTIALS:
        response = await harness.client.get(f"/partials/{name}", headers=harness.operator)
        assert response.status_code == 200, name
        assert "<h1" not in response.text, name  # partials never carry a page heading
    assert (await harness.client.get("/partials/nope", headers=harness.operator)).status_code == 404


@pytest.mark.asyncio
async def test_case_filters(harness: Harness) -> None:
    ok = await harness.triage(4213)  # success -> AWAITING_REMEDIATION_APPROVAL
    human = await harness.triage(4212)  # % 3 -> needs_human
    ok_href, human_href = f"/cases/{ok.id}", f"/cases/{human.id}"

    async def rows(**params: str) -> str:
        response = await harness.client.get(
            "/partials/cases", params=params, headers=harness.operator
        )
        assert response.status_code == 200
        return response.text

    both = await rows()
    assert ok_href in both and human_href in both
    assert 'id="case-filters"' in both and "hx-preserve" in both
    only = await rows(q="4213")
    assert ok_href in only and human_href not in only
    assert ok_href in await rows(state=CaseState.AWAITING_REMEDIATION_APPROVAL.value)
    assert human_href not in await rows(state=CaseState.AWAITING_REMEDIATION_APPROVAL.value)
    approval = await rows(phase="approval")
    assert ok_href in approval
    ok_rec = Recommendation((await harness.case(ok.id)).recommendation).value
    assert ok_href in await rows(recommendation=ok_rec)
    other = next(r for r in Recommendation if r.value != ok_rec)
    assert ok_href not in await rows(recommendation=other.value)
    for bad in ({"state": "BOGUS"}, {"phase": "nowhere"}, {"recommendation": "??"}, {"q": "zzz"}):
        text = await rows(**bad)
        assert ok_href not in text and human_href not in text
        assert "No cases match" in text


@pytest.mark.asyncio
async def test_funnel_partial_reflects_state_counts(harness: Harness) -> None:
    await harness.triage(4213)
    text = (await harness.client.get("/partials/funnel", headers=harness.operator)).text
    assert 'data-stage="approval"' in text
    assert re.search(r'data-stage="approval"[^>]*data-count="1"', text)
    assert "Terminal outcomes" in text


@pytest.mark.asyncio
async def test_active_and_attention_partials(rem: RemediationHarness) -> None:
    case = await _run_fixture(rem, RemediationFixture.SUCCESS)
    # The fake Devin finishes within one worker claim, so REMEDIATING is never observable
    # between steps. Rewind the persisted rows to the mid-session snapshot for rendering.
    async with rem.base.factory() as session:
        row = await session.get(Case, case.id)
        assert row is not None
        row.state = CaseState.REMEDIATING
        attempt = (await rem.attempts(case.id))[0]
        live = await session.get(Attempt, attempt.id)
        assert live is not None
        live.status = AttemptStatus.RUNNING
        await session.commit()
    active = (await rem.base.client.get("/partials/active", headers=rem.base.operator)).text
    assert f"/cases/{case.id}" in active
    assert "ACU" in active and 'scope="col"' in active and "<caption" in active
    kpis = (await rem.base.client.get("/partials/kpis", headers=rem.base.operator)).text
    assert 'data-kpi="sessions"' in kpis
    assert re.search(r'data-kpi="sessions"[^>]*data-value="1"', kpis)
    topbar = (await rem.base.client.get("/partials/topbar_status", headers=rem.base.operator)).text
    assert 'id="active-sessions"' in topbar


@pytest.mark.asyncio
async def test_phase5_capacity_wait_and_acu_report_render_only_when_present(
    rem: RemediationHarness,
) -> None:
    case = await _run_fixture(rem, RemediationFixture.SUCCESS)
    active_url, detail_url = "/partials/active", f"/partials/case/{case.id}"
    async with rem.base.factory() as session:
        row = await session.get(Case, case.id)
        assert row is not None
        row.state = CaseState.REMEDIATING
        attempt = (await rem.attempts(case.id))[0]
        live = await session.get(Attempt, attempt.id)
        assert live is not None
        live.status = AttemptStatus.RUNNING
        await session.commit()
    active = (await rem.base.client.get(active_url, headers=rem.base.operator)).text
    detail = (await rem.base.client.get(detail_url, headers=rem.base.operator)).text
    assert "waiting for" not in active and "waiting for" not in detail
    assert "ACU billed" not in active and "billing unavailable" not in detail

    async with rem.base.factory() as session:
        row = await session.get(Case, case.id)
        assert row is not None
        row.waiting_for = "triage"
        row.waiting_since = datetime.now(UTC)
        live = await session.get(Attempt, attempt.id)
        assert live is not None
        live.acu_report_status = "simulated"
        live.acu_reported = 1.5
        await session.commit()
    active = (await rem.base.client.get(active_url, headers=rem.base.operator)).text
    detail = (await rem.base.client.get(detail_url, headers=rem.base.operator)).text
    assert "waiting for triage" in active and "waiting for triage" in detail
    assert "1.5 ACU billed" in active and "1.5 ACU billed" in detail
    assert "simulated" in detail


@pytest.mark.asyncio
async def test_failures_partial_lists_blocked_cases(rem: RemediationHarness) -> None:
    case = await _run_fixture(rem, RemediationFixture.NEEDS_HUMAN)
    assert case.state == CaseState.REMEDIATION_HUMAN_BLOCKED
    text = (await rem.base.client.get("/partials/failures", headers=rem.base.operator)).text
    assert f"/cases/{case.id}" in text
    assert "REMEDIATION_HUMAN_BLOCKED" in text
    assert 'data-tone="warn"' in text or 'data-tone="err"' in text


# ------------------------------------------------------------------------- authentication


@pytest.mark.asyncio
async def test_htmx_auth_expiry_returns_401_with_redirect(harness: Harness) -> None:
    response = await harness.client.get("/partials/kpis", headers=HX)
    assert response.status_code == 401
    assert response.headers.get("HX-Redirect") == "/login"
    # Full-page browser navigation still redirects.
    page = await harness.client.get("/")
    assert page.status_code == 303 and page.headers["location"].startswith("/login")
    # JSON API keeps its 401.
    assert (await harness.client.get("/api/cases/apache/superset/1")).status_code == 401


@pytest.mark.asyncio
async def test_login_page_and_bad_token_render_accessible_error(harness: Harness) -> None:
    page = await harness.client.get("/login")
    assert page.status_code == 200
    assert '<label for="token"' in page.text and 'id="token"' in page.text
    assert _count(r"<h1[\s>]", page.text) == 1
    bad = await harness.client.post(
        "/login", data={"token": "wrong"}, headers={"accept": "text/html"}
    )
    assert bad.status_code == 401
    assert 'role="alert"' in bad.text and "Invalid token" in bad.text
    assert 'aria-describedby="login-error"' in bad.text and 'id="login-error"' in bad.text
    bad_json = await harness.client.post("/login", data={"token": "wrong"})
    assert bad_json.status_code == 401 and "<html" not in bad_json.text


# ------------------------------------------------------------------------- case detail


def _assert_case_shell(html: str, case_id: Any) -> None:
    assert _count(r"<h1[\s>]", html) == 1
    assert '<nav class="breadcrumb" aria-label="Breadcrumb">' in html
    assert 'aria-current="page"' in html
    assert f'hx-get="/partials/case/{case_id}"' in html
    assert "Elapsed in stage:" in html and "Age:" in html
    assert "approve-remediation" not in html
    assert not re.search(r'(src|href)="https?://[^"]*(cdn|unpkg|jsdelivr)', html)


@pytest.mark.asyncio
async def test_case_detail_awaiting_approval(harness: Harness) -> None:
    case, _request, token = await harness.notify_and_approve()
    await harness.drain()
    page = await harness.client.get(f"/cases/{case.id}", headers=harness.operator)
    assert page.status_code == 200
    _assert_case_shell(page.text, case.id)
    assert "Remediation approval" in page.text and "Approval timeline" in page.text
    assert "approval happens in Slack" in page.text
    assert "Scope is bounded." in page.text
    assert token not in page.text
    # Rubric checks are rendered as glyph + word, never as Python booleans.
    assert 'data-check="pass"' in page.text
    assert ">True<" not in page.text and ">False<" not in page.text
    # Status badge: glyph hidden from AT, visible label, sr-only status word.
    badge = re.search(
        r'<span class="badge tone-(\w+)" data-tone="\w+" data-raw="AWAITING_REMEDIATION_APPROVAL">'
        r'<span class="badge-glyph" aria-hidden="true">[^<]+</span>'
        r'<span class="badge-text">AWAITING_REMEDIATION_APPROVAL</span>'
        r'<span class="sr-only"> \(([^)]+)\)</span>',
        page.text,
    )
    assert badge, "state badge markup"
    assert badge.group(1) in TONES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture", "expected_state", "markers"),
    [
        (
            RemediationFixture.SUCCESS,
            CaseState.CI_PASSED,
            ("Remediation (Phase 4)", "Independent probe executions", "ready for human PR review"),
        ),
        (RemediationFixture.NEEDS_HUMAN, CaseState.REMEDIATION_HUMAN_BLOCKED, ("blocked reason",)),
        (RemediationFixture.CI_FAILED, CaseState.CI_FAILED, ("CI", "failure")),
        (RemediationFixture.PR_WRONG_AUTHOR, CaseState.REMEDIATION_FAILED, ("corroboration",)),
        (RemediationFixture.PROBE_HEAD_FAILS, CaseState.REMEDIATION_FAILED, ("HEAD",)),
        (RemediationFixture.MALFORMED_OUTPUT, CaseState.REMEDIATION_FAILED, ("Session",)),
    ],
)
async def test_case_detail_fixture_variants(
    rem: RemediationHarness,
    fixture: RemediationFixture,
    expected_state: CaseState,
    markers: tuple[str, ...],
) -> None:
    case = await _run_fixture(rem, fixture)
    assert case.state == expected_state
    page = await rem.base.client.get(f"/cases/{case.id}", headers=rem.base.operator)
    assert page.status_code == 200
    _assert_case_shell(page.text, case.id)
    assert expected_state.value in page.text
    for marker in markers:
        assert marker in page.text, marker
    partial = await rem.base.client.get(f"/partials/case/{case.id}", headers=rem.base.operator)
    assert partial.status_code == 200 and "<h1" in partial.text and "<html" not in partial.text


@pytest.mark.asyncio
async def test_case_actions_have_consequence_specific_confirmations(
    rem: RemediationHarness,
) -> None:
    case = await rem.approve(number_for(RemediationFixture.PROBE_INFRASTRUCTURE))
    case = await rem.run(case.id)
    assert case.state == CaseState.PROBE_INFRASTRUCTURE_BLOCKED
    page = (await rem.base.client.get(f"/cases/{case.id}", headers=rem.base.operator)).text
    assert f"/operator/cases/{case.id}/retry-probe" in page
    assert f"/operator/cases/{case.id}/cancel" in page
    confirms = re.findall(r'hx-confirm="([^"]+)"', page)
    assert confirms and all(len(c) > 30 for c in confirms)
    assert any("terminated" in c for c in confirms)
    assert 'hx-disabled-elt="this"' in page


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture",
    [RemediationFixture.CI_FAILED, RemediationFixture.PR_WRONG_AUTHOR, RemediationFixture.SUCCESS],
)
async def test_terminal_cases_offer_no_cancel_action(
    rem: RemediationHarness, fixture: RemediationFixture
) -> None:
    case = await _run_fixture(rem, fixture)
    assert CaseState(case.state) in TERMINAL_STATES
    page = (await rem.base.client.get(f"/cases/{case.id}", headers=rem.base.operator)).text
    assert f"/operator/cases/{case.id}/cancel" not in page
    response = await rem.base.client.post(
        f"/operator/cases/{case.id}/cancel", headers=rem.base.operator
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_invalid_transition_message_is_preserved(rem: RemediationHarness) -> None:
    done = await _run_fixture(rem, RemediationFixture.SUCCESS)
    response = await rem.base.client.post(
        f"/operator/cases/{done.id}/retry", headers={**rem.base.operator, **HX}
    )
    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert "cannot retry remediation from CI_PASSED" in response.text


# ------------------------------------------------------------------------- query budget


@pytest.mark.asyncio
async def test_dashboard_query_count_is_bounded(
    harness: Harness, integration_engine: AsyncEngine
) -> None:
    for number in (4213, 4214, 4216):
        await harness.triage(number)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(integration_engine.sync_engine, "before_cursor_execute", record)
    try:
        await harness.client.get("/", headers=harness.operator)
        page_queries = len(statements)
        statements.clear()
        await harness.client.get("/partials/cases", headers=harness.operator)
        partial_queries = len(statements)
    finally:
        event.remove(integration_engine.sync_engine, "before_cursor_execute", record)
    # Case list / lookups are loaded once; templates never trigger per-row queries.
    assert page_queries <= 20, page_queries
    assert partial_queries <= 20, partial_queries
