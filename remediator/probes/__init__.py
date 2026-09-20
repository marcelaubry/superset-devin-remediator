from .remote import RemoteProbeRunner, VerifierBusyError
from .runner import FakeProbeRunner, ProbeRunner, ProbeRunResult, ProbeRunSpec

__all__ = [
    "FakeProbeRunner",
    "ProbeRunResult",
    "ProbeRunSpec",
    "ProbeRunner",
    "RemoteProbeRunner",
    "VerifierBusyError",
    "build_probe_runner",
]


def build_probe_runner(
    mode: str,
    verifier_url: str | None,
    shared_secret: str | None = None,
    *,
    require_isolation: bool = True,
    request_timeout_seconds: float = 30.0,
) -> ProbeRunner:
    """Worker-side factory. `local` is intentionally not offered: the worker holds
    credentials, so repository code only ever runs inside the verifier container."""
    if mode == "remote":
        if not verifier_url:
            raise ValueError("PROBE_RUNNER_MODE=remote requires PROBE_VERIFIER_URL")
        if not shared_secret:
            raise ValueError("PROBE_RUNNER_MODE=remote requires PROBE_VERIFIER_SHARED_SECRET")
        return RemoteProbeRunner(
            verifier_url,
            shared_secret,
            require_isolation=require_isolation,
            request_timeout_seconds=request_timeout_seconds,
        )
    if mode == "fake":
        return FakeProbeRunner()
    raise ValueError(f"unsupported PROBE_RUNNER_MODE {mode!r} for the worker")
