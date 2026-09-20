"""Worker-side probe runner that delegates to the credential-free verifier container.

The worker holds every application credential, so it never runs repository code itself.
Before each probe it asks the verifier for `GET /health` and refuses (as an *infrastructure*
failure, never a verdict) when the verifier is unreachable, speaks another protocol, runs as
root or on a writable root filesystem, or can see any credential-shaped variable or secret
path. Only then is the snapshotted spec forwarded to `POST /probe`.
"""

import time
from dataclasses import asdict

import httpx

from ..verifier.protocol import VERIFIER_PROTOCOL_VERSION, Health, ProbeResponse
from .runner import ProbeRunResult, ProbeRunSpec, command_identity

_HEALTH_TIMEOUT_SECONDS = 10.0
_REQUEST_OVERHEAD_SECONDS = 120.0


class RemoteProbeRunner:
    mode = "remote"

    def __init__(self, verifier_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not verifier_url.startswith(("http://", "https://")):
            raise ValueError("verifier_url must be an http(s) URL")
        self.verifier_url = verifier_url.rstrip("/")
        self._client = client or httpx.AsyncClient(base_url=self.verifier_url)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> Health | str:
        """The verifier's self-report, or a reason string when it cannot be trusted."""
        try:
            response = await self._client.get(
                f"{self.verifier_url}/health", timeout=_HEALTH_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            return f"verifier unreachable: {exc.__class__.__name__}"
        if response.status_code != 200:
            return f"verifier health returned HTTP {response.status_code}"
        try:
            health = Health.model_validate(response.json())
        except ValueError as exc:
            return f"verifier health is malformed: {exc.__class__.__name__}"
        if health.protocol_version != VERIFIER_PROTOCOL_VERSION:
            return (
                f"verifier speaks {health.protocol_version}, expected {VERIFIER_PROTOCOL_VERSION}"
            )
        if health.credential_exposure:
            return f"verifier can see credentials {health.credential_exposure}; refusing to use it"
        if health.uid == 0:
            return "verifier runs as root; refusing to use it"
        if health.root_writable:
            return "verifier root filesystem is writable; refusing to use it"
        return health

    async def run(self, spec: ProbeRunSpec) -> ProbeRunResult:
        identity = command_identity(spec)
        started = time.monotonic()
        health = await self.health()
        if isinstance(health, str):
            return self._infra(identity, started, health)
        payload = {"protocol_version": VERIFIER_PROTOCOL_VERSION, **asdict(spec)}
        payload["target"] = spec.target.value
        try:
            response = await self._client.post(
                f"{self.verifier_url}/probe",
                json=payload,
                timeout=spec.timeout_seconds + _REQUEST_OVERHEAD_SECONDS,
            )
        except httpx.HTTPError as exc:
            return self._infra(
                identity, started, f"verifier request failed: {exc.__class__.__name__}"
            )
        if response.status_code != 200:
            return self._infra(identity, started, f"verifier returned HTTP {response.status_code}")
        try:
            body = ProbeResponse.model_validate(response.json())
        except ValueError as exc:
            return self._infra(
                identity, started, f"verifier response malformed: {exc.__class__.__name__}"
            )
        if body.command_identity != identity:
            return self._infra(
                identity, started, "verifier reported a different command identity than requested"
            )
        return ProbeRunResult(
            runner_mode=self.mode,
            command_identity=identity,
            exit_code=body.exit_code,
            stdout=body.stdout,
            stderr=body.stderr,
            output_truncated=body.output_truncated,
            timed_out=body.timed_out,
            duration_ms=body.duration_ms,
            infrastructure_error=body.infrastructure_error,
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
        )
