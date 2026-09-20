"""Structured remediation contract (`remediation.v1`).

The schema is sent to Devin as `structured_output_schema` and re-validated locally when the
session finishes. A valid document is *evidence* about what Devin believes it did; every
claim in it (PR URL, head SHA, branch, probe identity) is cross-checked against Devin's own
`pull_requests[]`, GitHub and an independent probe run before the case advances.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft7Validator

REMEDIATION_SCHEMA_VERSION = "remediation.v1"
REMEDIATION_OUTCOMES = ("pr_created", "no_change_needed", "needs_human", "failed")

_SHA_PATTERN = "^[0-9a-f]{40}$"
_STRING_LIST = {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 100}

REMEDIATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SupersetIssueRemediation",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "outcome",
        "summary",
        "base_sha",
        "head_sha",
        "branch",
        "pr_url",
        "issue_reference",
        "changed_files",
        "commits",
        "tests_run",
        "probe_identifier",
        "probe_hash",
        "risks",
        "blocking_questions",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": REMEDIATION_SCHEMA_VERSION},
        "outcome": {"type": "string", "enum": list(REMEDIATION_OUTCOMES)},
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
        "base_sha": {"type": "string", "pattern": _SHA_PATTERN},
        "head_sha": {"type": ["string", "null"], "pattern": _SHA_PATTERN},
        "branch": {"type": ["string", "null"], "maxLength": 255},
        "pr_url": {"type": ["string", "null"], "maxLength": 500},
        "issue_reference": {"type": "string", "maxLength": 200},
        "changed_files": _STRING_LIST,
        "commits": {
            "type": "array",
            "maxItems": 100,
            "items": {"type": "string", "pattern": _SHA_PATTERN},
        },
        "tests_run": _STRING_LIST,
        "probe_identifier": {"type": "string", "maxLength": 300},
        "probe_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "risks": _STRING_LIST,
        "blocking_questions": _STRING_LIST,
    },
}

_validator = Draft7Validator(REMEDIATION_OUTPUT_SCHEMA)
Draft7Validator.check_schema(REMEDIATION_OUTPUT_SCHEMA)
assert len(json.dumps(REMEDIATION_OUTPUT_SCHEMA).encode()) < 64 * 1024

_ISSUE_REF_RE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)$")


class RemediationValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RemediationResult:
    outcome: str
    summary: str
    base_sha: str
    head_sha: str | None
    branch: str | None
    pr_url: str | None
    issue_reference: str
    changed_files: tuple[str, ...]
    commits: tuple[str, ...]
    tests_run: tuple[str, ...]
    probe_identifier: str
    probe_hash: str
    risks: tuple[str, ...]
    blocking_questions: tuple[str, ...]
    raw: dict[str, Any]

    @property
    def pr_created(self) -> bool:
        return self.outcome == "pr_created"

    def issue_reference_parts(self) -> tuple[str, int] | None:
        match = _ISSUE_REF_RE.match(self.issue_reference.strip())
        if match is None:
            return None
        return match.group("repo").lower(), int(match.group("number"))


def validate_remediation_output(output: object) -> RemediationResult:
    if output is None:
        raise RemediationValidationError("structured output missing")
    if not isinstance(output, dict):
        raise RemediationValidationError(
            f"structured output is {type(output).__name__}, expected object"
        )
    errors = sorted(_validator.iter_errors(output), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise RemediationValidationError(f"structured output invalid at {path}: {first.message}")
    result = RemediationResult(
        outcome=str(output["outcome"]),
        summary=str(output["summary"]),
        base_sha=str(output["base_sha"]),
        head_sha=output["head_sha"],
        branch=output["branch"],
        pr_url=output["pr_url"],
        issue_reference=str(output["issue_reference"]),
        changed_files=tuple(str(f) for f in output["changed_files"]),
        commits=tuple(str(c) for c in output["commits"]),
        tests_run=tuple(str(t) for t in output["tests_run"]),
        probe_identifier=str(output["probe_identifier"]),
        probe_hash=str(output["probe_hash"]),
        risks=tuple(str(r) for r in output["risks"]),
        blocking_questions=tuple(str(q) for q in output["blocking_questions"]),
        raw=output,
    )
    if result.pr_created and not (result.pr_url and result.head_sha and result.branch):
        raise RemediationValidationError(
            "outcome pr_created requires pr_url, head_sha and branch to be present"
        )
    return result
