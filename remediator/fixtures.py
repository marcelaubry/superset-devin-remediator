"""Deterministic Phase 4 fixture issue numbers shared by every fake adapter.

Each number selects one remediation failure mode. Issue numbers were chosen so the fake
Devin *triage* scenario is `success` (not divisible by 3, 5 or 7) and the case therefore
reaches `REMEDIATION_APPROVED` before the remediation-specific behaviour kicks in. Numbers
outside this table take the default (successful) path in every fake.
"""

import hashlib
from enum import StrEnum


class RemediationFixture(StrEnum):
    SUCCESS = "success"
    # Devin session / structured output
    MALFORMED_OUTPUT = "malformed_output"
    UNCERTAIN_CREATE = "uncertain_create"
    SESSION_TIMEOUT = "session_timeout"
    NO_CHANGE_NEEDED = "no_change_needed"
    NEEDS_HUMAN = "needs_human"
    OUTCOME_FAILED = "outcome_failed"
    PR_CLAIMED_BUT_ABSENT = "pr_claimed_but_absent"
    CONTRADICTORY_PR_URL = "contradictory_pr_url"
    CONTRADICTORY_HEAD_SHA = "contradictory_head_sha"
    # GitHub PR validation
    PR_WRONG_REPOSITORY = "pr_wrong_repository"
    PR_WRONG_BASE_BRANCH = "pr_wrong_base_branch"
    PR_WRONG_BRANCH_PREFIX = "pr_wrong_branch_prefix"
    PR_MERGED = "pr_merged"
    PR_WRONG_AUTHOR = "pr_wrong_author"
    PR_ISSUE_SUBSTRING = "pr_issue_substring"
    PR_FORBIDDEN_FILES = "pr_forbidden_files"
    PR_SCOPE_EXPANSION = "pr_scope_expansion"
    PR_DIVERGED_BASE = "pr_diverged_base"
    # Independent probe
    PROBE_BASE_PASSES = "probe_base_passes"
    PROBE_HEAD_FAILS = "probe_head_fails"
    PROBE_INFRASTRUCTURE = "probe_infrastructure"
    # CI
    CI_FAILED = "ci_failed"
    CI_PENDING_FOREVER = "ci_pending_forever"
    CI_ABSENT = "ci_absent"


REMEDIATION_FIXTURES: dict[int, RemediationFixture] = {
    4702: RemediationFixture.SUCCESS,
    4703: RemediationFixture.MALFORMED_OUTPUT,
    4706: RemediationFixture.UNCERTAIN_CREATE,
    4708: RemediationFixture.SESSION_TIMEOUT,
    4709: RemediationFixture.NO_CHANGE_NEEDED,
    4712: RemediationFixture.NEEDS_HUMAN,
    4714: RemediationFixture.OUTCOME_FAILED,
    4717: RemediationFixture.PR_CLAIMED_BUT_ABSENT,
    4721: RemediationFixture.CONTRADICTORY_PR_URL,
    4723: RemediationFixture.CONTRADICTORY_HEAD_SHA,
    4724: RemediationFixture.PR_WRONG_REPOSITORY,
    4726: RemediationFixture.PR_WRONG_BASE_BRANCH,
    4727: RemediationFixture.PR_WRONG_BRANCH_PREFIX,
    4729: RemediationFixture.PR_MERGED,
    4733: RemediationFixture.PR_WRONG_AUTHOR,
    4736: RemediationFixture.PR_ISSUE_SUBSTRING,
    4738: RemediationFixture.PR_FORBIDDEN_FILES,
    4741: RemediationFixture.PR_SCOPE_EXPANSION,
    4742: RemediationFixture.PR_DIVERGED_BASE,
    4744: RemediationFixture.PROBE_BASE_PASSES,
    4747: RemediationFixture.PROBE_HEAD_FAILS,
    4748: RemediationFixture.PROBE_INFRASTRUCTURE,
    4751: RemediationFixture.CI_FAILED,
    4754: RemediationFixture.CI_PENDING_FOREVER,
    4756: RemediationFixture.CI_ABSENT,
}


def remediation_fixture(issue_number: int) -> RemediationFixture:
    return REMEDIATION_FIXTURES.get(issue_number, RemediationFixture.SUCCESS)


def fake_pr_number(issue_number: int) -> int:
    return 9000 + issue_number


def fake_head_sha(issue_number: int) -> str:
    """Deterministic 40-hex head SHA shared by the fake Devin and fake GitHub adapters."""
    return hashlib.sha1(f"head:{issue_number}".encode()).hexdigest()
