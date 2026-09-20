import json

from remediator.models import Recommendation
from remediator.rubric import (
    MISSING_EXPECTED,
    MISSING_PROBLEM,
    MISSING_SIGNAL,
    IssueSnapshot,
    evaluate,
)

GOOD_BODY = (
    "Steps to reproduce:\n1. Open a table chart with a temporal column.\n2. Sort by it.\n"
    "Expected behavior: rows are ordered by timestamp. Actual behavior: rows are ordered as "
    "strings, so 10:00 sorts before 9:00. Affected code: `superset-frontend/src/utils/sort.ts`.\n"
    "Acceptance criteria: the column sorts chronologically and the sort unit tests pass."
)


def test_good_candidate() -> None:
    result = evaluate(IssueSnapshot("Fix bug", GOOD_BODY, ["bug"]))
    assert result.eligible
    assert result.missing == ()
    assert result.recommendation == Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE
    assert all(check.reason for check in result.checks)
    assert not any("confidence" in check.name for check in result.checks)


def test_expected_outcome_alone_is_not_enough() -> None:
    result = evaluate(IssueSnapshot("Fix bug", "Expected behavior works.", []))
    assert not result.eligible
    assert MISSING_PROBLEM in result.missing


def test_dependency_label_does_not_decide_eligibility() -> None:
    """A dependency label is advisory: eligibility is decided by context only."""
    thin = evaluate(IssueSnapshot("chore: bump dependency", "", ["dependencies"]))
    assert not thin.eligible
    assert set(thin.missing) == {MISSING_PROBLEM, MISSING_EXPECTED, MISSING_SIGNAL}
    rich = evaluate(
        IssueSnapshot(
            "chore: bump js-yaml from 3.14.1 to 4.1.0",
            "Problem: `superset-frontend/package.json` pins js-yaml 3.14.1, flagged by `npm audit` "
            "(GHSA-8j8c-7jfh-h6hx) and no longer maintained.\n"
            "Expected outcome: js-yaml is upgraded to 4.1.0 and the two call sites in "
            "`superset-frontend/src/utils/yaml.ts` use the 4.x `load` API.\n"
            "Validation: `npm audit --audit-level=high` reports no js-yaml advisory and "
            "`npm run test -- yaml` passes.",
            ["dependencies"],
        )
    )
    assert rich.eligible
    assert rich.recommendation == Recommendation.USE_DETERMINISTIC_AUTOMATION


def test_architecture_category_does_not_decide_eligibility() -> None:
    thin = evaluate(
        IssueSnapshot(
            "Architecture change",
            "This is a security breaking change across frontend and backend.",
            [],
        )
    )
    assert not thin.eligible
    assert MISSING_EXPECTED in thin.missing
    rich = evaluate(
        IssueSnapshot(
            "Chart data API rebuilds every query on each dashboard filter change",
            "Problem: each native-filter change issues one `POST /api/v1/chart/data` per chart "
            "and `superset/common/query_context_processor.py` rebuilds the SQLAlchemy query from "
            "scratch, so a 30-chart dashboard takes 4 to 6 seconds before the first repaint.\n"
            "Expected outcome: a filter change repaints the first chart in under a second and "
            "the `/api/v1/chart/data` responses stay byte-identical for the same inputs.\n"
            "Acceptance criteria: the `tests/integration_tests/charts/data` suite passes "
            "unchanged. This is an architecture change and may be a breaking change for clients.",
            [],
        )
    )
    assert rich.eligible
    assert rich.recommendation == Recommendation.HUMAN_LED


def test_no_numeric_confidence() -> None:
    serialized = json.dumps(
        evaluate(IssueSnapshot("Fix bug", GOOD_BODY, ["bug"])).__dict__, default=str
    )
    assert "confidence" not in serialized.lower()
    assert "score" not in serialized.lower()
    assert "%" not in serialized
