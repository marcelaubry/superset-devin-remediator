"""Zero-ACU eligibility is a context-completeness check, not a category judgment.

The deterministic rubric answers one question: "is there enough context for bounded
code-aware triage?" It never answers "should this work be autonomously remediated?" - that
belongs to the Devin triage evidence plus an authorized human's Slack decision. Dependency
bumps, migration-graph problems, accessibility defects and architecture-level issues are all
admitted when they carry a concrete problem, an expected outcome and an investigation signal;
category signals are recorded only as an advisory `Recommendation`.
"""

import json
from pathlib import Path

import pytest

from remediator.models import Recommendation
from remediator.rubric import (
    MISSING_EXPECTED,
    MISSING_PROBLEM,
    MISSING_SIGNAL,
    IssueSnapshot,
    RubricResult,
    change_areas,
    evaluate,
    has_unresolved_decision,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/github/issue_digit_only_temporal.json"


def _snapshot(fixture: Path = FIXTURE) -> IssueSnapshot:
    issue = json.loads(fixture.read_text())["issue"]
    return IssueSnapshot(
        issue["title"], issue["body"], [label["name"] for label in issue["labels"]]
    )


def _check(result: RubricResult, name: str):  # type: ignore[no-untyped-def]
    return next(check for check in result.checks if check.name == name)


def _serialized(result: RubricResult) -> str:
    return json.dumps(result.__dict__, default=str).lower()


# --------------------------------------------------------------------------- admission


def test_digit_only_temporal_issue_is_eligible_for_devin_triage() -> None:
    result = evaluate(_snapshot())
    assert result.eligible
    assert result.missing == ()
    assert result.recommendation == Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE
    assert all(check.passed for check in result.checks), [
        (check.name, check.reason) for check in result.checks if not check.passed
    ]
    # The rubric can only admit to triage; it never emits a remediation verdict.
    assert "remediation" not in _serialized(result)


def test_digit_only_temporal_issue_mentions_backend_only_as_context() -> None:
    issue = _snapshot()
    text = f"{issue.title}\n{issue.body}"
    assert "backend" in text.lower()
    assert change_areas(text) == {"frontend"}
    assert not has_unresolved_decision(text)


DEPENDENCY_BUMP = (
    "chore(deps): bump js-yaml from 3.14.1 to 4.1.0 in superset-frontend",
    "Problem: `superset-frontend/package.json` pins `js-yaml@3.14.1`, which `npm audit` flags "
    "for prototype pollution (GHSA-8j8c-7jfh-h6hx); the 3.x line is unmaintained.\n\n"
    "Expected outcome: `js-yaml` is upgraded to `4.1.0` in `package.json` and "
    "`package-lock.json`, and the call sites in `superset-frontend/src/utils/yaml.ts` use the "
    "4.x `load`/`dump` API.\n\n"
    "Validation: `npm audit --audit-level=high` reports no js-yaml advisory and "
    "`npm run test -- yaml` passes.\n"
    "Reference: https://github.com/nodeca/js-yaml/blob/master/CHANGELOG.md#400",
    ["dependencies"],
    Recommendation.USE_DETERMINISTIC_AUTOMATION,
)
MULTIPLE_MIGRATION_HEADS = (
    "Alembic reports multiple heads after merging two migration PRs",
    "Steps to reproduce:\n1. Check out `master` after PR #29811 and PR #29840 merged.\n"
    "2. Run `superset db upgrade`.\n\n"
    "Actual behavior: the command aborts with "
    "`alembic.util.exc.CommandError: Multiple head revisions are present; please specify a "
    "specific target revision, '<branchname>@head' to narrow to a specific head, or 'heads' "
    "for all heads`.\n"
    "Expected behavior: `superset db upgrade` completes and `superset db heads` prints a "
    "single revision.\n\n"
    "Affected code: `superset/migrations/versions/` (the two new revisions both declare "
    "`down_revision = 'a1b2c3d4e5f6'`).\n"
    "Acceptance criteria: `superset db heads` prints one head and `tests/integration_tests/"
    "migrations` passes.",
    ["bug"],
    Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE,
)
ACCESSIBILITY_DEFECT = (
    "Dashboard filter bar toggle has no accessible name",
    "Steps to reproduce:\n1. Open any dashboard with native filters.\n"
    "2. Tab to the collapse button of the filter bar.\n"
    "3. Listen with VoiceOver or NVDA.\n\n"
    "Actual behavior: the screen reader announces only `button`; the icon-only control in "
    "`superset-frontend/src/dashboard/components/nativeFilters/FilterBar/` has no `aria-label` "
    "and no visible text.\n"
    "Expected behavior: the control is announced as `Collapse filter bar` / `Expand filter bar` "
    "depending on its state.\n\n"
    "Acceptance criteria: the button exposes an accessible name in both states, "
    "`axe-core` reports no `button-name` violation on the dashboard page, and a focused "
    "jest test asserts the label.",
    ["accessibility"],
    Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE,
)
ARCHITECTURE_ISSUE = (
    "Chart data API recomputes the full query for every dashboard filter change",
    "Problem: every native-filter change issues one `POST /api/v1/chart/data` per chart and "
    "each request rebuilds the SQLAlchemy query from scratch in "
    "`superset/common/query_context_processor.py`. On a 30-chart dashboard a single filter "
    "click causes 30 full query builds and currently takes 4 to 6 seconds before the first "
    "repaint.\n\n"
    "Expected outcome: a filter change on the same dashboard repaints the first chart in "
    "under a second because query construction for unchanged form data is reused, and "
    "`/api/v1/chart/data` responses stay byte-identical for the same inputs.\n\n"
    "Evidence: `get_df_payload` is 70% of request time in a `py-spy` profile taken while "
    "clicking a filter.\n"
    "Acceptance criteria: the `tests/integration_tests/charts/data` suite passes unchanged. "
    "This is an architecture change to the query context layer and may be a breaking change "
    "for API clients.",
    [],
    Recommendation.HUMAN_LED,
)


@pytest.mark.parametrize(
    ("title", "body", "labels", "advisory"),
    [DEPENDENCY_BUMP, MULTIPLE_MIGRATION_HEADS, ACCESSIBILITY_DEFECT, ARCHITECTURE_ISSUE],
    ids=["dependency-bump", "alembic-multiple-heads", "accessibility-defect", "architecture"],
)
def test_context_rich_issues_reach_devin_triage_regardless_of_category(
    title: str, body: str, labels: list[str], advisory: Recommendation
) -> None:
    result = evaluate(IssueSnapshot(title, body, labels))
    assert result.eligible, result.missing
    assert result.missing == ()
    assert all(check.passed for check in result.checks)
    # Category is recorded as advice for the triage prompt and dashboard, never as a gate.
    assert result.recommendation == advisory
    assert "remediation" not in _serialized(result)


def test_advisory_is_recorded_as_a_passing_check_not_a_rejection() -> None:
    result = evaluate(IssueSnapshot(*ARCHITECTURE_ISSUE[:3]))
    advisory = _check(result, "advisory")
    assert advisory.passed
    assert "HUMAN_LED" in advisory.reason
    assert "does not gate eligibility" in advisory.reason


def test_no_exact_file_or_fix_is_required() -> None:
    body = (
        "When I open a pivot table with a `month` time grain, every column header shows "
        "`Invalid date` while the underlying data is correct in the table view.\n"
        "Expected: each header shows the month, for example `2024-03`.\n"
        "The console logs `RangeError: Invalid time value` when the chart renders."
    )
    result = evaluate(IssueSnapshot("Pivot table headers show Invalid date", body, ["bug"]))
    assert result.eligible, result.missing


# --------------------------------------------------------------------------- rejection


@pytest.mark.parametrize(
    ("title", "body", "expected_missing"),
    [
        ("Chart is broken", "", {MISSING_PROBLEM, MISSING_EXPECTED, MISSING_SIGNAL}),
        (
            "Table chart sorts timestamps as strings",
            "   \n",
            {MISSING_PROBLEM, MISSING_EXPECTED, MISSING_SIGNAL},
        ),
        ("Fix the chart", "Please fix this", {MISSING_PROBLEM, MISSING_EXPECTED, MISSING_SIGNAL}),
        (
            "See linked issue",
            "https://github.com/apache/superset/issues/12345",
            {MISSING_PROBLEM, MISSING_EXPECTED},
        ),
        (
            "Temporal formatter produces 1970 dates",
            "When I open a table chart with an integer column the header renders as `7` and "
            "the cell renders as `202609`, which is not the value stored in the database. The "
            "values come from `superset-frontend/src/utils/dates.ts` on the current main branch.",
            {MISSING_EXPECTED},
        ),
        (
            "Table chart dates",
            "Expected behavior: the table chart header reads a seven-day duration and the year "
            "2024 for the corresponding temporal inputs. Acceptance criteria: header and cells "
            "read correctly in `superset-frontend/src/utils/dates.ts`.",
            {MISSING_PROBLEM},
        ),
        (
            "Dashboards feel slow",
            "Dashboards have been feeling slow for our users lately and it is getting worse. "
            "We would like the dashboards to feel fast again so that people are happy with the "
            "product and stop complaining about the experience in general.",
            {MISSING_SIGNAL},
        ),
    ],
    ids=[
        "empty-body",
        "title-only",
        "please-fix-this",
        "link-without-explanation",
        "problem-without-expected-outcome",
        "expected-outcome-without-problem",
        "generic-request-without-signal",
    ],
)
def test_insufficient_context_is_rejected_with_explicit_missing_elements(
    title: str, body: str, expected_missing: set[str]
) -> None:
    result = evaluate(IssueSnapshot(title, body, ["bug"]))
    assert not result.eligible
    assert set(result.missing) == expected_missing, result.missing
    # Rejection is explained by what is missing, never by a category label.
    assert not any(
        label in reason for reason in result.missing for label in ("HUMAN_LED", "NEEDS_SCOPING")
    )
    assert [check.name for check in result.checks if not check.passed]


def test_missing_reasons_are_the_failed_check_reasons() -> None:
    result = evaluate(IssueSnapshot("Fix", "Please fix this", []))
    failed = [check.reason for check in result.checks if not check.passed]
    for element in result.missing:
        assert any(reason.startswith(element) for reason in failed), element
