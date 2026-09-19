import json

from remediator.models import Recommendation
from remediator.rubric import IssueSnapshot, evaluate


def test_good_candidate() -> None:
    body = (
        "Steps to reproduce:\n1. Run it.\nExpected behavior works. "
        "Actual behavior fails. Acceptance criteria: fixed. Similar existing code."
    )
    result = evaluate(IssueSnapshot("Fix bug", body, ["bug"]))
    assert result.recommendation == Recommendation.GOOD_CANDIDATE
    assert all(check.reason for check in result.checks)
    assert not any("confidence" in check.name for check in result.checks)


def test_needs_scoping() -> None:
    assert (
        evaluate(IssueSnapshot("Fix bug", "Expected behavior works.", [])).recommendation
        == Recommendation.NEEDS_SCOPING
    )


def test_deterministic() -> None:
    assert (
        evaluate(IssueSnapshot("chore: bump dependency", "", ["dependencies"])).recommendation
        == Recommendation.USE_DETERMINISTIC_AUTOMATION
    )


def test_human_led() -> None:
    assert (
        evaluate(
            IssueSnapshot(
                "Architecture change",
                "This is a security breaking change across frontend and backend.",
                [],
            )
        ).recommendation
        == Recommendation.HUMAN_LED
    )


def test_no_numeric_confidence() -> None:
    body = (
        "Steps to reproduce:\n1. Run it.\nExpected behavior works. "
        "Actual behavior fails. Acceptance criteria: fixed. Similar existing code."
    )
    serialized = json.dumps(evaluate(IssueSnapshot("Fix bug", body, ["bug"])).__dict__, default=str)
    assert "confidence" not in serialized.lower()
    assert "score" not in serialized.lower()
    assert "%" not in serialized
