"""GitHub Issues adapters used by the outbox dispatcher.

Every method enforces the repository allowlist before any network call. The live client is
a thin REST wrapper (https://docs.github.com/en/rest/issues): `GET /repos/{r}/issues/{n}`,
`POST /repos/{r}/issues/{n}/labels`, `POST /repos/{r}/issues/{n}/comments`.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

# Fixture issue numbers whose first label attempts fail in the fake client; used by
# scripts/simulate.py and the tests to exercise APPROVAL_DELIVERY_FAILED + operator retry.
FAKE_LABEL_FAILURE_ISSUES: frozenset[int] = frozenset({4688})


class GitHubApiError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


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


class GitHubIssuesClient(Protocol):
    async def get_issue(self, repository: str, issue_number: int) -> IssueSnapshot: ...

    async def add_label(self, repository: str, issue_number: int, label: str) -> LabelResult: ...

    async def create_comment(self, repository: str, issue_number: int, body: str) -> int: ...

    async def aclose(self) -> None: ...


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
        return self._next_comment_id


RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


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
        if response.status_code in RETRYABLE_STATUS_CODES:
            raise GitHubApiError(
                f"{method} {path}: HTTP {response.status_code}",
                status_code=response.status_code,
                retryable=True,
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

    async def create_comment(self, repository: str, issue_number: int, body: str) -> int:
        repo = _check_allowed(self._allowed, repository)
        payload = await self._request(
            "POST", f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body}
        )
        if not isinstance(payload, dict) or "id" not in payload:
            raise GitHubApiError("POST comment returned an unexpected body", retryable=False)
        return int(payload["id"])
