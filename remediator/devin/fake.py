"""Deterministic in-memory Devin client that speaks the v3 status vocabulary.

Scenarios are selected per issue number so simulation fixtures and tests can
exercise every worker path without network access or ACU spend.
"""

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..fixtures import RemediationFixture, fake_head_sha, fake_pr_number, remediation_fixture
from .client import (
    ConsumptionReport,
    CreateSessionRequest,
    DevinApiError,
    DevinSessionNotFound,
    DevinTransportError,
    SessionPullRequest,
    SessionSnapshot,
)
from .remediation import REMEDIATION_SCHEMA_VERSION
from .tags import tag_value
from .triage import TRIAGE_SCHEMA_VERSION

_PROBE_ID_RE = re.compile(r"^- identifier: `([^`]+)`$", re.MULTILINE)
_PROBE_HASH_RE = re.compile(r"^- script sha256: `([0-9a-f]{64})`$", re.MULTILINE)
_BRANCH_PREFIX_RE = re.compile(r"named `([^`<]+)<short-unique-slug>`")


class FakeScenario(StrEnum):
    SUCCESS = "success"
    NEEDS_HUMAN = "needs_human"
    DETERMINISTIC_AUTOMATION = "deterministic_automation"
    NO_CHANGE_NEEDED = "no_change_needed"
    ERROR = "error"
    WAITING_FOR_HUMAN = "waiting_for_human"
    # An interactive session that completed its task, submitted structured output and then
    # went idle ("Devin is awaiting instructions"): status running/waiting_for_user *with*
    # structured output attached. Not a human block.
    IDLE_WITH_OUTPUT = "idle_with_output"
    IDLE_WITH_NEEDS_HUMAN_OUTPUT = "idle_with_needs_human_output"
    IDLE_WITH_MALFORMED_OUTPUT = "idle_with_malformed_output"
    MALFORMED_OUTPUT = "malformed_output"
    MISSING_OUTPUT = "missing_output"
    UNCERTAIN_CREATE = "uncertain_create"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    UNKNOWN_STATUS = "unknown_status"
    CREATE_REJECTED = "create_rejected"


# Fixture issue numbers used by scripts/simulate.py and the integration tests.
FIXTURE_SCENARIOS: dict[int, FakeScenario] = {
    4611: FakeScenario.MALFORMED_OUTPUT,
    4622: FakeScenario.UNCERTAIN_CREATE,
    4633: FakeScenario.QUOTA,
    4644: FakeScenario.TIMEOUT,
    4655: FakeScenario.MISSING_OUTPUT,
    4666: FakeScenario.UNKNOWN_STATUS,
    4677: FakeScenario.CREATE_REJECTED,
    4811: FakeScenario.IDLE_WITH_OUTPUT,
    4822: FakeScenario.IDLE_WITH_MALFORMED_OUTPUT,
}


# Remediation-kind sessions reuse the generic session scenarios for the failure modes that
# happen *before* structured output matters; everything else is `SUCCESS` and the
# remediation fixture decides the output shape.
_REMEDIATION_SESSION_SCENARIOS: dict[RemediationFixture, FakeScenario] = {
    RemediationFixture.UNCERTAIN_CREATE: FakeScenario.UNCERTAIN_CREATE,
    RemediationFixture.SESSION_TIMEOUT: FakeScenario.TIMEOUT,
    RemediationFixture.MALFORMED_OUTPUT: FakeScenario.MALFORMED_OUTPUT,
}


def remediation_scenario_for(issue_number: int) -> FakeScenario:
    return _REMEDIATION_SESSION_SCENARIOS.get(
        remediation_fixture(issue_number), FakeScenario.SUCCESS
    )


def scenario_for(
    issue_number: int, overrides: dict[int, FakeScenario] | None = None, kind: str = "TRIAGE"
) -> FakeScenario:
    if overrides and issue_number in overrides:
        return overrides[issue_number]
    if kind == "REMEDIATION":
        return remediation_scenario_for(issue_number)
    if issue_number in FIXTURE_SCENARIOS:
        return FIXTURE_SCENARIOS[issue_number]
    if issue_number % 5 == 0:
        return FakeScenario.ERROR
    if issue_number % 7 == 0:
        return FakeScenario.WAITING_FOR_HUMAN
    if issue_number % 3 == 0:
        return FakeScenario.NEEDS_HUMAN
    return FakeScenario.SUCCESS


