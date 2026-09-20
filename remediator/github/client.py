"""GitHub adapters used by the outbox dispatcher and the Phase 4 verification pipeline.

Every method enforces the repository allowlist before any network call. The live client is
a thin REST wrapper (https://docs.github.com/en/rest): issues, labels and comments for the
approval flow; pulls, pull files, compare, check-runs, the issue timeline and the GraphQL
`closingIssuesReferences` connection for independent PR verification.
"""

import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..fixtures import RemediationFixture, fake_head_sha, fake_pr_number, remediation_fixture
from ..metrics import instrument_http_client

logger = logging.getLogger(__name__)

# Fixture issue numbers whose first label attempts fail in the fake client; used by
# scripts/simulate.py and the tests to exercise APPROVAL_DELIVERY_FAILED + operator retry.
FAKE_LABEL_FAILURE_ISSUES: frozenset[int] = frozenset({4688})


class GitHubApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class RepositoryNotAllowed(GitHubApiError):
    def __init__(self, repository: str) -> None:
        super().__init__(f"repository {repository!r} is not allowlisted", retryable=False)


class IssueNotFound(GitHubApiError):
    def __init__(self, repository: str, issue_number: int) -> None:
        super().__init__(f"{repository}#{issue_number} not found", status_code=404, retryable=False)


@dataclass(frozen=True)
class IssueSnapshot:
    repository: str
    number: int
    title: str
    state: str
    labels: tuple[str, ...]
    html_url: str


@dataclass(frozen=True)
class LabelResult:
    applied: bool  # False when the label was already present (idempotent no-op)
    labels: tuple[str, ...]


class PullRequestNotFound(GitHubApiError):
    def __init__(self, repository: str, number: int) -> None:
        super().__init__(f"{repository} pull #{number} not found", status_code=404, retryable=False)


@dataclass(frozen=True)
class PullRequestSnapshot:
    repository: str
    number: int
    html_url: str
    state: str
    draft: bool
    merged: bool
    title: str
    body: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    head_repository: str
    author_login: str
    author_type: str


@dataclass(frozen=True)
class CompareResult:
    """`GET /repos/{r}/compare/{base}...{head}`: status is ahead|behind|diverged|identical."""

    status: str
    ahead_by: int
    behind_by: int
    merge_base_sha: str | None


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str  # queued | in_progress | completed | waiting | requested | pending
    conclusion: str | None  # success | failure | neutral | cancelled | skipped | timed_out | ...
    html_url: str
    app_slug: str | None = None


class GitHubIssuesClient(Protocol):
    async def get_issue(self, repository: str, issue_number: int) -> IssueSnapshot: ...

    async def add_label(self, repository: str, issue_number: int, label: str) -> LabelResult: ...

    async def create_comment(self, repository: str, issue_number: int, body: str) -> int: ...

    async def find_comment(self, repository: str, issue_number: int, marker: str) -> int | None:
        """Id of an existing issue comment containing `marker`, for crash-safe idempotency."""
        ...

    async def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot: ...

    async def list_pull_request_files(self, repository: str, number: int) -> tuple[str, ...]: ...

    async def compare_commits(self, repository: str, base: str, head: str) -> CompareResult: ...

    async def closing_issue_references(
        self, repository: str, number: int
    ) -> tuple[int, ...] | None:
        """Issue numbers GitHub itself resolved as closed-by this PR; None when unavailable."""
        ...

    async def pull_requests_referencing_issue(
        self, repository: str, issue_number: int
    ) -> tuple[int, ...] | None:
        """PR numbers cross-referenced on the issue timeline; None when unavailable."""
        ...

    async def list_check_runs(self, repository: str, sha: str) -> tuple[CheckRun, ...]: ...

    async def aclose(self) -> None: ...


FAKE_CHECK_NAMES = ("pre-commit", "python-unit-tests")


def _mentions_issue(body: str, issue_number: int) -> bool:
    return re.search(rf"#{issue_number}(?![0-9])", body) is not None


def comment_marker(kind: str, approval_request_id: str) -> str:
    return f"<!-- remediator:{kind}:{approval_request_id} -->"


def _check_allowed(allowed: frozenset[str], repository: str) -> str:
    normalized = repository.strip().lower()
    if normalized not in allowed:
        raise RepositoryNotAllowed(repository)
    return normalized


@dataclass
class _FakeIssue:
    title: str
    labels: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    comment_ids: list[int] = field(default_factory=list)
    label_calls: int = 0


