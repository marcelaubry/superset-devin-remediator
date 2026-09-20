"""Credential-free probe verifier service.

This process is the *only* place the local probe runner executes. It is deployed as its own
container (docker/verifier/Dockerfile, the `verifier` service in docker-compose.yml) that:

* receives no `env_file` and no application secret - it does not even import
  `remediator.config`, so no `.env` can be loaded by accident;
* runs as a dedicated non-root UID on a read-only root filesystem with `/tmp` on a bounded
  tmpfs, `cap_drop: ALL`, `no-new-privileges`, and PID/memory limits;
* refuses to run a probe when `credential_exposure()` sees anything at all, and reports that
  state on `GET /health` so the worker can refuse to trust it.

The wire protocol is deliberately tiny: `POST /probe` takes a `ProbeRunSpec` and returns a
`ProbeRunResult`. The verifier decides nothing about the case; it only executes the already
snapshotted script against one exact commit.
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException

from ..probes.runner import LocalProbeRunner, ProbeResourceLimits, _which, credential_exposure
from .protocol import (
    KNOWN_TOOLS,
    VERIFIER_PROTOCOL_VERSION,
    Health,
    ProbeRequest,
    ProbeResponse,
)


class VerifierConfig:
    """Read from plain environment variables; never from `.env`."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        self.clone_url_format = env.get(
            "VERIFIER_CLONE_URL_FORMAT", "https://github.com/{repository}.git"
        )
        self.workspace_root = Path(env.get("VERIFIER_WORKSPACE_ROOT", "/tmp"))
        self.max_timeout_seconds = int(env.get("VERIFIER_MAX_TIMEOUT_SECONDS", "3600"))
        self.limits = ProbeResourceLimits(
            max_processes=_int_or_none(env.get("VERIFIER_MAX_PROCESSES", "256")),
            max_file_size_bytes=_int_or_none(
                env.get("VERIFIER_MAX_FILE_SIZE_BYTES", str(512 * 1024 * 1024))
            ),
            max_memory_bytes=_int_or_none(env.get("VERIFIER_MAX_MEMORY_BYTES", "")),
        )
        if not self.clone_url_format.startswith("https://"):
            raise ValueError("VERIFIER_CLONE_URL_FORMAT must be an https:// URL")
        if self.max_timeout_seconds <= 0:
            raise ValueError("VERIFIER_MAX_TIMEOUT_SECONDS must be positive")


def _int_or_none(raw: str | None) -> int | None:
    return int(raw) if raw else None


def health() -> Health:
    return Health(
        credential_exposure=credential_exposure(),
        uid=os.getuid(),
        root_writable=os.access("/", os.W_OK),
        tools={tool: _which(tool) is not None for tool in KNOWN_TOOLS},
    )


def create_app(config: VerifierConfig | None = None) -> FastAPI:
    cfg = config or VerifierConfig()
    runner = LocalProbeRunner(
        cfg.clone_url_format, workspace_root=cfg.workspace_root, limits=cfg.limits
    )
    app = FastAPI(title="remediator probe verifier", docs_url=None, redoc_url=None)

    @app.get("/health", response_model=Health)
    async def get_health() -> Health:
        return health()

    @app.post("/probe", response_model=ProbeResponse)
    async def run_probe(request: ProbeRequest) -> ProbeResponse:
        if request.protocol_version != VERIFIER_PROTOCOL_VERSION:
            raise HTTPException(status_code=400, detail="unsupported protocol_version")
        return ProbeResponse.from_result(await runner.run(request.to_spec(cfg.max_timeout_seconds)))

    return app