def branch_for(issue_number: int, prefix: str = "devin/") -> str:
    fixture = remediation_fixture(issue_number)
    if fixture == RemediationFixture.PR_WRONG_BRANCH_PREFIX:
        prefix = "fix/"
    return f"{prefix}fix-issue-{issue_number}"


def fake_pr_url(repository: str, issue_number: int) -> str:
    return f"https://github.com/{repository}/pull/{fake_pr_number(issue_number)}"


def sample_remediation_output(
    issue_number: int,
    repository: str,
    base_sha: str,
    probe_identifier: str,
    probe_hash: str,
    branch_prefix: str = "devin/",
) -> dict[str, Any]:
    fixture = remediation_fixture(issue_number)
    pr_repository = repository
    if fixture == RemediationFixture.PR_WRONG_REPOSITORY:
        pr_repository = "someone-else/superset"
    outcome = "pr_created"
    blocking: list[str] = []
    if fixture == RemediationFixture.NO_CHANGE_NEEDED:
        outcome = "no_change_needed"
    elif fixture == RemediationFixture.NEEDS_HUMAN:
        outcome = "needs_human"
        blocking = ["Which of the two documented behaviours is intended?"]
    elif fixture == RemediationFixture.OUTCOME_FAILED:
        outcome = "failed"
    created = outcome == "pr_created"
    pr_url = fake_pr_url(pr_repository, issue_number) if created else None
    if fixture == RemediationFixture.CONTRADICTORY_PR_URL:
        pr_url = fake_pr_url(repository, issue_number + 1)
    head_sha = fake_head_sha(issue_number) if created else None
    if fixture == RemediationFixture.CONTRADICTORY_HEAD_SHA and head_sha is not None:
        head_sha = fake_head_sha(issue_number + 1)
    return {
        "schema_version": REMEDIATION_SCHEMA_VERSION,
        "outcome": outcome,
        "summary": f"simulated remediation of {repository}#{issue_number}: {outcome}",
        "base_sha": base_sha,
        "head_sha": head_sha,
        "branch": branch_for(issue_number, branch_prefix) if created else None,
        "pr_url": pr_url,
        "issue_reference": f"{repository}#{issue_number}",
        "changed_files": (
            ["superset/views/core.py", "tests/unit_tests/views/test_core.py"] if created else []
        ),
        "commits": [head_sha] if head_sha else [],
        "tests_run": ["pytest tests/unit_tests/views/test_core.py -q"] if created else [],
        "probe_identifier": probe_identifier,
        "probe_hash": probe_hash,
        "risks": ["simulated: low"],
        "blocking_questions": blocking,
    }


def sample_triage_output(
    issue_number: int, repository: str, outcome: str = "remediation_candidate"
) -> dict[str, Any]:
    candidate = outcome == "remediation_candidate"
    summaries = {
        "remediation_candidate": "bounded fix identified",
        "needs_human": "requires product decision",
        "no_change_needed": "behaviour is already correct at base",
        "deterministic_automation": "a scripted dependency bump should handle this",
        "invalid_issue": "not actionable in this repository",
    }
    return {
        "schema_version": TRIAGE_SCHEMA_VERSION,
        "outcome": outcome,
        "reproducible": candidate,
        "summary": (
            f"simulated triage of {repository}#{issue_number}: "
            f"{summaries.get(outcome, 'requires product decision')}"
        ),
        "severity": "medium",
        "priority": "p2",
        "confidence": 0.82 if candidate else 0.4,
        "evidence": ["simulated: reproduced with focused test"],
        "affected_files": ["superset/views/core.py"] if candidate else [],
        "acceptance_criteria": ["focused test passes"] if candidate else [],
        "probe": {
            "command": "pytest tests/unit_tests/views/test_core.py -q" if candidate else "",
            "expected_base_exit_code": 1 if candidate else 0,
        },
        "focused_tests": ["tests/unit_tests/views/test_core.py"] if candidate else [],
        "scope": "single module" if candidate else "needs owner input",
        "risk": {"level": "low", "notes": "simulated"},
        "blocking_questions": [] if candidate else ["Which behaviour is intended?"],
    }


@dataclass
class _FakeSession:
    session_id: str
    request: CreateSessionRequest
    issue_number: int
    kind: str
    scenario: FakeScenario
    polls: int = 0
    terminated: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


