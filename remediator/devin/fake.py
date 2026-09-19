import hashlib
import re

from .client import DevinSession


class FakeDevinClient:
    def __init__(self) -> None:
        self._sessions: dict[str, tuple[int, int, str, int]] = {}

    async def create_session(self, prompt: str, tags: dict[str, str]) -> DevinSession:
        issue_number = int(tags["issue_number"])
        kind = tags["kind"]
        repository = tags["repository"]
        digest = hashlib.sha256(f"{repository}:{issue_number}:{kind}".encode()).hexdigest()[:12]
        session_id = f"fake-{kind.lower()}-{issue_number}-{digest}"
        self._sessions[session_id] = (issue_number, 0, repository, issue_number)
        return DevinSession(session_id, f"https://app.devin.ai/sessions/{session_id}", "working")

    async def get_session(self, session_id: str) -> DevinSession:
        issue_number, count, repository, _ = self._sessions[session_id]
        count += 1
        self._sessions[session_id] = (issue_number, count, repository, issue_number)
        if issue_number % 5 == 0:
            return DevinSession(
                session_id,
                f"https://app.devin.ai/sessions/{session_id}",
                "failed",
                error="simulated Devin failure",
            )
        if issue_number % 7 == 0:
            return DevinSession(
                session_id, f"https://app.devin.ai/sessions/{session_id}", "blocked"
            )
        if count < 3:
            return DevinSession(
                session_id, f"https://app.devin.ai/sessions/{session_id}", "working"
            )
        match = re.search(r"remediation", session_id)
        output: dict[str, object] | None = (
            {"pr_url": f"https://github.com/{repository}/pull/{9000 + issue_number}"}
            if match
            else None
        )
        return DevinSession(
            session_id, f"https://app.devin.ai/sessions/{session_id}", "finished", output=output
        )

    async def send_message(self, session_id: str, text: str) -> None:
        return None

    async def terminate_session(self, session_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None
