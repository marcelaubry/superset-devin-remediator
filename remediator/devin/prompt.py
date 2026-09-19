import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .triage import TRIAGE_SCHEMA_VERSION

TRIAGE_PROMPT_VERSION = "triage_v1"
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


def render_triage_prompt(
    data: TriagePromptInput, version: str = TRIAGE_PROMPT_VERSION, nonce: str | None = None
) -> str:
    nonce = nonce or secrets.token_hex(8)
    boundary = _boundary(nonce)
    body = data.issue_body
    title = data.issue_title
    for untrusted in (body, title):
        if boundary in untrusted:
            raise ValueError("issue content contains the delimiter nonce")
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
