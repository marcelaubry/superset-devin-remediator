"""Probe executor: the credential-free process that actually runs repository code.

Deployed as its own container (`verifier-runner` in docker-compose.yml, built from
docker/verifier/Dockerfile) that holds *no* secret at all - not even the verifier HMAC key,
which stays in the key-holding front (`remediator.verifier`). Repository code executed here
runs under the same UID as this process and can therefore read anything this process can;
that is exactly why nothing worth reading is here.

Trust boundary
--------------
The executor is reachable only from the verifier front over an internal Docker network and
speaks an unauthenticated protocol: whoever can reach `POST /run` can ask for a probe run.
That set is the front and - while a probe is running - the probe itself (loopback). A probe
gains nothing by it: it can only name an allowlisted repository, an exact commit and a probe
that must already exist in the read-only registry with the approved hashes, the single slot
is occupied by the probe's own run, and every process of the probe UID is killed when that
run ends. The front owns idempotency and result identity, so a probe cannot forge a verdict
for the request that spawned it.

Egress
------
Probe children receive `HTTPS_PROXY`/`HTTP_PROXY` pointing at the egress proxy when
`VERIFIER_EGRESS_PROXY_URL` is set; with the container on an `internal` network that proxy is
the only route out. `GET /health` reports `direct_egress`, a bounded TCP spot check towards
public addresses made without the proxy, so the worker and readiness can fail closed when a
deployment left egress open.
"""

import asyncio
import logging
import os
import socket
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..probes.registry import (
    ProbeRegistryError,
    load_approved_probe,
    manifest_cache_inputs,
    manifest_setup_steps,
    manifest_setup_timeout,
)
from ..probes.runner import (
    LocalProbeRunner,
    ProbeResourceLimits,
    ProbeRunSpec,
    _which,
    credential_exposure,
    kill_stray_processes,
    tool_versions,
)
from .protocol import (
    KNOWN_TOOLS,
    MAX_REQUEST_BODY_BYTES,
    VERIFIER_PROTOCOL_VERSION,
    Isolation,
    ProbeRequest,
    ProbeResponse,
    RunnerHealth,
)

logger = logging.getLogger(__name__)

_CGROUP_ROOT = Path("/sys/fs/cgroup")
# Public anycast resolvers: reachable from any host with unrestricted egress, without DNS.
_EGRESS_PROBE_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 443))
_EGRESS_PROBE_TIMEOUT_SECONDS = 2.0
_EGRESS_CACHE_SECONDS = 60.0


