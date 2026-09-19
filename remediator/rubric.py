import re
from dataclasses import dataclass

from .models import Recommendation


@dataclass(frozen=True)
class IssueSnapshot:
    title: str
    body: str
    labels: list[str]


@dataclass(frozen=True)
class RubricCheck:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class RubricResult:
    recommendation: Recommendation
    checks: list[RubricCheck]


def _has(text: str, *terms: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def evaluate(issue: IssueSnapshot) -> RubricResult:
    text = f"{issue.title}\n{issue.body}"
    labels = {label.lower() for label in issue.labels}
    areas = sum(_has(text, area) for area in ("frontend", "backend", "infra", "database", "api"))
    checks = [
        RubricCheck(
            "scope",
            len(issue.body) <= 12000
            and not _has(text, "refactor everything", "migrate everything")
            and areas <= 2,
            (
                "Scope is bounded."
                if len(issue.body) <= 12000
                and areas <= 2
                and not _has(text, "refactor everything", "migrate everything")
                else ("Issue exceeds the body-length, area-count, or broad-refactor scope limit.")
            ),
        ),
        RubricCheck(
            "objective_verification",
            _has(text, "test", "expected", "actual", "assert", "stack trace"),
            "Includes objective verification details."
            if _has(text, "test", "expected", "actual", "assert", "stack trace")
            else "No test, expected/actual, assertion, or stack trace evidence found.",
        ),
        RubricCheck(
            "reproduction_steps",
            bool(re.search(r"steps?\s+to\s+reproduce|(^|\n)\s*\d+[.)]", text, re.I)),
            "Contains reproducible steps."
            if bool(re.search(r"steps?\s+to\s+reproduce|(^|\n)\s*\d+[.)]", text, re.I))
            else "Reproduction steps are missing.",
        ),
        RubricCheck(
            "acceptance_criteria",
            _has(text, "acceptance criteria", "expected behavior", "acceptance:"),
            "Acceptance criteria are stated."
            if _has(text, "acceptance criteria", "expected behavior", "acceptance:")
            else "Acceptance criteria are missing.",
        ),
        RubricCheck(
            "isolation",
            not labels.intersection({"epic", "discussion", "design", "rfc"}) and areas <= 1,
            "Issue appears isolated."
            if not labels.intersection({"epic", "discussion", "design", "rfc"}) and areas <= 1
            else "Issue is a discussion/design item or spans multiple areas.",
        ),
        RubricCheck(
            "existing_patterns",
            _has(text, "similar", "existing code", "pattern", "like x")
            or bool(labels.intersection({"bug", "good first issue"})),
            "References existing patterns or is a bug/first issue."
            if _has(text, "similar", "existing code", "pattern", "like x")
            or labels.intersection({"bug", "good first issue"})
            else "No existing pattern or related implementation reference found.",
        ),
        RubricCheck(
            "requires_repo_reasoning",
            not _has(text, "architecture", "performance", "breaking change", "security"),
            "Does not require deep repository reasoning."
            if not _has(text, "architecture", "performance", "breaking change", "security")
            else "Requires architecture, performance, security, or breaking-change reasoning.",
        ),
    ]
    names = {check.name: check for check in checks}
    if not names["isolation"].passed or not names["requires_repo_reasoning"].passed:
        recommendation = Recommendation.HUMAN_LED
    elif labels.intersection(
        {"dependencies", "lint", "formatting", "typo", "version-bump"}
    ) or re.search(r"\b(bump|typo|lint)\b", issue.title, re.I):
        recommendation = Recommendation.USE_DETERMINISTIC_AUTOMATION
    elif (
        not names["reproduction_steps"].passed
        or not names["objective_verification"].passed
        or not names["acceptance_criteria"].passed
    ):
        recommendation = Recommendation.NEEDS_SCOPING
    else:
        recommendation = Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE
    return RubricResult(recommendation, checks)
