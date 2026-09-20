import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .remediation import REMEDIATION_SCHEMA_VERSION
from .triage import TRIAGE_SCHEMA_VERSION

TRIAGE_PROMPT_VERSION = "triage_v1"
REMEDIATION_PROMPT_VERSION = "remediation_v1"
_PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class TriagePromptInput:
    repository: str
    base_sha: str
    issue_number: int
    issue_title: str
    issue_body: str
    issue_labels: tuple[str, ...]
    issue_url: str
    eligibility_reasons: tuple[str, ...]
    operation_key: str
    case_id: str
    attempt_id: str


@dataclass(frozen=True)
class RemediationPromptInput:
    repository: str
    base_ref: str
    base_sha: str
    branch_prefix: str
    issue_number: int
    issue_title: str
    issue_body: str
    issue_url: str
    triage_output: dict[str, Any]
    triage_result_hash: str
    probe_identifier: str
    probe_hash: str
    probe_script: str
    probe_expected_base_exit: int
    probe_expected_head_exit: int
    probe_registry_path: str
    approved_by: str
    approved_at: str
    operation_key: str
    case_id: str
    attempt_id: str


@lru_cache
def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_PROMPTS_DIR)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _boundary(nonce: str) -> str:
    return f"=====UNTRUSTED-ISSUE-{nonce}====="


def _check_boundary(boundary: str, *untrusted: str) -> None:
    for text in untrusted:
        if boundary in text:
            raise ValueError("issue content contains the delimiter nonce")


def render_triage_prompt(
    data: TriagePromptInput, version: str = TRIAGE_PROMPT_VERSION, nonce: str | None = None
) -> str:
    nonce = nonce or secrets.token_hex(8)
    boundary = _boundary(nonce)
    body = data.issue_body
    title = data.issue_title
    _check_boundary(boundary, body, title)
    template = _environment().get_template(f"{version}.md")
    return template.render(
        prompt_version=version,
        schema_version=TRIAGE_SCHEMA_VERSION,
        repository=data.repository,
        base_sha=data.base_sha,
        issue_number=data.issue_number,
        issue_title=title.replace("\n", " "),
        issue_labels=", ".join(data.issue_labels) or "-",
        issue_url=data.issue_url,
        issue_body=body,
        eligibility_reasons=list(data.eligibility_reasons) or ["(none recorded)"],
        operation_key=data.operation_key,
        case_id=data.case_id,
        attempt_id=data.attempt_id,
        boundary=boundary,
    )


def render_remediation_prompt(
    data: RemediationPromptInput,
    version: str = REMEDIATION_PROMPT_VERSION,
    nonce: str | None = None,
) -> str:
    nonce = nonce or secrets.token_hex(8)
    boundary = _boundary(nonce)
    _check_boundary(boundary, data.issue_body, data.issue_title)
    if "```" in data.probe_script:
        raise ValueError("probe script contains a fenced-code delimiter")
    triage = data.triage_output
    template = _environment().get_template(f"{version}.md")
    return template.render(
        prompt_version=version,
        schema_version=REMEDIATION_SCHEMA_VERSION,
        repository=data.repository,
        base_ref=data.base_ref,
        base_sha=data.base_sha,
        branch_prefix=data.branch_prefix,
        issue_number=data.issue_number,
        issue_title=data.issue_title.replace("\n", " "),
        issue_url=data.issue_url,
        issue_body=data.issue_body,
        triage={
            "outcome": triage.get("outcome", ""),
            "summary": triage.get("summary", ""),
            "severity": triage.get("severity", ""),
            "priority": triage.get("priority", ""),
            "affected_files": list(triage.get("affected_files") or []) or ["(none listed)"],
            "acceptance_criteria": list(triage.get("acceptance_criteria") or [])
            or ["(none listed)"],
            "scope": triage.get("scope", "") or "(not specified)",
            "focused_tests": list(triage.get("focused_tests") or []) or ["(none listed)"],
        },
        triage_result_hash=data.triage_result_hash,
        probe_identifier=data.probe_identifier,
        probe_hash=data.probe_hash,
        probe_script=data.probe_script.rstrip("\n"),
        probe_expected_base_exit=data.probe_expected_base_exit,
        probe_expected_head_exit=data.probe_expected_head_exit,
        probe_registry_path=data.probe_registry_path,
        approved_by=data.approved_by,
        approved_at=data.approved_at,
        operation_key=data.operation_key,
        case_id=data.case_id,
        attempt_id=data.attempt_id,
        boundary=boundary,
    )
