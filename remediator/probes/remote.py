"""Worker-side probe runner that delegates to the credential-free verifier container.

The worker holds every application credential, so it never runs repository code itself.
Before each probe it asks the verifier for its authenticated `GET /capabilities` and
refuses (as an *infrastructure* failure, never a verdict) when the verifier is unreachable,
speaks another protocol, runs as root or on a writable root filesystem, can see any
credential-shaped variable or secret path, or - when `require_isolation` is on, the default
outside tests - cannot show that `no-new-privileges`, empty capabilities and PID/memory
limits are actually enforced. Only then is the approved snapshot *named* to `POST /probe`:
the request carries hashes and identities, never the script.

Requests are signed with the verifier HMAC key (`PROBE_VERIFIER_SHARED_SECRET`), a secret
that authorises nothing except probe requests against the verifier's own allowlist and
registry. Provider credentials never leave the worker.
"""

import time
import uuid

import httpx

from ..verifier.protocol import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    VERIFIER_PROTOCOL_VERSION,
    Capabilities,
    ProbeResponse,
    sign,
)
from .runner import STAGE_PREFLIGHT, ProbeRunResult, ProbeRunSpec, command_identity

_HEALTH_TIMEOUT_SECONDS = 10.0
_REQUEST_OVERHEAD_SECONDS = 120.0
_CAPABILITIES_TTL_SECONDS = 30.0


class VerifierBusyError(Exception):
    """The verifier serializes probes and is occupied; the caller should retry later without
    recording any verdict (not even an infrastructure failure)."""


class RemoteProbeRunner:
    mode = "remote"

    def __init__(
        self,
        verifier_url: str,
        shared_secret: str,
        *,
        client: httpx.AsyncClient | None = None,
        require_isolation: bool = True,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        if not verifier_url.startswith(("http://", "https://")):
            raise ValueError("verifier_url must be an http(s) URL")
        if len(shared_secret) < 32:
            raise ValueError("verifier shared secret must hold at least 32 characters")
        self.verifier_url = verifier_url.rstrip("/")
        self._secret = shared_secret
        self.require_isolation = require_isolation
        self.request_timeout_seconds = request_timeout_seconds
        self._client = client or httpx.AsyncClient(base_url=self.verifier_url)
        self._capabilities: tuple[float, Capabilities] | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, body: bytes) -> dict[str, str]:
        stamp = str(int(time.time()))
        return {
            TIMESTAMP_HEADER: stamp,
            SIGNATURE_HEADER: sign(self._secret, stamp, body),
            "Content-Type": "application/json",
        }

    async def capabilities(self, *, fresh: bool = False) -> Capabilities | str:
        """The verifier's authenticated self-report, or a reason string when it cannot be
        trusted. Cached briefly so a probe does not double the verifier's request load."""
        cached = self._capabilities
        if cached and not fresh and time.monotonic() - cached[0] < _CAPABILITIES_TTL_SECONDS:
            return cached[1]
        try:
            response = await self._client.get(
                f"{self.verifier_url}/capabilities",
                headers=self._headers(b""),
                timeout=self.request_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            return f"verifier unreachable: {exc.__class__.__name__}"
        if response.status_code == 401:
            return "verifier rejected our signature; check PROBE_VERIFIER_SHARED_SECRET"
        if response.status_code != 200:
            return f"verifier capabilities returned HTTP {response.status_code}"
        try:
            caps = Capabilities.model_validate(response.json())
        except ValueError as exc:
            return f"verifier capabilities malformed: {exc.__class__.__name__}"
        if caps.protocol_version != VERIFIER_PROTOCOL_VERSION:
            return f"verifier speaks {caps.protocol_version}, expected {VERIFIER_PROTOCOL_VERSION}"
        iso = caps.isolation
        if iso.credential_exposure:
            return f"verifier can see credentials {iso.credential_exposure}; refusing to use it"
        if iso.uid == 0:
            return "verifier runs as root; refusing to use it"
        if iso.root_writable:
            return "verifier root filesystem is writable; refusing to use it"
        if not caps.registry_present:
            return "verifier has no probe registry mounted"
        if self.require_isolation:
            problems = iso.unenforced()
            if problems:
                return "verifier isolation not enforced: " + "; ".join(problems)
        self._capabilities = (time.monotonic(), caps)
        return caps

    async def health(self) -> Capabilities | str:
        return await self.capabilities(fresh=True)

    async def run(self, spec: ProbeRunSpec) -> ProbeRunResult:
        identity = command_identity(spec)
        started = time.monotonic()
        caps = await self.capabilities()
        if isinstance(caps, str):
            return self._infra(identity, started, caps)
        if spec.repository not in caps.repository_allowlist:
            return self._infra(identity, started, "repository is not in the verifier allowlist")
        missing = [tool for tool in spec.required_tools if not caps.tools.get(tool)]
        if missing:
            return self._infra(identity, started, f"verifier lacks required tools: {missing}")
        if not spec.manifest_hash or not spec.request_id:
            return self._infra(identity, started, "spec lacks manifest_hash/request_id")
        payload = {
            "protocol_version": VERIFIER_PROTOCOL_VERSION,
            "request_id": spec.request_id,
            "repository": spec.repository,
            "issue_number": spec.issue_number,
            "commit_sha": spec.commit_sha,
            "target": spec.target.value,
            "probe_identifier": spec.probe_identifier,
            "script_hash": spec.script_hash,
            "manifest_hash": spec.manifest_hash,
            "timeout_seconds": spec.timeout_seconds,
            "max_output_bytes": spec.max_output_bytes,
        }
        body = httpx.Request("POST", "http://x/", json=payload).content
        try:
            response = await self._client.post(
                f"{self.verifier_url}/probe",
                content=body,
                headers=self._headers(body),
                timeout=spec.timeout_seconds
                + spec.setup_timeout_seconds
                + _REQUEST_OVERHEAD_SECONDS,
            )
        except httpx.HTTPError as exc:
            return self._infra(
                identity, started, f"verifier request failed: {exc.__class__.__name__}"
            )
        if response.status_code == 409 and _detail(response) == "verifier is at capacity":
            raise VerifierBusyError("verifier is running another probe")
        if response.status_code != 200:
            return self._infra(
                identity,
                started,
                f"verifier returned HTTP {response.status_code}: {_detail(response)}",
            )
        try:
            result = ProbeResponse.model_validate(response.json())
        except ValueError as exc:
            return self._infra(
                identity, started, f"verifier response malformed: {exc.__class__.__name__}"
            )
        if (
            result.command_identity != identity
            or result.request_id != spec.request_id
            or result.repository != spec.repository
            or result.commit_sha != spec.commit_sha
            or result.script_hash != spec.script_hash
        ):
            return self._infra(
                identity, started, "verifier reported a different execution than requested"
            )
        return ProbeRunResult(
            runner_mode=self.mode,
            command_identity=identity,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            output_truncated=result.output_truncated,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
            infrastructure_error=result.infrastructure_error,
            failure_stage=result.failure_stage,
            tool_versions=dict(result.tool_versions),
        )

    def _infra(self, identity: str, started: float, reason: str) -> ProbeRunResult:
        return ProbeRunResult(
            runner_mode=self.mode,
            command_identity=identity,
            exit_code=None,
            stdout="",
            stderr="",
            output_truncated=False,
            timed_out=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            infrastructure_error=reason,
            failure_stage=STAGE_PREFLIGHT,
        )


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return str(detail)[:200] if isinstance(detail, str) else ""


def new_request_id() -> str:
    return uuid.uuid4().hex
