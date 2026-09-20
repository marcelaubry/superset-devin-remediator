"""Zero-ACU eligibility filter: reject obviously unsuitable work, admit focused bugs.

The deterministic rubric must never duplicate Devin's code-aware triage judgment. A focused
bug with reproduction, enumerated current/expected values, objective acceptance criteria,
bounded code scope and a deterministic validation path is *eligible for triage*; only the
Devin triage session may later call it a remediation candidate. Dependency bumps, migration
graph decisions, architecture/breaking-change proposals and unscoped reports stay rejected
before any session is created.
"""

import json
from pathlib import Path

import pytest

from remediator.models import Recommendation
from remediator.rubric import IssueSnapshot, change_areas, evaluate, has_unresolved_decision

FIXTURE = Path(__file__).parents[1] / "fixtures/github/issue_digit_only_temporal.json"


def _snapshot(fixture: Path = FIXTURE) -> IssueSnapshot:
    issue = json.loads(fixture.read_text())["issue"]
    return IssueSnapshot(
        issue["title"], issue["body"], [label["name"] for label in issue["labels"]]
    )


def _check(result, name: str):  # type: ignore[no-untyped-def]
    return next(check for check in result.checks if check.name == name)


def test_digit_only_temporal_issue_is_eligible_for_devin_triage() -> None:
    issue = _snapshot()
    result = evaluate(issue)
    assert result.recommendation == Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE
    assert all(check.passed for check in result.checks), [
        (check.name, check.reason) for check in result.checks if not check.passed
    ]
    # The rubric can only admit to triage; it never emits a remediation verdict.
    assert "remediation" not in json.dumps(result.__dict__, default=str).lower()


def test_digit_only_temporal_issue_mentions_backend_only_as_context() -> None:
    issue = _snapshot()
    text = f"{issue.title}\n{issue.body}"
    assert "backend" in text.lower()  # the old substring rule counted this as a second area
    assert change_areas(text) == {"frontend"}
    assert not has_unresolved_decision(text)


def test_area_mentions_in_code_negation_and_non_goals_do_not_count() -> None:
    body = (
        "Steps to reproduce:\n1. Run `superset-frontend/x.ts`.\n"
        "Expected behavior: value is returned unchanged. Actual: 1970 date.\n"
        "This is the payload the backend produces for an integer column.\n"
        "## Scope\nFrontend only: one utility and its tests.\n"
        "## Non-goals\nNo database, API or infra changes.\n"
        "Acceptance criteria: unit tests pass."
    )
    assert change_areas(body) == {"frontend"}
    assert evaluate(IssueSnapshot("Fix formatting", body, ["bug"])).recommendation == (
        Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE
    )


def test_comparable_focused_bug_is_eligible() -> None:
    body = (
        "## Problem\n`parseRange('1-3')` returns [1, 2] instead of [1, 2, 3].\n"
        "## Reproduction\n1. `npx tsx probe.ts` with inputs '1-3', '5', '0-0'.\n"
        "## Current behavior\n'1-3' -> [1, 2]; '5' -> []; '0-0' -> [].\n"
        "## Expected behavior\n'1-3' -> [1, 2, 3]; '5' -> [5]; '0-0' -> [0]. "
        "The existing `parseList` helper is the pattern to follow.\n"
        "## Acceptance criteria\n- [ ] `parseRange` unit tests for the three inputs pass.\n"
        "## Scope\nBackend only: `utils/range.py` and its test module.\n"
        "## Non-goals\nNo changes to the frontend or the API contract.\n"
    )
    result = evaluate(IssueSnapshot("fix(range): inclusive upper bound", body, ["bug"]))
    assert result.recommendation == Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE


def test_issue_that_actually_changes_two_areas_stays_human_led() -> None:
    body = (
        "Steps to reproduce:\n1. Open a chart.\nExpected behavior: it renders. Actual: blank.\n"
        "Fix requires a new backend endpoint and the matching frontend fetch.\n"
        "Acceptance criteria: chart renders."
    )
    result = evaluate(IssueSnapshot("Chart blank", body, ["bug"]))
    assert result.recommendation == Recommendation.HUMAN_LED
    assert change_areas(body) == {"backend", "frontend"}


@pytest.mark.parametrize(
    "title,body,labels,expected",
    [
        (
            "chore(deps): bump js-yaml from 3.14.1 to 4.1.0",
            "Steps to reproduce:\n1. Run npm audit.\nExpected: no advisory. Actual: advisory.\n"
            "Acceptance criteria: lockfile updated and tests pass.",
            ["dependencies"],
            Recommendation.USE_DETERMINISTIC_AUTOMATION,
        ),
        (
            "Alembic reports multiple heads after merging two migration PRs",
            "Steps to reproduce:\n1. Run `superset db upgrade`.\n"
            "Expected behavior: a single head. Actual: 'Multiple head revisions are present'.\n"
            "We need to decide which revision becomes the merge parent and whether to reorder "
            "the down_revision chain. Acceptance criteria: single head.",
            ["bug"],
            Recommendation.HUMAN_LED,
        ),
        (
            "Proposal: split the chart data API into a streaming service",
            "Steps to reproduce:\n1. Load a large chart.\nExpected: fast. Actual: slow.\n"
            "This architecture proposal is a breaking change for API clients. "
            "Acceptance criteria: design approved.",
            [],
            Recommendation.HUMAN_LED,
        ),
        (
            "Breaking API design: rename every /api/v1 query parameter",
            "Steps to reproduce:\n1. Call the API.\nExpected behavior: consistent names. "
            "Actual: mixed. Should we version the API or break existing clients? "
            "Acceptance criteria: agreed naming.",
            [],
            Recommendation.HUMAN_LED,
        ),
        (
            "Chart is blank sometimes",
            "Expected behavior: chart renders. Actual behavior: blank. Acceptance criteria: fixed.",
            ["bug"],
            Recommendation.NEEDS_SCOPING,
        ),
        (
            "Formatter returns 1970 dates",
            "Steps to reproduce:\n1. Format '7'.\n2. Observe output.\nIt looks wrong to me.",
            ["bug"],
            Recommendation.NEEDS_SCOPING,
        ),
    ],
    ids=[
        "dependency-bump",
        "alembic-multiple-heads-decision",
        "architecture-breaking-proposal",
        "breaking-api-design-question",
        "missing-reproduction",
        "missing-acceptance-criteria",
    ],
)
def test_regression_matrix_stays_rejected_before_devin(
    title: str, body: str, labels: list[str], expected: Recommendation
) -> None:
    result = evaluate(IssueSnapshot(title, body, labels))
    assert result.recommendation == expected
    assert result.recommendation != Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE


def test_isolation_reason_names_the_failing_condition() -> None:
    undecided = evaluate(
        IssueSnapshot("Fix", "Steps to reproduce:\n1. x\nShould we drop the column?", ["bug"])
    )
    assert "decision" in _check(undecided, "isolation").reason
    multi = evaluate(IssueSnapshot("Fix", "Change the backend and the frontend.", ["bug"]))
    assert "more than one area" in _check(multi, "isolation").reason
    labelled = evaluate(IssueSnapshot("Fix", "Steps to reproduce:\n1. x", ["rfc"]))
    assert "discussion/design" in _check(labelled, "isolation").reason
