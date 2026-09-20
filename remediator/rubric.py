"""Zero-ACU eligibility: does the issue carry enough context for bounded, code-aware triage?

The filter deliberately does NOT judge whether a category of work (dependency bump, migration,
architecture, product question) is appropriate for automation. That judgment belongs to the
Devin triage evidence and to the authorized human who approves or rejects in Slack. What the
filter rejects is an issue whose text does not let a triage session begin repository
investigation without first asking "what is the problem?".

Category signals are still computed, but only as an *advisory* `Recommendation` recorded for
the dashboard and for humans; they never gate eligibility.
"""

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
    eligible: bool
    missing: tuple[str, ...]
    checks: list[RubricCheck]
    recommendation: Recommendation
    """Advisory category signal; NEEDS_SCOPING when context is insufficient."""


MISSING_PROBLEM = "Missing concrete problem statement"
MISSING_EXPECTED = "Missing expected outcome"
MISSING_SIGNAL = "Missing reproduction/example/affected-component signal"
MISSING_ACTIONABLE = "Not enough actionable detail to start repository investigation"

MIN_PROBLEM_WORDS = 12
MIN_ACTIONABLE_WORDS = 20
MIN_SENTENCE_WORDS = 5

AREAS = ("frontend", "backend", "infra", "database", "api")

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_URL = re.compile(r"https?://\S+")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKUP = re.compile(r"[*_#>|~-]{1,}")
_SENTENCE_SPLIT = re.compile(r"(?<=[.;!?])\s+|\n+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")

# Present-state / defect vocabulary: something is wrong or must change today.
_PROBLEM = re.compile(
    r"\b(?:fails?|failing|failed|errors?|exceptions?|incorrect(?:ly)?|wrong(?:ly)?|broken|breaks?|"
    r"does ?n[o']t|do ?n[o']t|is ?n[o']t|are ?n[o']t|was ?n[o']t|cannot|can ?n[o']t|unable|"
    r"crash(?:es|ed)?|throws?|missing|unexpected(?:ly)?|instead|currently|today|actual(?:ly)?|"
    r"however|regress(?:ion|ed)?|outdated|out of date|vulnerab\w*|advisor(?:y|ies)|"
    r"multiple heads|conflicts?|deprecated|inconsistent|slow|time[sd]? out|blank|empty|"
    r"undefined|null|nan|renders? as|show\w*|display\w*|returns?|produces?|"
    r"results? in|causes?|leads? to|because|stuck|hangs?|ignor\w+|dropped|duplicated?|"
    r"not\s+\w+|no longer|never|only|but\b|problem|bug|defect|issue is|violat\w+|"
    r"inaccessible|unreadable|blocked|blocks|coerc\w+|treated as|interpreted as)\b",
    re.I,
)
# Desired-state vocabulary: what "done" looks like.
_EXPECTED = re.compile(
    r"\b(?:expected|expect|should|must|so that|desired|want(?:ed)?|goal|acceptance|"
    r"correct(?:ly)?|resolve[sd]?|fix(?:ed|es)?\b|after the (?:fix|change|upgrade)|ought|"
    r"done when|success(?: criteria)?|target|need(?:s)? to (?:be|become|render|return|show|produce|"
    r"upgrade|bump|resolve|have|use|pass|match)|single (?:head|revision)|"
    r"upgrade(?: \w+){0,3} to|bump(?: \w+){0,3} to|remain|preserve[sd]?|continue[sd]? to|"
    r"instead of|rather than|until|passes|pass\b|green)\b",
    re.I,
)

