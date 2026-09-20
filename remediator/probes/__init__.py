"""Immutable probe registry and independent probe runners (Phase 4)."""

from .remote import RemoteProbeRunner
from .runner import FakeProbeRunner, ProbeRunner


def build_probe_runner(mode: str, verifier_url: str | None) -> ProbeRunner:
    """Worker-side factory. `local` is intentionally not offered: the worker holds
    credentials, so repository code only ever runs inside the verifier container."""
    if mode == "remote":
        if not verifier_url:
            raise ValueError("PROBE_RUNNER_MODE=remote requires PROBE_VERIFIER_URL")
        return RemoteProbeRunner(verifier_url)
    if mode == "fake":
        return FakeProbeRunner()
    raise ValueError(f"unsupported PROBE_RUNNER_MODE {mode!r} for the worker")
