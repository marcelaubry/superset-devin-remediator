"""Resolves the exact base commit SHA that a triage session must check out."""

import hashlib
import re
from typing import Protocol

import httpx

from .config import Settings

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class BaseCommitResolutionError(RuntimeError):
    pass


class BaseCommitResolver(Protocol):
    async def resolve(self, repository: str, ref: str) -> str: ...

    async def aclose(self) -> None: ...


class FakeBaseCommitResolver:
    async def resolve(self, repository: str, ref: str) -> str:
        return hashlib.sha1(f"{repository}@{ref}".encode()).hexdigest()

    async def aclose(self) -> None:
        return None


class GitHubBaseCommitResolver:
    def __init__(self, base_url: str, token: str | None, timeout_seconds: float = 15.0) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_seconds
        )

    async def resolve(self, repository: str, ref: str) -> str:
        try:
            response = await self._http.get(f"/repos/{repository}/commits/{ref}")
        except httpx.HTTPError as exc:
            raise BaseCommitResolutionError(
                f"GitHub commit lookup failed: {exc.__class__.__name__}"
            ) from exc
        if response.status_code >= 400:
            raise BaseCommitResolutionError(f"GitHub commit lookup returned {response.status_code}")
        try:
            sha = response.json().get("sha")
        except (ValueError, AttributeError) as exc:
            raise BaseCommitResolutionError("GitHub commit lookup returned no JSON") from exc
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise BaseCommitResolutionError("GitHub commit lookup returned no full SHA")
        return sha

    async def aclose(self) -> None:
        await self._http.aclose()


def build_base_commit_resolver(settings: Settings) -> BaseCommitResolver:
    if not settings.live_mode:
        return FakeBaseCommitResolver()
    token = settings.github_api_token.get_secret_value() if settings.github_api_token else None
    return GitHubBaseCommitResolver(settings.github_api_base_url, token)