# Investigation signals; any one is enough. Each has a human-readable label for the reason.
_REPRO = re.compile(
    r"steps?\s+to\s+reproduce|\brepro(?:duce[sd]?|duction|ducible)?\b|to trigger|"
    r"(?:^|\n)\s*\d+[.)]\s+\S+.*\n\s*\d+[.)]\s+\S+|when (?:i|you|we|a user|the user)\b|"
    r"open(?:ing)? (?:a|the) (?:chart|dashboard|page|explore)|\brun(?:ning)? `",
    re.I,
)
_CURRENT_VS_EXPECTED = (
    re.compile(r"\b(?:actual|current(?:ly)?|observed|today|renders? as|shows?|got)\b", re.I),
    re.compile(r"\b(?:expected|should|instead|want(?:ed)?|desired)\b", re.I),
)
_ERROR_OR_LOG = re.compile(
    r"\b(?:traceback|stack ?trace|exception|error(?:s|ed)?\b|warning:|failed with|"
    r"log(?:s| output)?:|[A-Z][A-Za-z]+(?:Error|Exception|Warning)\b|HTTP ?[45]\d\d|"
    r"status ?[45]\d\d|exit code \d+)",
    re.I,
)
_SAMPLE_IO = re.compile(
    r"\b(?:input|output|returns?|renders?|payload|response|request body|value)s?\b.{0,80}"
    r"(?:->|→|=>|:|becomes|yields|gives|shows)",
    re.I,
)
_PATH = re.compile(
    r"(?<![\w/])(?:[\w.-]+/)+[\w.-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|sql|ya?ml|json|md|html|css|"
    r"less|scss|toml|ini|cfg|sh|txt)\b|"
    r"\b[\w.-]+\.(?:py|ts|tsx|js|jsx)\b|\b\w+\([^)]{0,40}\)|/api/v\d+/[\w/{}-]+|"
    r"\b(?:superset|superset-frontend|alembic|npm|pytest|jest|vitest|make)\s+[\w./:-]+",
)
_COMPONENT_WORDS = re.compile(
    r"\b(?:chart|dashboard|endpoint|component|module|plugin|command|table|column|filter|"
    r"migration|revision|package|dependency|hook|util(?:ity|s)?|helper|view|route|handler|"
    r"job|worker|scheduler|query|dataset|database connector|sql ?lab|explore)\b\s+"
    r"[`\"']?[A-Za-z_][\w.-]*",
    re.I,
)
_TEST_SIGNAL = re.compile(
    r"\b(?:failing|proposed|new|existing|unit|focused|regression) tests?\b|"
    r"\btests?\b[^.\n]{0,60}(?:\.py|\.ts|\.tsx|\.js|::|pytest|jest|vitest)|"
    r"\b(?:pytest|jest|vitest|npm test|npm run test)\b|\bassert",
    re.I,
)
_REFERENCE_LINK = re.compile(
    r"https?://\S*(?:pull|issues|commit|blob|discussions|releases|advisories|CVE|GHSA)\S*|"
    r"\b(?:CVE|GHSA)-[\w-]+|\b(?:upstream|reference|see also|related|per|as in)\b[^.\n]{0,40}"
    r"(?:https?://|#\d+|PR\s*#?\d+)",
    re.I,
)

SIGNAL_LABELS: tuple[tuple[str, str], ...] = (
    ("reproduction", "reproduction steps"),
    ("current_vs_expected", "current-versus-expected example"),
    ("error_or_log", "error message or log"),
    ("sample_io", "sample input and output"),
    ("component", "affected component, file, endpoint or code path"),
    ("test", "failing or proposed test"),
    ("reference", "link to concrete upstream/reference behavior"),
)

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
_CATEGORY_HUMAN = re.compile(r"\b(?:architecture|breaking change|security|performance)\b", re.I)
_CATEGORY_DETERMINISTIC = re.compile(r"\b(?:bump|typo|lint)\b", re.I)


def _prose(text: str) -> str:
    """Body text with code, URLs, images and markdown decoration removed."""
    text = _FENCE.sub(" ", text)
    text = _INLINE_CODE.sub(r" \1 ", text)
    text = _IMAGE.sub(" ", text)
    text = _URL.sub(" ", text)
    return _MARKUP.sub(" ", text)


def _words(text: str) -> int:
    return len(_WORD.findall(text))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _descriptive(sentence: str, vocabulary: re.Pattern[str]) -> bool:
    return _words(sentence) >= MIN_SENTENCE_WORDS and bool(vocabulary.search(sentence))


def problem_statement(body: str) -> bool:
    """A concrete description of what is wrong or what needs to change.

    Requires real prose (not a bare title, link or one-line request) and at least one full
    sentence describing the present, defective or to-be-changed state.
    """
    prose = _prose(body)
    if _words(prose) < MIN_PROBLEM_WORDS:
        return False
    return any(_descriptive(sentence, _PROBLEM) for sentence in _sentences(prose))


def expected_outcome(body: str) -> bool:
    """Expected behavior, desired result, acceptance criteria or an objectively stated goal."""
    prose = _prose(body)
    return any(_descriptive(sentence, _EXPECTED) for sentence in _sentences(prose))


