import hashlib
import re

from .client import DevinSession


class FakeDevinClient:
    def __init__(self, never_finish_issues: set[int] | None = None) -> None:
        self._sessions: dict[str, tuple[int, int, str, int]] = {}
        self._idempotency: dict[str, str] = {}
        self._never_finish_issues = never_finish_issues or set()

    async def create_session(
        self, prompt: str, tags: dict[str, str], idempotency_key: str = ""
    ) -> DevinSession:
        issue_number = int(tags["issue_number"])
        kind = tags["kind"]
        repository = tags["repository"]
        if not idempotency_key:
            idempotency_key = f"{repository}:{issue_number}:{kind}"
        if idempotency_key in self._idempotency:
            return await self.get_session(self._idempotency[idempotency_key])
        digest = hashlib.sha256(f"{repository}:{issue_number}:{kind}".encode()).hexdigest()[:12]
        session_id = f"fake-{kind.lower()}-{issue_number}-{digest}"
        self._idempotency[idempotency_key] = session_id
        self._sessions[session_id] = (issue_number, 0, repository, issue_number)
        return DevinSession(session_id, f"https://app.devin.ai/sessions/{session_id}", "working")

    async def find_session(self, idempotency_key: str) -> DevinSession | None:
        session_id = self._idempotency.get(idempotency_key)
        if session_id is None:
            return None
        return DevinSession(session_id, f"https://app.devin.ai/sessions/{session_id}", "working")

    async def get_session(self, session_id: str) -> DevinSession:
        issue_number, count, repository, _ = self._sessions[session_id]
        count += 1
        self._sessions[session_id] = (issue_number, count, repository, issue_number)
        if issue_number in self._never_finish_issues:
            return DevinSession(
                session_id, f"https://app.devin.ai/sessions/{session_id}", "working"
            )
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
        output: dict[str, object]
        if match:
            output = {"pr_url": f"https://github.com/{repository}/pull/{9000 + issue_number}"}
        else:
            feasible = issue_number % 3 != 0
            output = {
                "remediation_feasible": feasible,
                "summary": (
                    "simulated triage: remediation feasible"
                    if feasible
                    else "simulated triage: requires product decision"
                ),
            }
        return DevinSession(
            session_id, f"https://app.devin.ai/sessions/{session_id}", "finished", output=output
        )

    async def send_message(self, session_id: str, text: str) -> None:
        return None

    async def terminate_session(self, session_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None
