"""Credential-free probe verifier service (protocol `verifier.v2`).

This process is the *only* place the local probe runner executes. It is deployed as its own
container (docker/verifier/Dockerfile, the `verifier` service in docker-compose.yml) that:

* receives no `env_file` and no application secret - it does not even import
  `remediator.config`, so no `.env` can be loaded by accident. Its only secret is the
  verifier HMAC key, which authorises nothing but probe requests against this allowlist;
* runs as a dedicated non-root UID on a read-only root filesystem with `/tmp` on a bounded
  tmpfs, `cap_drop: ALL`, `no-new-privileges`, PID/memory/CPU limits;
* refuses to run a probe when `credential_exposure()` sees anything at all, and reports its
  observable confinement on `GET /capabilities` so the worker can refuse to trust it.

Trust model of a request
------------------------
Requests are untrusted even when authentic. The verifier decides nothing about the case;
it executes one probe from *its own* read-only registry against one exact commit of one
allowlisted repository. The request may only *name* the probe (repository, issue, script
hash, manifest hash); if the registry does not hold exactly that content the request is
refused. No script, URL, path or shell fragment is ever accepted from the network.

Idempotency
-----------
`request_id` identifies an execution. A repeated request with the same id returns the
stored result (`replayed: true`) instead of running again; a concurrent duplicate waits for
the in-flight run. Results are kept for a bounded time and count.

Concurrency
-----------
At most `VERIFIER_MAX_CONCURRENT` probes run at once (default 1); extra requests get `409`
which the worker treats as transient. After every run the stray-process sweep kills every
remaining process of the probe UID, so an escapee cannot tamper with the next workspace.
"""

import asyncio
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path

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
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    VERIFIER_PROTOCOL_VERSION,
    Capabilities,
    Health,
    Isolation,
    ProbeRequest,
    ProbeResponse,
    verify_signature,
)

logger = logging.getLogger(__name__)

HMAC_KEY_ENV = "VERIFIER_HMAC_KEY"
HMAC_KEY_FILE_ENV = "VERIFIER_HMAC_KEY_FILE"
_RESULT_TTL_SECONDS = 6 * 3600
_RESULT_MAX_ENTRIES = 512
_CGROUP_ROOT = Path("/sys/fs/cgroup")


def _read_hmac_key(env: dict[str, str]) -> str:
    """The key comes from a file (Docker secret / tmpfs) or, failing that, the environment;
    either way it is removed from `os.environ` so no child could inherit it."""
    path = env.get(HMAC_KEY_FILE_ENV)
    key = ""
    if path:
        try:
            key = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(
                f"{HMAC_KEY_FILE_ENV} is unreadable: {exc.__class__.__name__}"
            ) from exc
    if not key:
        key = env.get(HMAC_KEY_ENV, "").strip()
    os.environ.pop(HMAC_KEY_ENV, None)
    if len(key) < 32:
        raise ValueError(
            f"{HMAC_KEY_ENV} (or {HMAC_KEY_FILE_ENV}) must hold at least 32 characters"
        )
    return key


class VerifierConfig:
    """Read from plain environment variables; never from `.env`."""

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
        self.hmac_key = _read_hmac_key(env)
        if not self.clone_url_format.startswith("https://"):
            raise ValueError("VERIFIER_CLONE_URL_FORMAT must be an https:// URL")
        if self.max_timeout_seconds <= 0:
            raise ValueError("VERIFIER_MAX_TIMEOUT_SECONDS must be positive")
        if self.max_concurrent <= 0:
            raise ValueError("VERIFIER_MAX_CONCURRENT must be positive")
        if not self.repository_allowlist:
            raise ValueError("VERIFIER_REPOSITORY_ALLOWLIST must name at least one owner/repo")
        for repo in self.repository_allowlist:
            owner, _, name = repo.partition("/")
            if not owner or not name or "/" in name:
                raise ValueError(f"VERIFIER_REPOSITORY_ALLOWLIST entry {repo!r} is not owner/repo")


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


def isolation() -> Isolation:
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
        credential_exposure=credential_exposure(),
    )


def health() -> Health:
    return Health(
        credential_exposure=credential_exposure(),
        uid=os.getuid(),
        root_writable=os.access("/", os.W_OK),
        tools={tool: _which(tool) is not None for tool in KNOWN_TOOLS},
    )