class ExecutorConfig:
    """Read from plain environment variables; never from `.env`. Holds no secret."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        env = dict(os.environ if environ is None else environ)
        self.clone_url_format = env.get(
            "VERIFIER_CLONE_URL_FORMAT", "https://github.com/{repository}.git"
        )
        self.workspace_root = Path(env.get("VERIFIER_WORKSPACE_ROOT", "/tmp"))
        self.probe_root = Path(env.get("VERIFIER_PROBE_ROOT", "/probes"))
        cache = env.get("VERIFIER_CACHE_ROOT", "")
        self.cache_root = Path(cache) if cache else None
        self.max_timeout_seconds = int(env.get("VERIFIER_MAX_TIMEOUT_SECONDS", "3600"))
        self.max_concurrent = int(env.get("VERIFIER_MAX_CONCURRENT", "1"))
        allow = env.get("VERIFIER_REPOSITORY_ALLOWLIST", "")
        self.repository_allowlist = tuple(
            sorted({item.strip() for item in allow.split(",") if item.strip()})
        )
        self.limits = ProbeResourceLimits(
            max_processes=_int_or_none(env.get("VERIFIER_MAX_PROCESSES", "256")),
            max_file_size_bytes=_int_or_none(
                env.get("VERIFIER_MAX_FILE_SIZE_BYTES", str(512 * 1024 * 1024))
            ),
            max_memory_bytes=_int_or_none(env.get("VERIFIER_MAX_MEMORY_BYTES", "")),
        )
        self.egress_proxy_url = env.get("VERIFIER_EGRESS_PROXY_URL", "").strip()
        self.egress_check = env.get("VERIFIER_EGRESS_CHECK", "on").strip().lower() != "off"
        if not self.clone_url_format.startswith("https://"):
            raise ValueError("VERIFIER_CLONE_URL_FORMAT must be an https:// URL")
        if self.max_timeout_seconds <= 0:
            raise ValueError("VERIFIER_MAX_TIMEOUT_SECONDS must be positive")
        if self.max_concurrent != 1:
            # The post-run sweep kills every process of the probe UID; a second in-flight
            # run would be killed with it and misreported as a product verdict.
            raise ValueError(
                "VERIFIER_MAX_CONCURRENT must be 1: the UID-wide process sweep after each "
                "run cannot tell a concurrent probe from an escapee"
            )
        if not self.repository_allowlist:
            raise ValueError("VERIFIER_REPOSITORY_ALLOWLIST must name at least one owner/repo")
        for repo in self.repository_allowlist:
            owner, _, name = repo.partition("/")
            if not owner or not name or "/" in name:
                raise ValueError(f"VERIFIER_REPOSITORY_ALLOWLIST entry {repo!r} is not owner/repo")
        if self.egress_proxy_url and not self.egress_proxy_url.startswith("http://"):
            raise ValueError("VERIFIER_EGRESS_PROXY_URL must be an http:// proxy URL")

    def child_env(self) -> dict[str, str]:
        """Extra variables every probe child receives (proxy routing only, never a secret)."""
        if not self.egress_proxy_url:
            return {}
        proxy = self.egress_proxy_url
        return {
            "HTTPS_PROXY": proxy,
            "https_proxy": proxy,
            "HTTP_PROXY": proxy,
            "http_proxy": proxy,
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
        }

    def build_runner(self) -> LocalProbeRunner:
        return LocalProbeRunner(
            self.clone_url_format,
            workspace_root=self.workspace_root,
            limits=self.limits,
            cache_root=self.cache_root,
            extra_env=self.child_env(),
        )


def _int_or_none(raw: str | None) -> int | None:
    return int(raw) if raw else None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _cgroup_int(name: str) -> int | None:
    raw = _read_text(_CGROUP_ROOT / name)
    if raw is None or raw == "max":
        return None
    try:
        return int(raw.split()[0])
    except ValueError:
        return None


def _proc_status_field(field: str) -> str | None:
    status = _read_text(Path("/proc/self/status"))
    if status is None:
        return None
    for line in status.splitlines():
        if line.startswith(field + ":"):
            return line.split(":", 1)[1].strip()
    return None


def direct_egress_possible() -> bool:
    """True when a TCP connection to a public address succeeds without any proxy."""
    for host, port in _EGRESS_PROBE_TARGETS:
        try:
            with socket.create_connection((host, port), timeout=_EGRESS_PROBE_TIMEOUT_SECONDS):
                return True
        except OSError:
            continue
    return False


class _EgressState:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._cached: tuple[float, bool] | None = None

    async def current(self) -> bool | None:
        if not self.enabled:
            return None
        cached = self._cached
        if cached and time.monotonic() - cached[0] < _EGRESS_CACHE_SECONDS:
            return cached[1]
        value = await asyncio.to_thread(direct_egress_possible)
        self._cached = (time.monotonic(), value)
        return value


def isolation(
    *,
    direct_egress: bool | None = None,
    egress_proxy_configured: bool = False,
    allow_own_secret: bool = False,
) -> Isolation:
    nnp = _proc_status_field("NoNewPrivs")
    cpu = _read_text(_CGROUP_ROOT / "cpu.max")
    return Isolation(
        uid=os.getuid(),
        root_writable=os.access("/", os.W_OK),
        no_new_privs=None if nnp is None else nnp == "1",
        effective_capabilities=_proc_status_field("CapEff"),
        pids_limit=_cgroup_int("pids.max"),
        memory_limit_bytes=_cgroup_int("memory.max"),
        cpu_quota=None if cpu in (None, "max 100000") else cpu,
        docker_socket_present=Path("/var/run/docker.sock").exists(),
        credential_exposure=credential_exposure(allow_own_secret=allow_own_secret),
        direct_egress=direct_egress,
        egress_proxy_configured=egress_proxy_configured,
    )


async def spec_for(
    request: ProbeRequest,
    *,
    probe_root: Path,
    repository_allowlist: tuple[str, ...],
    max_timeout_seconds: int,
) -> ProbeRunSpec:
    """Bind the request to the registry's approved content, or refuse."""
    if request.repository not in repository_allowlist:
        raise HTTPException(status_code=403, detail="repository is not allowlisted")
    try:
        probe = await load_approved_probe(
            probe_root, request.repository, request.issue_number, allow_smoke=True
        )
    except ProbeRegistryError as exc:
        logger.warning("registry refused %s: %s", request.probe_identifier, exc)
        raise HTTPException(
            status_code=422, detail="no approved probe in the verifier registry"
        ) from exc
    if probe.script_hash != request.script_hash or probe.manifest_hash != request.manifest_hash:
        raise HTTPException(
            status_code=409, detail="registry probe does not match the approved snapshot"
        )
    if probe.identifier != request.probe_identifier:
        raise HTTPException(status_code=409, detail="probe identifier mismatch")
    return ProbeRunSpec(
        repository=probe.repository,
        issue_number=probe.issue_number,
        commit_sha=request.commit_sha,
        target=request.target,
        probe_identifier=probe.identifier,
        script_hash=probe.script_hash,
        script_content=probe.script_content,
        timeout_seconds=min(request.timeout_seconds, probe.timeout_seconds, max_timeout_seconds),
        max_output_bytes=request.max_output_bytes,
        required_tools=probe.required_tools,
        setup_steps=manifest_setup_steps(probe.manifest),
        setup_timeout_seconds=min(manifest_setup_timeout(probe.manifest), max_timeout_seconds),
        cache_inputs=manifest_cache_inputs(probe.manifest),
    )


