"""Deterministic in-memory Devin client that speaks the v3 status vocabulary.

Scenarios are selected per issue number so simulation fixtures and tests can
exercise every worker path without network access or ACU spend.
"""

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .client import (
    CreateSessionRequest,
    DevinApiError,
    DevinSessionNotFound,
    DevinTransportError,
    SessionSnapshot,
)
from .tags import tag_value
from .triage import TRIAGE_SCHEMA_VERSION


class FakeScenario(StrEnum):
    SUCCESS = "success"
    NEEDS_HUMAN = "needs_human"
    ERROR = "error"
    WAITING_FOR_HUMAN = "waiting_for_human"
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
}


def scenario_for(
    issue_number: int, overrides: dict[int, FakeScenario] | None = None
) -> FakeScenario:
    if overrides and issue_number in overrides:
        return overrides[issue_number]
    if issue_number in FIXTURE_SCENARIOS:
        return FIXTURE_SCENARIOS[issue_number]
    if issue_number % 5 == 0:
        return FakeScenario.ERROR
    if issue_number % 7 == 0:
        return FakeScenario.WAITING_FOR_HUMAN
    if issue_number % 3 == 0:
        return FakeScenario.NEEDS_HUMAN
    return FakeScenario.SUCCESS


def sample_triage_output(
    issue_number: int, repository: str, outcome: str = "remediation_candidate"
) -> dict[str, Any]:
    candidate = outcome == "remediation_candidate"
    return {
        "schema_version": TRIAGE_SCHEMA_VERSION,
        "outcome": outcome,
        "reproducible": candidate,
        "summary": (
            f"simulated triage of {repository}#{issue_number}: "
            + ("bounded fix identified" if candidate else "requires product decision")
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
            scenario=scenario_for(issue_number, self._scenarios),
            tags=tuple(request.all_tags()),
        )
        self._sessions[session_id] = fake
        self._by_operation[request.operation_key] = session_id
        return fake

    async def create_session(self, request: CreateSessionRequest) -> SessionSnapshot:
        self.create_calls += 1
        issue_number = int(tag_value(request.tags, "issue:") or 0)
        scenario = scenario_for(issue_number, self._scenarios)
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

    async def aclose(self) -> None:
        return None

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
            case FakeScenario.WAITING_FOR_HUMAN:
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
        if detail != "finished" or status != "running":
            return None
        if fake.kind == "REMEDIATION":
            repository = tag_value(fake.tags, "repo:") or "apache/superset"
            return {"pr_url": f"https://github.com/{repository}/pull/{9000 + fake.issue_number}"}
        repository = tag_value(fake.tags, "repo:") or "apache/superset"
        match fake.scenario:
            case FakeScenario.MALFORMED_OUTPUT:
                return {"schema_version": TRIAGE_SCHEMA_VERSION, "outcome": "maybe", "summary": 1}
            case FakeScenario.NEEDS_HUMAN:
                return sample_triage_output(fake.issue_number, repository, "needs_human")
            case _:
                return sample_triage_output(fake.issue_number, repository)

    def _snapshot(self, fake: _FakeSession, status: str, detail: str | None) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=fake.session_id,
            url=self._url(fake.session_id),
            status=status,
            status_detail=detail,
            tags=fake.tags,
            structured_output=self._output(fake, status, detail),
            acus_consumed=round(min(fake.polls, self._polls_until_finish) * 0.25, 2),
        )