class _ResultStore:
    """Bounded, TTL'd idempotency store keyed by request_id.

    Each id is bound to the fingerprint of the request it first arrived with; the same id
    with different parameters is a conflict, never a replay of someone else's verdict."""

    def __init__(self) -> None:
        self._done: OrderedDict[str, tuple[float, ProbeResponse]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[ProbeResponse]] = {}
        self._fingerprints: OrderedDict[str, str] = OrderedDict()

    def bind(self, request_id: str, fingerprint: str) -> bool:
        """True when the id is new or already bound to this exact fingerprint."""
        known = self._fingerprints.get(request_id)
        if known is None:
            self._fingerprints[request_id] = fingerprint
            while len(self._fingerprints) > 2 * _RESULT_MAX_ENTRIES:
                self._fingerprints.popitem(last=False)
            return True
        return known == fingerprint

    def _evict(self) -> None:
        now = time.monotonic()
        while self._done:
            key, (stamp, _) = next(iter(self._done.items()))
            if now - stamp > _RESULT_TTL_SECONDS or len(self._done) > _RESULT_MAX_ENTRIES:
                self._done.popitem(last=False)
                self._fingerprints.pop(key, None)
            else:
                break

    def get(self, request_id: str) -> ProbeResponse | None:
        self._evict()
        hit = self._done.get(request_id)
        return hit[1] if hit else None

    def inflight(self, request_id: str) -> asyncio.Future[ProbeResponse] | None:
        return self._inflight.get(request_id)

    def start(self, request_id: str) -> asyncio.Future[ProbeResponse]:
        fut: asyncio.Future[ProbeResponse] = asyncio.get_running_loop().create_future()
        self._inflight[request_id] = fut
        return fut

    def finish(self, request_id: str, response: ProbeResponse) -> None:
        fut = self._inflight.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(response)
        self._done[request_id] = (time.monotonic(), response)
        self._evict()

    def abort(self, request_id: str, exc: BaseException) -> None:
        fut = self._inflight.pop(request_id, None)
        self._fingerprints.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_exception(exc)


async def _authenticate(request: Request, key: str) -> bytes:
    body = await request.body()
    if len(body) > MAX_REQUEST_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request body too large")
    problem = verify_signature(
        key, request.headers.get(TIMESTAMP_HEADER), request.headers.get(SIGNATURE_HEADER), body
    )
    if problem is not None:
        raise HTTPException(status_code=401, detail=problem)
    return body


def create_app(config: VerifierConfig | None = None) -> FastAPI:
    cfg = config or VerifierConfig()
    runner = LocalProbeRunner(
        cfg.clone_url_format,
        workspace_root=cfg.workspace_root,
        limits=cfg.limits,
        cache_root=cfg.cache_root,
    )
    app = FastAPI(title="remediator probe verifier", docs_url=None, redoc_url=None)
    slots = asyncio.Semaphore(cfg.max_concurrent)
    store = _ResultStore()
    in_flight = {"count": 0}

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        # Never echo request content back; the detail strings above are constants.
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/health", response_model=Health)
    async def get_health() -> Health:
        return health()

    @app.get("/capabilities", response_model=Capabilities)
    async def get_capabilities(request: Request) -> Capabilities:
        await _authenticate(request, cfg.hmac_key)
        return Capabilities(
            isolation=isolation(),
            tools={tool: _which(tool) is not None for tool in KNOWN_TOOLS},
            tool_versions=await tool_versions(),
            repository_allowlist=list(cfg.repository_allowlist),
            registry_present=cfg.probe_root.is_dir(),
            max_concurrent=cfg.max_concurrent,
            max_timeout_seconds=cfg.max_timeout_seconds,
            cache_enabled=cfg.cache_root is not None,
            in_flight=in_flight["count"],
        )

    async def _spec_for(request: ProbeRequest) -> ProbeRunSpec:
        if request.repository not in cfg.repository_allowlist:
            raise HTTPException(status_code=403, detail="repository is not allowlisted")
        try:
            probe = await load_approved_probe(
                cfg.probe_root, request.repository, request.issue_number
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
            timeout_seconds=min(
                request.timeout_seconds, probe.timeout_seconds, cfg.max_timeout_seconds
            ),
            max_output_bytes=request.max_output_bytes,
            required_tools=probe.required_tools,
            setup_steps=manifest_setup_steps(probe.manifest),
            setup_timeout_seconds=min(
                manifest_setup_timeout(probe.manifest), cfg.max_timeout_seconds
            ),
            cache_inputs=manifest_cache_inputs(probe.manifest),
        )

    @app.post("/probe", response_model=ProbeResponse)
    async def run_probe(request: Request) -> ProbeResponse:
        body = await _authenticate(request, cfg.hmac_key)
        try:
            parsed = ProbeRequest.model_validate_json(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="malformed probe request") from exc
        if parsed.protocol_version != VERIFIER_PROTOCOL_VERSION:
            raise HTTPException(status_code=400, detail="unsupported protocol_version")
        if not store.bind(parsed.request_id, parsed.fingerprint()):
            raise HTTPException(
                status_code=409, detail="request_id already used with different parameters"
            )
        done = store.get(parsed.request_id)
        if done is not None:
            return done.model_copy(update={"replayed": True})
        pending = store.inflight(parsed.request_id)
        if pending is not None:
            return (await asyncio.shield(pending)).model_copy(update={"replayed": True})
        spec = await _spec_for(parsed)
        if slots.locked():
            raise HTTPException(status_code=409, detail="verifier is at capacity")
        store.start(parsed.request_id)
        try:
            async with slots:
                in_flight["count"] += 1
                try:
                    result = await runner.run(spec)
                finally:
                    in_flight["count"] -= 1
                    await asyncio.to_thread(kill_stray_processes)
        except BaseException as exc:
            store.abort(parsed.request_id, exc)
            raise
        response = ProbeResponse.from_result(parsed, result)
        store.finish(parsed.request_id, response)
        return response

    return app
