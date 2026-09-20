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


AREAS = ("frontend", "backend", "infra", "database", "api")

_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_HEADING = re.compile(
    r"(?:^|\n)[ \t]*(?:#{1,6}[ \t]+([^\n]+?)|\*\*([^\n*]{1,60})\*\*:?|([A-Za-z][A-Za-z /-]{0,40}):)"
    r"[ \t]*(?=\n|$)"
)
_SCOPE_HEADINGS = re.compile(r"^(?:in[- ])?scope|^affected (?:areas?|components?)$", re.I)
_EXCLUSION_HEADINGS = re.compile(
    r"^(?:non[- ]goals?|out[- ]of[- ]scope|not in scope|unchanged|no changes? to)\b", re.I
)
_NEGATED_SENTENCE = re.compile(
    r"\b(?:no|not|without|unchanged|untouched|stays? as is|n't|does not|must not)\b", re.I
)
_UNRESOLVED_DECISION = re.compile(
    r"\b(?:needs? (?:a )?decision|decision (?:is )?(?:needed|required|pending)|"
    r"(?:need|have) to decide|decide (?:whether|which|between)|should we\b|"
    r"which (?:approach|option|head|revision) (?:should|do we|to)|open question|"
    r"design (?:choice|question|discussion)|proposal|rfc|trade-?offs?)",
    re.I,
)


def _has(text: str, *terms: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def _sections(text: str) -> list[tuple[str, str]]:
    """Split markdown-ish text into (heading, body) pairs; the preamble has heading ''."""
    matches = list(_HEADING.finditer(text))
    sections: list[tuple[str, str]] = []
    cursor = 0
    heading = ""
    for match in matches:
        sections.append((heading, text[cursor : match.start()]))
        heading = next(group for group in match.groups() if group is not None).strip()
        cursor = match.end()
    sections.append((heading, text[cursor:]))
    return sections


def change_areas(text: str) -> set[str]:
    """Areas the issue asks to CHANGE, not every area it happens to mention.

    Code spans/paths are ignored (a file under `superset-frontend/` says nothing about
    scope), an explicit scope section wins when present, exclusion sections (non-goals,
    out of scope) never count, and negated sentences ("no changes to the backend") never
    count either.
    """
    prose = _CODE.sub(" ", text)
    sections = _sections(prose)
    scoped = [body for heading, body in sections if _SCOPE_HEADINGS.search(heading)]
    if scoped:
        candidate = "\n".join(scoped)
    else:
        candidate = "\n".join(
            body for heading, body in sections if not _EXCLUSION_HEADINGS.search(heading)
        )
    sentences = [
        sentence
        for sentence in re.split(r"(?<=[.;!?])\s+|\n+", candidate)
        if not _NEGATED_SENTENCE.search(sentence)
    ]
    kept = " ".join(sentences).lower()
    return {area for area in AREAS if area in kept}


def has_unresolved_decision(text: str) -> bool:
    """True when the issue itself still asks for a product/architecture decision."""
    return bool(_UNRESOLVED_DECISION.search(_CODE.sub(" ", text)))


def evaluate(issue: IssueSnapshot) -> RubricResult:
    text = f"{issue.title}\n{issue.body}"
    labels = {label.lower() for label in issue.labels}
    areas = len(change_areas(text))
    undecided = has_unresolved_decision(text)
    isolated = (
        not labels.intersection({"epic", "discussion", "design", "rfc"})
        and areas <= 1
        and not undecided
    )
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
            isolated,
            "Issue appears isolated."
            if isolated
            else (
                "Issue is a discussion/design item."
                if labels.intersection({"epic", "discussion", "design", "rfc"})
                else "Issue still asks for a product/architecture decision."
                if undecided
                else "Issue asks to change more than one area."
            ),
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