class FakeDevinClient:
    mode = "fake"

    def __init__(
        self,
        scenarios: dict[int, FakeScenario] | None = None,
        never_finish_issues: set[int] | None = None,
        polls_until_finish: int = 3,
        fail_terminate: bool = False,
    ) -> None:
        self._scenarios = dict(scenarios or {})
        for issue in never_finish_issues or set():
            self._scenarios[issue] = FakeScenario.TIMEOUT
        self._polls_until_finish = polls_until_finish
        self.fail_terminate = fail_terminate
        self._sessions: dict[str, _FakeSession] = {}
        self._by_operation: dict[str, str] = {}
        self._uncertain_seen: set[str] = set()
        self.create_calls = 0
        self.terminate_calls: list[str] = []

    def _url(self, session_id: str) -> str:
        return f"https://app.devin.ai/sessions/{session_id}"

    def _register(self, request: CreateSessionRequest) -> _FakeSession:
        issue_number = int(tag_value(request.tags, "issue:") or 0)
        kind = tag_value(request.tags, "kind:") or "TRIAGE"
        digest = hashlib.sha256(request.operation_key.encode()).hexdigest()[:12]
        session_id = f"fake-{kind.lower()}-{issue_number}-{digest}"
        fake = _FakeSession(
            session_id=session_id,
            request=request,
            issue_number=issue_number,
            kind=kind,
            scenario=scenario_for(issue_number, self._scenarios, kind),
            tags=tuple(request.all_tags()),
        )
        self._sessions[session_id] = fake
        self._by_operation[request.operation_key] = session_id
        return fake

    async def create_session(self, request: CreateSessionRequest) -> SessionSnapshot:
        self.create_calls += 1
        issue_number = int(tag_value(request.tags, "issue:") or 0)
        kind = tag_value(request.tags, "kind:") or "TRIAGE"
        scenario = scenario_for(issue_number, self._scenarios, kind)
        if scenario == FakeScenario.CREATE_REJECTED:
            raise DevinApiError(422, "simulated: structured_output_schema rejected")
        if request.operation_key in self._by_operation:
            raise DevinApiError(409, "simulated: duplicate operation tag")
        fake = self._register(request)
        if (
            scenario == FakeScenario.UNCERTAIN_CREATE
            and request.operation_key not in self._uncertain_seen
        ):
            self._uncertain_seen.add(request.operation_key)
            raise DevinTransportError("simulated: connection reset after POST was sent")
        return self._snapshot(fake, "new", None)

    async def find_sessions_by_tag(self, tag: str) -> list[SessionSnapshot]:
        return [
            self._snapshot(fake, *self._state(fake, advance=False))
            for fake in self._sessions.values()
            if tag in fake.tags
        ]

    async def get_session(self, session_id: str) -> SessionSnapshot:
        fake = self._sessions.get(session_id)
        if fake is None:
            raise DevinSessionNotFound(session_id)
        return self._snapshot(fake, *self._state(fake, advance=True))

    async def terminate_session(self, session_id: str) -> SessionSnapshot | None:
        self.terminate_calls.append(session_id)
        if self.fail_terminate:
            raise DevinTransportError("simulated: DELETE timed out")
        fake = self._sessions.get(session_id)
        if fake is None:
            raise DevinSessionNotFound(session_id)
        fake.terminated = True
        return self._snapshot(fake, "exit", "user_request")

    async def session_consumption(self, session_id: str) -> ConsumptionReport:
        """Fake consumption is always labelled `simulated`; it never looks like billing."""
        fake = self._sessions.get(session_id)
        if fake is None:
            return ConsumptionReport("unavailable", detail="unknown fake session")
        return ConsumptionReport(
            "simulated", acus=round(min(fake.polls, self._polls_until_finish) * 0.25, 2)
        )

    async def aclose(self) -> None:
        return None

    def set_scenario(self, session_id: str, scenario: FakeScenario) -> None:
        """Change what an existing fake session reports from now on (e.g. a session that
        was waiting for a human later submits structured output)."""
        self._sessions[session_id].scenario = scenario

    def _state(self, fake: _FakeSession, advance: bool) -> tuple[str, str | None]:
        if fake.terminated:
            return "exit", "user_request"
        if advance:
            fake.polls += 1
        if fake.scenario == FakeScenario.TIMEOUT:
            return "running", "working"
        if fake.polls < self._polls_until_finish:
            return ("claimed", None) if fake.polls <= 1 else ("running", "working")
        match fake.scenario:
            case FakeScenario.ERROR:
                return "error", "error"
            case (
                FakeScenario.WAITING_FOR_HUMAN
                | FakeScenario.IDLE_WITH_OUTPUT
                | FakeScenario.IDLE_WITH_NEEDS_HUMAN_OUTPUT
                | FakeScenario.IDLE_WITH_MALFORMED_OUTPUT
            ):
                return "running", "waiting_for_user"
            case FakeScenario.QUOTA:
                return "suspended", "out_of_credits"
            case FakeScenario.MISSING_OUTPUT:
                return "exit", "finished"
            case FakeScenario.UNKNOWN_STATUS:
                return "hibernating", None
            case _:
                return "running", "finished"

    def _output(self, fake: _FakeSession, status: str, detail: str | None) -> dict[str, Any] | None:
        idle_with_output = fake.scenario in {
            FakeScenario.IDLE_WITH_OUTPUT,
            FakeScenario.IDLE_WITH_NEEDS_HUMAN_OUTPUT,
            FakeScenario.IDLE_WITH_MALFORMED_OUTPUT,
        }
        if idle_with_output:
            if status != "running" or detail != "waiting_for_user":
                return None
        elif detail != "finished" or status != "running":
            return None
        repository = tag_value(fake.tags, "repo:") or "apache/superset"
        if fake.kind == "REMEDIATION":
            if fake.scenario == FakeScenario.MALFORMED_OUTPUT:
                return {"schema_version": REMEDIATION_SCHEMA_VERSION, "outcome": "done"}
            prompt = fake.request.prompt
            id_match = _PROBE_ID_RE.search(prompt)
            hash_match = _PROBE_HASH_RE.search(prompt)
            prefix_match = _BRANCH_PREFIX_RE.search(prompt)
            return sample_remediation_output(
                fake.issue_number,
                repository,
                fake.request.base_sha,
                probe_identifier=id_match.group(1) if id_match else "unknown",
                probe_hash=hash_match.group(1) if hash_match else "0" * 64,
                branch_prefix=prefix_match.group(1) if prefix_match else "devin/",
            )
        match fake.scenario:
            case FakeScenario.MALFORMED_OUTPUT | FakeScenario.IDLE_WITH_MALFORMED_OUTPUT:
                return {"schema_version": TRIAGE_SCHEMA_VERSION, "outcome": "maybe", "summary": 1}
            case FakeScenario.NEEDS_HUMAN | FakeScenario.IDLE_WITH_NEEDS_HUMAN_OUTPUT:
                return sample_triage_output(fake.issue_number, repository, "needs_human")
            case FakeScenario.DETERMINISTIC_AUTOMATION:
                return sample_triage_output(
                    fake.issue_number, repository, "deterministic_automation"
                )
            case FakeScenario.NO_CHANGE_NEEDED:
                return sample_triage_output(fake.issue_number, repository, "no_change_needed")
            case _:
                return sample_triage_output(fake.issue_number, repository)

    def _pull_requests(
        self, fake: _FakeSession, output: dict[str, Any] | None
    ) -> tuple[SessionPullRequest, ...]:
        """What Devin's own `pull_requests[]` would report, independent of structured output."""
        if fake.kind != "REMEDIATION" or output is None:
            return ()
        fixture = remediation_fixture(fake.issue_number)
        if output.get("outcome") != "pr_created" and fixture != RemediationFixture.MALFORMED_OUTPUT:
            return ()
        repository = tag_value(fake.tags, "repo:") or "apache/superset"
        if fixture == RemediationFixture.PR_WRONG_REPOSITORY:
            repository = "someone-else/superset"
        return (
            SessionPullRequest(pr_url=fake_pr_url(repository, fake.issue_number), pr_state="open"),
        )

    def _snapshot(self, fake: _FakeSession, status: str, detail: str | None) -> SessionSnapshot:
        output = self._output(fake, status, detail)
        return SessionSnapshot(
            session_id=fake.session_id,
            url=self._url(fake.session_id),
            status=status,
            status_detail=detail,
            tags=fake.tags,
            structured_output=output,
            acus_consumed=round(min(fake.polls, self._polls_until_finish) * 0.25, 2),
            pull_requests=self._pull_requests(fake, output),
        )