def investigation_signals(body: str) -> list[str]:
    """Which concrete investigation hooks the issue offers (see SIGNAL_LABELS)."""
    prose = _prose(body)
    found: list[str] = []
    if _REPRO.search(body):
        found.append("reproduction")
    current, expected = _CURRENT_VS_EXPECTED
    if current.search(prose) and expected.search(prose):
        found.append("current_vs_expected")
    if _ERROR_OR_LOG.search(body):
        found.append("error_or_log")
    if _SAMPLE_IO.search(body) or _FENCE.search(body):
        found.append("sample_io")
    if _PATH.search(body) or _COMPONENT_WORDS.search(prose):
        found.append("component")
    if _TEST_SIGNAL.search(body):
        found.append("test")
    if _REFERENCE_LINK.search(body):
        found.append("reference")
    return found


def actionable(body: str) -> bool:
    """Enough substance that triage can begin without asking what the problem is."""
    return _words(_prose(body)) >= MIN_ACTIONABLE_WORDS


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
    """Areas the issue asks to CHANGE, not every area it happens to mention (advisory only)."""
    prose = _FENCE.sub(" ", text)
    prose = _INLINE_CODE.sub(" ", prose)
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
    """True when the issue itself still asks for a product/architecture decision (advisory)."""
    return bool(_UNRESOLVED_DECISION.search(_FENCE.sub(" ", text)))


def advisory_recommendation(issue: IssueSnapshot) -> Recommendation:
    """Category signal for humans and the dashboard. Never gates eligibility."""
    text = f"{issue.title}\n{issue.body}"
    labels = {label.lower() for label in issue.labels}
    if labels.intersection(
        {"dependencies", "lint", "formatting", "typo", "version-bump"}
    ) or _CATEGORY_DETERMINISTIC.search(issue.title):
        return Recommendation.USE_DETERMINISTIC_AUTOMATION
    if (
        labels.intersection({"epic", "discussion", "design", "rfc"})
        or has_unresolved_decision(text)
        or len(change_areas(text)) > 1
        or _CATEGORY_HUMAN.search(_FENCE.sub(" ", text))
    ):
        return Recommendation.HUMAN_LED
    return Recommendation.ELIGIBLE_FOR_DEVIN_TRIAGE


def evaluate(issue: IssueSnapshot) -> RubricResult:
    body = issue.body
    has_problem = problem_statement(body)
    has_expected = expected_outcome(body)
    signals = investigation_signals(body)
    is_actionable = actionable(body) and has_problem and has_expected and bool(signals)
    labels = {label.lower() for label in issue.labels}
    signal_names = {key: label for key, label in SIGNAL_LABELS}
    checks = [
        RubricCheck(
            "problem",
            has_problem,
            "Describes concretely what is wrong or must change."
            if has_problem
            else f"{MISSING_PROBLEM}: body is a title, link or one-line request without a "
            "sentence describing the current defect or required change.",
        ),
        RubricCheck(
            "expected_outcome",
            has_expected,
            "States the expected behavior, desired result or acceptance criteria."
            if has_expected
            else f"{MISSING_EXPECTED}: no sentence states what correct behavior or 'done' is.",
        ),
        RubricCheck(
            "investigation_signal",
            bool(signals),
            "Investigation hooks: " + ", ".join(signal_names[s] for s in signals) + "."
            if signals
            else f"{MISSING_SIGNAL}: no reproduction steps, current-vs-expected example, error, "
            "sample input/output, affected component, test or reference link.",
        ),
        RubricCheck(
            "actionability",
            is_actionable,
            "A code-aware triage session can start repository investigation from this text."
            if is_actionable
            else f"{MISSING_ACTIONABLE}.",
        ),
    ]
    missing: list[str] = []
    if not has_problem:
        missing.append(MISSING_PROBLEM)
    if not has_expected:
        missing.append(MISSING_EXPECTED)
    if not signals:
        missing.append(MISSING_SIGNAL)
    if not missing and not is_actionable:
        missing.append(MISSING_ACTIONABLE)
    eligible = not missing
    advisory = advisory_recommendation(issue) if eligible else Recommendation.NEEDS_SCOPING
    if eligible:
        checks.append(
            RubricCheck(
                "advisory",
                True,
                f"Advisory category signal {advisory.value} (recorded for triage and the "
                "dashboard; does not gate eligibility)."
                + (" Labels: " + ", ".join(sorted(labels)) + "." if labels else ""),
            )
        )
    return RubricResult(eligible, tuple(missing), checks, advisory)
