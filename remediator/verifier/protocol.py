"""Wire types shared by the verifier service and the worker's RemoteProbeRunner.

Kept free of FastAPI and of `remediator.config` so the worker can import it without pulling
in the service, and the service can import it without any settings/`.env` machinery.
"""

from dataclasses import asdict

from pydantic import BaseModel, Field

from ..models import ProbeTarget
from ..probes.runner import ProbeRunResult, ProbeRunSpec

VERIFIER_PROTOCOL_VERSION = "verifier.v1"
KNOWN_TOOLS = ("git", "bash", "python3", "node", "npm", "yarn")


class ProbeRequest(BaseModel):
    protocol_version: str = VERIFIER_PROTOCOL_VERSION
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    issue_number: int = Field(ge=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    target: ProbeTarget
    probe_identifier: str = Field(min_length=1, max_length=255)
    script_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    script_content: str = Field(min_length=1, max_length=1_000_000)
    timeout_seconds: int = Field(ge=1)
    max_output_bytes: int = Field(ge=1024, le=10_000_000)
    required_tools: tuple[str, ...] = ()

    def to_spec(self, max_timeout_seconds: int) -> ProbeRunSpec:
        return ProbeRunSpec(
            repository=self.repository,
            issue_number=self.issue_number,
            commit_sha=self.commit_sha,
            target=self.target,
            probe_identifier=self.probe_identifier,
            script_hash=self.script_hash,
            script_content=self.script_content,
            timeout_seconds=min(self.timeout_seconds, max_timeout_seconds),
            max_output_bytes=self.max_output_bytes,
            required_tools=self.required_tools,
        )


class ProbeResponse(BaseModel):
    protocol_version: str = VERIFIER_PROTOCOL_VERSION
    runner_mode: str
    command_identity: str
    exit_code: int | None
    stdout: str
    stderr: str
    output_truncated: bool
    timed_out: bool
    duration_ms: int
    infrastructure_error: str | None = None

    @classmethod
    def from_result(cls, result: ProbeRunResult) -> "ProbeResponse":
        return cls(**asdict(result))


class Health(BaseModel):
    protocol_version: str = VERIFIER_PROTOCOL_VERSION
    credential_exposure: list[str]
    uid: int
    root_writable: bool
    tools: dict[str, bool]

    @property
    def credential_free(self) -> bool:
        return not self.credential_exposure and not self.root_writable and self.uid != 0