async def parse_probe_request(body: bytes) -> ProbeRequest:
    if len(body) > MAX_REQUEST_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request body too large")
    try:
        parsed = ProbeRequest.model_validate_json(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="malformed probe request") from exc
    if parsed.protocol_version != VERIFIER_PROTOCOL_VERSION:
        raise HTTPException(status_code=400, detail="unsupported protocol_version")
    return parsed


def create_executor_app(config: ExecutorConfig | None = None) -> FastAPI:
    cfg = config or ExecutorConfig()
    runner = cfg.build_runner()
    app = FastAPI(title="remediator probe executor", docs_url=None, redoc_url=None)
    slot = asyncio.Semaphore(1)
    in_flight = {"count": 0}
    egress = _EgressState(cfg.egress_check)

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/health", response_model=RunnerHealth)
    async def get_health() -> RunnerHealth:
        return RunnerHealth(
            isolation=isolation(
                direct_egress=await egress.current(),
                egress_proxy_configured=bool(cfg.egress_proxy_url),
            ),
            tools={tool: _which(tool) is not None for tool in KNOWN_TOOLS},
            tool_versions=await tool_versions(),
            repository_allowlist=list(cfg.repository_allowlist),
            registry_present=cfg.probe_root.is_dir(),
            cache_enabled=cfg.cache_root is not None,
            workspace_writable=os.access(cfg.workspace_root, os.W_OK),
            in_flight=in_flight["count"],
        )

    @app.post("/run", response_model=ProbeResponse)
    async def run_probe(request: Request) -> ProbeResponse:
        parsed = await parse_probe_request(await request.body())
        spec = await spec_for(
            parsed,
            probe_root=cfg.probe_root,
            repository_allowlist=cfg.repository_allowlist,
            max_timeout_seconds=cfg.max_timeout_seconds,
        )
        if slot.locked():
            raise HTTPException(status_code=409, detail="verifier is at capacity")
        async with slot:
            in_flight["count"] += 1
            try:
                result = await runner.run(spec)
            finally:
                in_flight["count"] -= 1
                await asyncio.to_thread(kill_stray_processes)
        return ProbeResponse.from_result(parsed, result)

    return app


def main() -> None:
    uvicorn.run(
        create_executor_app(),
        host=os.environ.get("VERIFIER_HOST", "0.0.0.0"),
        port=int(os.environ.get("VERIFIER_PORT", "8081")),
        log_level="info",
        # See remediator.verifier.__main__: uvloop keeps detached descendants' stdio alive.
        loop="asyncio",
    )


if __name__ == "__main__":
    main()
