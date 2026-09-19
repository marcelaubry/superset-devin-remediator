from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DevinSession:
    session_id: str
    url: str
    status: str
    output: dict[str, object] | None = None
    error: str | None = None


class DevinClient(Protocol):
    async def create_session(
        self, prompt: str, tags: dict[str, str], idempotency_key: str
    ) -> DevinSession: ...
    async def find_session(self, idempotency_key: str) -> DevinSession | None: ...
    async def get_session(self, session_id: str) -> DevinSession: ...
    async def send_message(self, session_id: str, text: str) -> None: ...
    async def terminate_session(self, session_id: str) -> None: ...
    async def aclose(self) -> None: ...
