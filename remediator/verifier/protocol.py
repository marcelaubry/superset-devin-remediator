"""Wire types shared by the verifier service and the worker's RemoteProbeRunner.

Kept free of FastAPI and of `remediator.config` so the worker can import it without pulling
in the service, and the service can import it without any settings/`.env` machinery.

`verifier.v2` differences from v1:

* requests carry no script: the verifier loads the probe from its own read-only registry
  and refuses unless the registry content hashes match the snapshot the worker approved;
* every request is authenticated (HMAC over timestamp + body with a verifier-only key) and
  carries a `request_id` the verifier uses for idempotent replay;
* responses carry the repository/SHA/probe identity they were computed for, the stage of an
  infrastructure failure and the tool versions that were used.
"""

import hashlib
import hmac
import time
from dataclasses import asdict

from pydantic import BaseModel, ConfigDict, Field

from ..models import ProbeTarget
from ..probes.runner import ProbeRunResult

VERIFIER_PROTOCOL_VERSION = "verifier.v2"
KNOWN_TOOLS = ("git", "bash", "python3", "node", "npm", "yarn")

SIGNATURE_HEADER = "X-Verifier-Signature"
TIMESTAMP_HEADER = "X-Verifier-Timestamp"
MAX_CLOCK_SKEW_SECONDS = 300
MAX_REQUEST_BODY_BYTES = 64 * 1024


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def verify_signature(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float | None = None,
) -> str | None:
    """Why the request is not authentic, or None when it is."""
    if not secret:
        return "verifier has no shared secret configured"
    if not timestamp or not signature:
        return "missing signature headers"
    try:
        sent = int(timestamp)
    except ValueError:
        return "malformed timestamp"
    current = time.time() if now is None else now
    if abs(current - sent) > MAX_CLOCK_SKEW_SECONDS:
        return "timestamp outside the accepted window"
    if not hmac.compare_digest(sign(secret, timestamp, body), signature):
        return "signature mismatch"
    return None


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: str = VERIFIER_PROTOCOL_VERSION
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    issue_number: int = Field(ge=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    target: ProbeTarget
    probe_identifier: str = Field(min_length=1, max_length=255)
    script_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeout_seconds: int = Field(ge=1)
    max_output_bytes: int = Field(ge=1024, le=10_000_000)

    def fingerprint(self) -> str:
        """Digest of everything that decides what runs; bound to `request_id` for replays."""
        return hashlib.sha256(self.model_dump_json(exclude={"request_id"}).encode()).hexdigest()


class ProbeResponse(BaseModel):
    protocol_version: str = VERIFIER_PROTOCOL_VERSION
    request_id: str
    repository: str
    commit_sha: str
    probe_identifier: str
    script_hash: str
    runner_mode: str
    command_identity: str
    exit_code: int | None
    stdout: str
    stderr: str
    output_truncated: bool
    timed_out: bool
    duration_ms: int
    infrastructure_error: str | None = None
    failure_stage: str | None = None
    tool_versions: dict[str, str] = Field(default_factory=dict)
    replayed: bool = False

    @classmethod
    def from_result(cls, request: ProbeRequest, result: ProbeRunResult) -> "ProbeResponse":
        return cls(
            request_id=request.request_id,
            repository=request.repository,
            commit_sha=request.commit_sha,
            probe_identifier=request.probe_identifier,
            script_hash=request.script_hash,
            **asdict(result),
        )


class Isolation(BaseModel):
    """What the verifier can *observe* about its own confinement. Anything it cannot see
    is reported as None, never as enforced."""

    uid: int
    root_writable: bool
    no_new_privs: bool | None
    effective_capabilities: str | None
    pids_limit: int | None
    memory_limit_bytes: int | None
    cpu_quota: str | None
    docker_socket_present: bool
    credential_exposure: list[str]

    @property
    def credential_free(self) -> bool:
        return not self.credential_exposure and not self.root_writable and self.uid != 0

    def unenforced(self) -> list[str]:
        """Claimed properties this runtime demonstrably does not enforce."""
        problems: list[str] = []
        if self.uid == 0:
            problems.append("runs as root")
        if self.root_writable:
            problems.append("root filesystem is writable")
        if self.docker_socket_present:
            problems.append("docker socket is mounted")
        if self.no_new_privs is False:
            problems.append("no-new-privileges is not set")
        if self.effective_capabilities not in (None, "0000000000000000"):
            problems.append("effective capabilities are not empty")
        if self.pids_limit is None:
            problems.append("no pids limit visible in the cgroup")
        if self.memory_limit_bytes is None:
            problems.append("no memory limit visible in the cgroup")
        return problems


class Health(BaseModel):
    protocol_version: str = VERIFIER_PROTOCOL_VERSION
    credential_exposure: list[str]
    uid: int
    root_writable: bool
    tools: dict[str, bool]

    @property
    def credential_free(self) -> bool:
        return not self.credential_exposure and not self.root_writable and self.uid != 0


class Capabilities(BaseModel):
    protocol_version: str = VERIFIER_PROTOCOL_VERSION
    isolation: Isolation
    tools: dict[str, bool]
    tool_versions: dict[str, str]
    repository_allowlist: list[str]
    registry_present: bool
    max_concurrent: int
    max_timeout_seconds: int
    cache_enabled: bool
    in_flight: int
