"""Structured triage contract shared by the prompt, the API request and validation."""

import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft7Validator

TRIAGE_SCHEMA_VERSION = "triage.v1"
TRIAGE_OUTCOMES = (
    "remediation_candidate",
    "needs_human",
    "no_change_needed",
    "deterministic_automation",
    "invalid_issue",
)
SEVERITIES = ("critical", "high", "medium", "low")
PRIORITIES = ("p0", "p1", "p2", "p3")

_STRING_LIST = {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 50}

TRIAGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SupersetIssueTriage",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "outcome",
        "reproducible",
        "summary",
        "severity",
        "priority",
        "confidence",
        "evidence",
        "affected_files",
        "acceptance_criteria",
        "probe",
        "focused_tests",
        "scope",
        "risk",
        "blocking_questions",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": TRIAGE_SCHEMA_VERSION},
        "outcome": {"type": "string", "enum": list(TRIAGE_OUTCOMES)},
        "reproducible": {"type": "boolean"},
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "priority": {"type": "string", "enum": list(PRIORITIES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": _STRING_LIST,
        "affected_files": _STRING_LIST,
        "acceptance_criteria": _STRING_LIST,
        "probe": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command", "expected_base_exit_code"],
            "properties": {
                "command": {"type": "string", "maxLength": 2000},
                "expected_base_exit_code": {"type": "integer", "minimum": 0, "maximum": 255},
                "rationale": {"type": "string", "maxLength": 2000},
            },
        },
        "focused_tests": _STRING_LIST,
        "scope": {"type": "string", "maxLength": 4000},
        "risk": {
            "type": "object",
            "additionalProperties": False,
            "required": ["level", "notes"],
            "properties": {
                "level": {"type": "string", "enum": ["low", "medium", "high"]},
                "notes": {"type": "string", "maxLength": 4000},
            },
        },
        "blocking_questions": _STRING_LIST,
    },
}

_validator = Draft7Validator(TRIAGE_OUTPUT_SCHEMA)
Draft7Validator.check_schema(TRIAGE_OUTPUT_SCHEMA)
assert len(json.dumps(TRIAGE_OUTPUT_SCHEMA).encode()) < 64 * 1024


class TriageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class TriageResult:
    outcome: str
    reproducible: bool
    summary: str
    severity: str
    priority: str
    confidence: float
    probe_command: str
    expected_base_exit_code: int
    blocking_questions: tuple[str, ...]
    raw: dict[str, Any]

    @property
    def remediation_candidate(self) -> bool:
        return self.outcome == "remediation_candidate"


def validate_triage_output(output: object) -> TriageResult:
    if output is None:
        raise TriageValidationError("structured output missing")
    if not isinstance(output, dict):
        raise TriageValidationError(
            f"structured output is {type(output).__name__}, expected object"
        )
    errors = sorted(_validator.iter_errors(output), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise TriageValidationError(f"structured output invalid at {path}: {first.message}")
    probe = output["probe"]
    return TriageResult(
        outcome=str(output["outcome"]),
        reproducible=bool(output["reproducible"]),
        summary=str(output["summary"]),
        severity=str(output["severity"]),
        priority=str(output["priority"]),
        confidence=float(output["confidence"]),
        probe_command=str(probe["command"]),
        expected_base_exit_code=int(probe["expected_base_exit_code"]),
        blocking_questions=tuple(str(q) for q in output["blocking_questions"]),
        raw=output,
    )
