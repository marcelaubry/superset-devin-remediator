from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class DevinError(Exception):
    """Base class for Devin client failures."""


class DevinApiError(DevinError):
    """The API answered definitively with an error response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Devin API {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class DevinTransportError(DevinError):
    """The request failed or timed out; the server outcome is unknown."""


class DevinSessionNotFound(DevinApiError):
    def __init__(self, session_id: str) -> None:
        super().__init__(404, f"session {session_id} not found")
        self.session_id = session_id


@dataclass(frozen=True)
class CreateSessionRequest:
    prompt: str
    repository: str
    base_sha: str
    max_acu_limit: int
    operation_key: str
    tags: tuple[str, ...]
    structured_output_schema: dict[str, Any]
    title: str | None = None

    def all_tags(self) -> list[str]:
        ordered: list[str] = [self.operation_key]
        for tag in self.tags:
            if tag not in ordered:
                ordered.append(tag)
        return ordered


@dataclass(frozen=True)
class SessionSnapshot:
    """A point-in-time view of a Devin session as returned by the v3 API."""

    session_id: str
    url: str
    status: str
    status_detail: str | None = None
    tags: tuple[str, ...] = ()
    structured_output: dict[str, Any] | None = None
    acus_consumed: float | None = None
    updated_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


class DevinClient(Protocol):
    mode: str

    async def create_session(self, request: CreateSessionRequest) -> SessionSnapshot: ...

    async def find_sessions_by_tag(self, tag: str) -> list[SessionSnapshot]: ...

    async def get_session(self, session_id: str) -> SessionSnapshot: ...

    async def terminate_session(self, session_id: str) -> SessionSnapshot | None: ...

    async def aclose(self) -> None: ...