class FakeGitHubClient:
    """In-memory issues store. Every fixture issue in an allowlisted repository exists."""

    def __init__(
        self,
        allowed_repositories: Iterable[str],
        *,
        fail_labels: bool = False,
        failing_issue_attempts: int = 0,
    ) -> None:
        self._allowed = frozenset(r.strip().lower() for r in allowed_repositories)
        self._fail_labels = fail_labels
        self._failing_issue_attempts = failing_issue_attempts
        self.issues: dict[tuple[str, int], _FakeIssue] = {}
        self.missing: set[tuple[str, int]] = set()
        self.label_calls: list[tuple[str, int, str]] = []
        self.comment_calls: list[tuple[str, int, str]] = []
        self._next_comment_id = 1000
        self.pulls: dict[tuple[str, int], PullRequestSnapshot] = {}
        self.pull_files: dict[tuple[str, int], tuple[str, ...]] = {}
        self.closing_refs: dict[tuple[str, int], tuple[int, ...] | None] = {}
        self.compare_results: dict[tuple[str, str, str], CompareResult] = {}
        self.check_runs: dict[tuple[str, str], tuple[CheckRun, ...]] = {}
        self.check_observations: dict[tuple[str, str], int] = {}
        self.pull_calls: list[tuple[str, int]] = []

    def __repr__(self) -> str:
        return f"FakeGitHubClient(fail_labels={self._fail_labels})"

    async def aclose(self) -> None:
        return None

    def _issue(self, repository: str, issue_number: int) -> _FakeIssue:
        key = (repository, issue_number)
        if key in self.missing:
            raise IssueNotFound(repository, issue_number)
        return self.issues.setdefault(key, _FakeIssue(title=f"fixture issue #{issue_number}"))

    async def get_issue(self, repository: str, issue_number: int) -> IssueSnapshot:
        repo = _check_allowed(self._allowed, repository)
        issue = self._issue(repo, issue_number)
        return IssueSnapshot(
            repository=repo,
            number=issue_number,
            title=issue.title,
            state="open",
            labels=tuple(issue.labels),
            html_url=f"https://github.com/{repo}/issues/{issue_number}",
        )

    async def add_label(self, repository: str, issue_number: int, label: str) -> LabelResult:
        repo = _check_allowed(self._allowed, repository)
        issue = self._issue(repo, issue_number)
        issue.label_calls += 1
        self.label_calls.append((repo, issue_number, label))
        if self._fail_labels:
            raise GitHubApiError(
                "fake GitHub adapter configured to fail label writes",
                status_code=502,
                retryable=True,
            )
        if (
            issue_number in FAKE_LABEL_FAILURE_ISSUES
            and issue.label_calls <= self._failing_issue_attempts
        ):
            raise GitHubApiError(
                f"fake GitHub label failure {issue.label_calls}/{self._failing_issue_attempts} "
                f"for fixture issue #{issue_number}",
                status_code=502,
                retryable=True,
            )
        if label in issue.labels:
            return LabelResult(applied=False, labels=tuple(issue.labels))
        issue.labels.append(label)
        return LabelResult(applied=True, labels=tuple(issue.labels))

    async def create_comment(self, repository: str, issue_number: int, body: str) -> int:
        repo = _check_allowed(self._allowed, repository)
        issue = self._issue(repo, issue_number)
        issue.comments.append(body)
        self.comment_calls.append((repo, issue_number, body))
        self._next_comment_id += 1
        issue.comment_ids.append(self._next_comment_id)
        return self._next_comment_id

    async def find_comment(self, repository: str, issue_number: int, marker: str) -> int | None:
        repo = _check_allowed(self._allowed, repository)
        issue = self._issue(repo, issue_number)
        for comment_id, body in zip(issue.comment_ids, issue.comments, strict=True):
            if marker in body:
                return comment_id
        return None

    # -- pull requests / checks -------------------------------------------------------

    def _fixture_pull(self, repo: str, number: int) -> PullRequestSnapshot | None:
        issue_number = number - 9000
        if issue_number <= 0 or fake_pr_number(issue_number) != number:
            return None
        fixture = remediation_fixture(issue_number)
        if fixture == RemediationFixture.PR_CLAIMED_BUT_ABSENT:
            return None
        prefix = "fix/" if fixture == RemediationFixture.PR_WRONG_BRANCH_PREFIX else "devin/"
        referenced = (
            issue_number * 10 if fixture == RemediationFixture.PR_ISSUE_SUBSTRING else issue_number
        )
        return PullRequestSnapshot(
            repository=repo,
            number=number,
            html_url=f"https://github.com/{repo}/pull/{number}",
            state="closed" if fixture == RemediationFixture.PR_MERGED else "open",
            draft=fixture != RemediationFixture.PR_MERGED,
            merged=fixture == RemediationFixture.PR_MERGED,
            title=f"fix: resolve #{referenced}",
            body=(
                f"Produced by Devin via the Superset remediation automation.\n\n"
                f"Closes {repo}#{referenced}\n"
            ),
            base_ref="develop" if fixture == RemediationFixture.PR_WRONG_BASE_BRANCH else "master",
            base_sha="b" * 40,
            head_ref=f"{prefix}fix-issue-{issue_number}",
            head_sha=fake_head_sha(issue_number),
            head_repository=repo,
            author_login=(
                "mallory"
                if fixture == RemediationFixture.PR_WRONG_AUTHOR
                else "devin-ai-integration[bot]"
            ),
            author_type="User" if fixture == RemediationFixture.PR_WRONG_AUTHOR else "Bot",
        )

    def _fixture_files(self, issue_number: int) -> tuple[str, ...]:
        fixture = remediation_fixture(issue_number)
        files = ["superset/views/core.py", "tests/unit_tests/views/test_core.py"]
        if fixture == RemediationFixture.PR_FORBIDDEN_FILES:
            files.append(".github/workflows/pre-commit.yml")
        if fixture == RemediationFixture.PR_SCOPE_EXPANSION:
            files.extend(f"superset/module_{i}/handler.py" for i in range(40))
        return tuple(files)

    def _fixture_checks(self, issue_number: int, observation: int) -> tuple[CheckRun, ...]:
        fixture = remediation_fixture(issue_number)
        if fixture == RemediationFixture.CI_ABSENT:
            return ()
        url = "https://github.com/apache/superset/actions/runs/1"

        def run(name: str, status: str, conclusion: str | None) -> CheckRun:
            return CheckRun(name=name, status=status, conclusion=conclusion, html_url=url)

        if fixture == RemediationFixture.CI_PENDING_FOREVER or observation < 2:
            return tuple(run(name, "in_progress", None) for name in FAKE_CHECK_NAMES)
        if fixture == RemediationFixture.CI_FAILED:
            return (
                run(FAKE_CHECK_NAMES[0], "completed", "success"),
                run(FAKE_CHECK_NAMES[1], "completed", "failure"),
            )
        return tuple(run(name, "completed", "success") for name in FAKE_CHECK_NAMES)

    def _served_pull(self, repo: str, number: int) -> PullRequestSnapshot | None:
        return self.pulls.get((repo, number)) or self._fixture_pull(repo, number)

    def _pull_for_head(self, repo: str, sha: str) -> PullRequestSnapshot | None:
        for pull in self.pulls.values():
            if pull.repository == repo and pull.head_sha == sha:
                return pull
        for pull_repo, number in self.pull_calls:
            fixture_pull = self._fixture_pull(pull_repo, number)
            if fixture_pull is not None and pull_repo == repo and fixture_pull.head_sha == sha:
                return fixture_pull
        return None

    async def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        repo = _check_allowed(self._allowed, repository)
        self.pull_calls.append((repo, number))
        pull = self._served_pull(repo, number)
        if pull is None:
            raise PullRequestNotFound(repo, number)
        return pull

    async def list_pull_request_files(self, repository: str, number: int) -> tuple[str, ...]:
        repo = _check_allowed(self._allowed, repository)
        await self.get_pull_request(repo, number)
        key = (repo, number)
        if key in self.pull_files:
            return self.pull_files[key]
        return self._fixture_files(number - 9000)

    async def compare_commits(self, repository: str, base: str, head: str) -> CompareResult:
        repo = _check_allowed(self._allowed, repository)
        key = (repo, base, head)
        if key in self.compare_results:
            return self.compare_results[key]
        pull = self._pull_for_head(repo, head)
        if pull is None:
            raise GitHubApiError(
                f"compare {base[:12]}...{head[:12]}: not found", status_code=404, retryable=False
            )
        if remediation_fixture(pull.number - 9000) == RemediationFixture.PR_DIVERGED_BASE:
            return CompareResult("diverged", ahead_by=1, behind_by=5, merge_base_sha="c" * 40)
        return CompareResult("ahead", ahead_by=1, behind_by=0, merge_base_sha=base)

    async def closing_issue_references(
        self, repository: str, number: int
    ) -> tuple[int, ...] | None:
        repo = _check_allowed(self._allowed, repository)
        key = (repo, number)
        if key in self.closing_refs:
            return self.closing_refs[key]
        pull = await self.get_pull_request(repo, number)
        issue_number = number - 9000
        if remediation_fixture(issue_number) == RemediationFixture.PR_ISSUE_SUBSTRING:
            return (issue_number * 10,)
        return (issue_number,) if _mentions_issue(pull.body, issue_number) else ()

    async def pull_requests_referencing_issue(
        self, repository: str, issue_number: int
    ) -> tuple[int, ...] | None:
        repo = _check_allowed(self._allowed, repository)
        self._issue(repo, issue_number)
        configured = tuple(
            number
            for (r, number), refs in self.closing_refs.items()
            if r == repo and refs and issue_number in refs
        )
        if configured:
            return configured
        fixture_pull = self._fixture_pull(repo, fake_pr_number(issue_number))
        if fixture_pull is not None and _mentions_issue(fixture_pull.body, issue_number):
            return (fixture_pull.number,)
        return ()

    async def list_check_runs(self, repository: str, sha: str) -> tuple[CheckRun, ...]:
        repo = _check_allowed(self._allowed, repository)
        key = (repo, sha)
        self.check_observations[key] = self.check_observations.get(key, 0) + 1
        if key in self.check_runs:
            return self.check_runs[key]
        pull = self._pull_for_head(repo, sha)
        if pull is None:
            return ()
        return self._fixture_checks(pull.number - 9000, self.check_observations[key])


RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _is_rate_limited(headers: httpx.Headers) -> bool:
    """GitHub signals primary/secondary rate limits with 403 + these headers."""
    if headers.get("retry-after") is not None:
        return True
    return str(headers.get("x-ratelimit-remaining")) == "0"


def _retry_after(headers: httpx.Headers) -> float | None:
    header = headers.get("retry-after")
    if header is not None:
        try:
            return max(float(header), 0.0)
        except ValueError:
            return None
    reset = headers.get("x-ratelimit-reset")
    if reset is not None and headers.get("x-ratelimit-remaining") == "0":
        try:
            return max(float(reset) - time.time(), 0.0)
        except ValueError:
            return None
    return None


class LiveGitHubClient:
    def __init__(
        self,
        token: str,
        allowed_repositories: Iterable[str],
        *,
        base_url: str = "https://api.github.com",
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allowed = frozenset(r.strip().lower() for r in allowed_repositories)
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "superset-devin-remediator/phase3",
            },
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )
        instrument_http_client(self._http, "github")

    def __repr__(self) -> str:
        return f"LiveGitHubClient(repositories={sorted(self._allowed)!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise GitHubApiError(
                f"{method} {path}: {exc.__class__.__name__}", retryable=True
            ) from exc
        if response.status_code == 404:
            raise GitHubApiError(f"{method} {path}: not found", status_code=404, retryable=False)
        retry_after = _retry_after(response.headers)
        if response.status_code in RETRYABLE_STATUS_CODES or (
            response.status_code == 403 and _is_rate_limited(response.headers)
        ):
            raise GitHubApiError(
                f"{method} {path}: HTTP {response.status_code}"
                + (" (rate limited)" if response.status_code == 403 else ""),
                status_code=response.status_code,
                retryable=True,
                retry_after_seconds=retry_after,
            )
        if response.status_code >= 400:
            raise GitHubApiError(
                f"{method} {path}: HTTP {response.status_code}",
                status_code=response.status_code,
                retryable=False,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubApiError(f"{method} {path}: non-JSON body", retryable=True) from exc

    async def get_issue(self, repository: str, issue_number: int) -> IssueSnapshot:
        repo = _check_allowed(self._allowed, repository)
        try:
            payload = await self._request("GET", f"/repos/{repo}/issues/{issue_number}")
        except GitHubApiError as exc:
            if exc.status_code == 404:
                raise IssueNotFound(repo, issue_number) from exc
            raise
        if not isinstance(payload, dict) or "pull_request" in payload:
            raise IssueNotFound(repo, issue_number)
        return IssueSnapshot(
            repository=repo,
            number=int(payload.get("number", issue_number)),
            title=str(payload.get("title", "")),
            state=str(payload.get("state", "")),
            labels=tuple(
                str(label.get("name", ""))
                for label in payload.get("labels") or []
                if isinstance(label, dict)
            ),
            html_url=str(payload.get("html_url", "")),
        )

    async def add_label(self, repository: str, issue_number: int, label: str) -> LabelResult:
        repo = _check_allowed(self._allowed, repository)
        current = await self.get_issue(repo, issue_number)
        if label in current.labels:
            return LabelResult(applied=False, labels=current.labels)
        payload = await self._request(
            "POST", f"/repos/{repo}/issues/{issue_number}/labels", json={"labels": [label]}
        )
        labels = (
            tuple(str(item.get("name", "")) for item in payload if isinstance(item, dict))
            if isinstance(payload, list)
            else current.labels + (label,)
        )
        return LabelResult(applied=True, labels=labels)

    async def find_comment(self, repository: str, issue_number: int, marker: str) -> int | None:
        repo = _check_allowed(self._allowed, repository)
        for page in range(1, 6):
            payload = await self._request(
                "GET",
                f"/repos/{repo}/issues/{issue_number}/comments"
                f"?per_page=100&page={page}&sort=created&direction=desc",
            )
            if not isinstance(payload, list):
                return None
            for item in payload:
                if isinstance(item, dict) and marker in str(item.get("body", "")):
                    return int(item["id"])
            if len(payload) < 100:
                break
        return None

    async def repository_metadata(self, repository: str) -> dict[str, Any]:
        """GET /repos/{repo}: identity, default branch and token permissions (readiness only)."""
        repo = _check_allowed(self._allowed, repository)
        payload = await self._request("GET", f"/repos/{repo}")
        return payload if isinstance(payload, dict) else {}

    async def resolve_ref(self, repository: str, ref: str) -> str:
        """GET /repos/{repo}/commits/{ref}: the full SHA at the tip of `ref` (readiness only,
        read-only; the worker pins through `github_refs.GitHubBaseCommitResolver`)."""
        repo = _check_allowed(self._allowed, repository)
        payload = await self._request("GET", f"/repos/{repo}/commits/{ref}")
        sha = payload.get("sha") if isinstance(payload, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubApiError(f"{repo}@{ref} returned no full SHA", retryable=False)
        return sha

    async def label_exists(self, repository: str, label: str) -> bool:
        """GET /repos/{repo}/labels/{name} (readiness only, read-only)."""
        repo = _check_allowed(self._allowed, repository)
        try:
            await self._request("GET", f"/repos/{repo}/labels/{label}")
        except GitHubApiError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    async def create_comment(self, repository: str, issue_number: int, body: str) -> int:
        repo = _check_allowed(self._allowed, repository)
        payload = await self._request(
            "POST", f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body}
        )
        if not isinstance(payload, dict) or "id" not in payload:
            raise GitHubApiError("POST comment returned an unexpected body", retryable=False)
        return int(payload["id"])

    # -- pull requests / checks (https://docs.github.com/en/rest/pulls, /checks) ----------

    async def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        repo = _check_allowed(self._allowed, repository)
        try:
            payload = await self._request("GET", f"/repos/{repo}/pulls/{number}")
        except GitHubApiError as exc:
            if exc.status_code == 404:
                raise PullRequestNotFound(repo, number) from exc
            raise
        if not isinstance(payload, dict) or "number" not in payload:
            raise PullRequestNotFound(repo, number)
        base = payload.get("base") or {}
        head = payload.get("head") or {}
        head_repo = (head.get("repo") or {}).get("full_name") or ""
        user = payload.get("user") or {}
        return PullRequestSnapshot(
            repository=repo,
            number=int(payload["number"]),
            html_url=str(payload.get("html_url", "")),
            state=str(payload.get("state", "")),
            draft=bool(payload.get("draft", False)),
            merged=bool(payload.get("merged", False)) or payload.get("merged_at") is not None,
            title=str(payload.get("title") or ""),
            body=str(payload.get("body") or ""),
            base_ref=str(base.get("ref", "")),
            base_sha=str(base.get("sha", "")),
            head_ref=str(head.get("ref", "")),
            head_sha=str(head.get("sha", "")),
            head_repository=str(head_repo).lower(),
            author_login=str(user.get("login", "")),
            author_type=str(user.get("type", "")),
        )

    async def list_pull_request_files(self, repository: str, number: int) -> tuple[str, ...]:
        repo = _check_allowed(self._allowed, repository)
        files: list[str] = []
        for page in range(1, 31):
            payload = await self._request(
                "GET", f"/repos/{repo}/pulls/{number}/files?per_page=100&page={page}"
            )
            if not isinstance(payload, list):
                break
            for item in payload:
                if isinstance(item, dict) and item.get("filename"):
                    files.append(str(item["filename"]))
                    previous = item.get("previous_filename")
                    if previous:
                        files.append(str(previous))
            if len(payload) < 100:
                break
        return tuple(files)

    async def compare_commits(self, repository: str, base: str, head: str) -> CompareResult:
        repo = _check_allowed(self._allowed, repository)
        payload = await self._request("GET", f"/repos/{repo}/compare/{base}...{head}?per_page=1")
        if not isinstance(payload, dict) or "status" not in payload:
            raise GitHubApiError("compare returned an unexpected body", retryable=False)
        merge_base = payload.get("merge_base_commit") or {}
        return CompareResult(
            status=str(payload["status"]),
            ahead_by=int(payload.get("ahead_by", 0)),
            behind_by=int(payload.get("behind_by", 0)),
            merge_base_sha=str(merge_base["sha"]) if merge_base.get("sha") else None,
        )

    async def closing_issue_references(
        self, repository: str, number: int
    ) -> tuple[int, ...] | None:
        """GraphQL `PullRequest.closingIssuesReferences` (the same data GitHub's UI shows)."""
        repo = _check_allowed(self._allowed, repository)
        owner, _, name = repo.partition("/")
        query = (
            "query($owner:String!,$name:String!,$number:Int!){"
            "repository(owner:$owner,name:$name){pullRequest(number:$number){"
            "closingIssuesReferences(first:50){nodes{number repository{nameWithOwner}}}}}}"
        )
        try:
            payload = await self._request(
                "POST",
                "/graphql",
                json={
                    "query": query,
                    "variables": {"owner": owner, "name": name, "number": number},
                },
            )
        except GitHubApiError as exc:
            logger.warning("closingIssuesReferences unavailable for %s#%s: %s", repo, number, exc)
            return None
        try:
            nodes = payload["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
        except (KeyError, TypeError):
            return None
        if not isinstance(nodes, list):
            return None
        return tuple(
            int(node["number"])
            for node in nodes
            if isinstance(node, dict)
            and str((node.get("repository") or {}).get("nameWithOwner", "")).lower() == repo
        )

    async def pull_requests_referencing_issue(
        self, repository: str, issue_number: int
    ) -> tuple[int, ...] | None:
        """Issue timeline `cross-referenced` events whose source is a PR in the same repo."""
        repo = _check_allowed(self._allowed, repository)
        numbers: list[int] = []
        for page in range(1, 11):
            try:
                payload = await self._request(
                    "GET",
                    f"/repos/{repo}/issues/{issue_number}/timeline?per_page=100&page={page}",
                )
            except GitHubApiError as exc:
                logger.warning("timeline unavailable for %s#%s: %s", repo, issue_number, exc)
                return None
            if not isinstance(payload, list):
                return None
            for event in payload:
                if not isinstance(event, dict) or event.get("event") != "cross-referenced":
                    continue
                source_issue = (event.get("source") or {}).get("issue") or {}
                if not source_issue.get("pull_request"):
                    continue
                source_repo = str(
                    (source_issue.get("repository") or {}).get("full_name", "")
                ).lower()
                if source_repo == repo and source_issue.get("number") is not None:
                    numbers.append(int(source_issue["number"]))
            if len(payload) < 100:
                break
        return tuple(dict.fromkeys(numbers))

    async def list_check_runs(self, repository: str, sha: str) -> tuple[CheckRun, ...]:
        repo = _check_allowed(self._allowed, repository)
        runs: list[CheckRun] = []
        for page in range(1, 11):
            payload = await self._request(
                "GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}"
            )
            if not isinstance(payload, dict):
                break
            items = payload.get("check_runs") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                app = item.get("app") or {}
                runs.append(
                    CheckRun(
                        name=str(item.get("name", "")),
                        status=str(item.get("status", "")),
                        conclusion=(
                            str(item["conclusion"]) if item.get("conclusion") is not None else None
                        ),
                        html_url=str(item.get("html_url") or ""),
                        app_slug=str(app["slug"]) if app.get("slug") else None,
                    )
                )
            if len(items) < 100:
                break
        return tuple(runs)
