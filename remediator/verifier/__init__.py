"""Probe verifier front (protocol `verifier.v2`): authentication, binding, idempotency.

Two processes make up the verifier (docker-compose.yml, docker/verifier/Dockerfile):

* this **front** (`verifier` service) holds the one secret of the boundary - the request
  HMAC key - and executes *no* repository code. It authenticates the worker's requests,
  binds them to its own read-only probe registry, owns idempotency and hands the bound
  request to the executor;
* the **executor** (`verifier-runner` service, `remediator.verifier.executor`) runs the
  probe. It holds no secret at all, not even the HMAC key, because repository code executed
  there runs under its UID and can read whatever that process can read. Its only network
  paths are this front and the egress proxy.

Neither process receives an `env_file` or application secret and neither imports
`remediator.config`, so no `.env` can be loaded by accident.

`VERIFIER_EXECUTION=in-process` makes the front run probes itself (tests and single-container
development). Capabilities then report `execution: in-process` and a worker with
`PROBE_VERIFIER_REQUIRE_ISOLATION=true` (live mode) refuses it: with the key and the probe in
one process the key is readable by the probe.

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
One probe runs at a time (`VERIFIER_MAX_CONCURRENT` must be 1: the executor's post-run
sweep kills every process of the probe UID); extra requests get `409`, which the worker
treats as transient.
"""

import asyncio
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..probes.runner import (
    STAGE_PREFLIGHT,
    LocalProbeRunner,
    ProbeRunResult,
    ProbeRunSpec,
    _which,
    command_identity,
    credential_exposure,
    kill_stray_processes,
    tool_versions,
)
from .executor import ExecutorConfig, isolation, parse_probe_request, spec_for
from .protocol import (
    EXECUTION_IN_PROCESS,
    EXECUTION_RUNNER,
    KNOWN_TOOLS,
    MAX_REQUEST_BODY_BYTES,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    VERIFIER_PROTOCOL_VERSION,
    Capabilities,
    Health,
    ProbeRequest,
    ProbeResponse,
    RunnerHealth,
    verify_signature,
)

logger = logging.getLogger(__name__)

HMAC_KEY_ENV = "VERIFIER_HMAC_KEY"
HMAC_KEY_FILE_ENV = "VERIFIER_HMAC_KEY_FILE"
_RESULT_TTL_SECONDS = 6 * 3600
_RESULT_MAX_ENTRIES = 512
_RUNNER_HEALTH_TIMEOUT_SECONDS = 15.0
_RUNNER_REQUEST_OVERHEAD_SECONDS = 60.0


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
        self.execution = env.get("VERIFIER_EXECUTION", EXECUTION_RUNNER).strip().lower()
        self.runner_url = env.get("VERIFIER_RUNNER_URL", "").strip().rstrip("/")
        self.hmac_key = _read_hmac_key(env)
        # Registry binding, allowlist and timeouts are enforced here *and* in the executor.
        self.executor = ExecutorConfig(env)
        self.probe_root = self.executor.probe_root
        self.repository_allowlist = self.executor.repository_allowlist
        self.max_timeout_seconds = self.executor.max_timeout_seconds
        self.max_concurrent = self.executor.max_concurrent
        if self.execution not in (EXECUTION_RUNNER, EXECUTION_IN_PROCESS):
            raise ValueError(
                f"VERIFIER_EXECUTION must be {EXECUTION_RUNNER!r} or {EXECUTION_IN_PROCESS!r}"
            )
        if self.execution == EXECUTION_RUNNER and not self.runner_url.startswith("http://"):
            raise ValueError(
                "VERIFIER_RUNNER_URL (http://verifier-runner:8081) is required unless "
                "VERIFIER_EXECUTION=in-process is set explicitly"
            )

    @property
    def clone_url_format(self) -> str:
        return self.executor.clone_url_format

    @clone_url_format.setter
    def clone_url_format(self, value: str) -> None:
        self.executor.clone_url_format = value


def health(*, allow_own_secret: bool = True) -> Health:
    return Health(
        credential_exposure=credential_exposure(allow_own_secret=allow_own_secret),
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


class ExecutorBusy(Exception):
    """The executor's single slot is taken; surfaced to the worker as 409."""


class _InProcessExecution:
    """Front and executor in one process (tests, development). Reports itself as such."""

    execution = EXECUTION_IN_PROCESS

    def __init__(self, cfg: ExecutorConfig) -> None:
        self.cfg = cfg
        self.runner: LocalProbeRunner = cfg.build_runner()
        self._slot = asyncio.Semaphore(1)
        self.in_flight = 0

    async def describe(self) -> RunnerHealth:
        return RunnerHealth(
            isolation=isolation(
                egress_proxy_configured=bool(self.cfg.egress_proxy_url), allow_own_secret=True
            ),
            tools={tool: _which(tool) is not None for tool in KNOWN_TOOLS},
            tool_versions=await tool_versions(),
            repository_allowlist=list(self.cfg.repository_allowlist),
            registry_present=self.cfg.probe_root.is_dir(),
            cache_enabled=self.cfg.cache_root is not None,
            workspace_writable=os.access(self.cfg.workspace_root, os.W_OK),
            in_flight=self.in_flight,
        )

    async def run(self, request: ProbeRequest, spec: ProbeRunSpec) -> ProbeResponse:
        if self._slot.locked():
            raise ExecutorBusy
        async with self._slot:
            self.in_flight += 1
            try:
                result = await self.runner.run(spec)
            finally:
                self.in_flight -= 1
                await asyncio.to_thread(kill_stray_processes)
        return ProbeResponse.from_result(request, result)


class _RemoteExecution:
    """Forwards bound requests to the credential-free executor over the internal network."""

    execution = EXECUTION_RUNNER

    def __init__(self, runner_url: str, client: httpx.AsyncClient | None = None) -> None:
        self.runner_url = runner_url
        self._client = client or httpx.AsyncClient(base_url=runner_url)

    async def describe(self) -> RunnerHealth:
        try:
            response = await self._client.get(
                f"{self.runner_url}/health", timeout=_RUNNER_HEALTH_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail=f"probe executor unreachable: {exc.__class__.__name__}"
            ) from exc
        if response.status_code != 200:
            raise HTTPException(status_code=503, detail="probe executor is not healthy")
        try:
            return RunnerHealth.model_validate(response.json())
        except ValueError as exc:
            raise HTTPException(
                status_code=503, detail="probe executor health is malformed"
            ) from exc

    async def run(self, request: ProbeRequest, spec: ProbeRunSpec) -> ProbeResponse:
        identity = command_identity(spec)
        started = time.monotonic()
        body = request.model_dump_json().encode()
        try:
            response = await self._client.post(
                f"{self.runner_url}/run",
                content=body,
                headers={"Content-Type": "application/json"},
                timeout=spec.timeout_seconds
                + spec.setup_timeout_seconds
                + _RUNNER_REQUEST_OVERHEAD_SECONDS,
            )
        except httpx.HTTPError as exc:
            return self._infra(
                request, identity, started, f"probe executor unreachable: {exc.__class__.__name__}"
            )
        if response.status_code == 409 and _detail(response) == "verifier is at capacity":
            raise ExecutorBusy
        if response.status_code != 200:
            return self._infra(
                request,
                identity,
                started,
                f"probe executor returned HTTP {response.status_code}: {_detail(response)}",
            )
        try:
            result = ProbeResponse.model_validate(response.json())
        except ValueError as exc:
            return self._infra(
                request, identity, started, f"executor response malformed: {exc.__class__.__name__}"
            )
        if (
            result.request_id != request.request_id
            or result.command_identity != identity
            or result.repository != request.repository
            or result.commit_sha != request.commit_sha
            or result.script_hash != request.script_hash
        ):
            return self._infra(
                request, identity, started, "executor reported a different execution than asked"
            )
        return result.model_copy(update={"replayed": False})

    @staticmethod
    def _infra(request: ProbeRequest, identity: str, started: float, reason: str) -> ProbeResponse:
        return ProbeResponse.from_result(
            request,
            ProbeRunResult(
                runner_mode="local",
                command_identity=identity,
                exit_code=None,
                stdout="",
                stderr="",
                output_truncated=False,
                timed_out=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                infrastructure_error=reason,
                failure_stage=STAGE_PREFLIGHT,
            ),
        )


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return str(detail)[:200] if isinstance(detail, str) else ""


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


def create_app(
    config: VerifierConfig | None = None, *, executor_client: httpx.AsyncClient | None = None
) -> FastAPI:
    cfg = config or VerifierConfig()
    execution: _InProcessExecution | _RemoteExecution
    if cfg.execution == EXECUTION_RUNNER:
        execution = _RemoteExecution(cfg.runner_url, executor_client)
    else:
        execution = _InProcessExecution(cfg.executor)
    app = FastAPI(title="remediator probe verifier", docs_url=None, redoc_url=None)
    store = _ResultStore()

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        # Never echo request content back; the detail strings above are constants.
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/health")
    async def get_health() -> dict[str, str]:
        # Unauthenticated liveness for Compose only: no uid, toolchain, credential or
        # boundary detail leaves the process unsigned. /capabilities (signed) has it all.
        return {"status": "ok", "protocol_version": VERIFIER_PROTOCOL_VERSION}

    @app.get("/capabilities", response_model=Capabilities)
    async def get_capabilities(request: Request) -> Capabilities:
        await _authenticate(request, cfg.hmac_key)
        runner = await execution.describe()
        return Capabilities(
            isolation=runner.isolation,
            tools=runner.tools,
            tool_versions=runner.tool_versions,
            repository_allowlist=sorted(
                set(cfg.repository_allowlist) & set(runner.repository_allowlist)
            ),
            registry_present=cfg.probe_root.is_dir() and runner.registry_present,
            max_concurrent=cfg.max_concurrent,
            max_timeout_seconds=cfg.max_timeout_seconds,
            cache_enabled=runner.cache_enabled,
            in_flight=runner.in_flight,
            execution=execution.execution,
        )

    @app.post("/probe", response_model=ProbeResponse)
    async def run_probe(request: Request) -> ProbeResponse:
        body = await _authenticate(request, cfg.hmac_key)
        parsed = await parse_probe_request(body)
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
        spec = await spec_for(
            parsed,
            probe_root=cfg.probe_root,
            repository_allowlist=cfg.repository_allowlist,
            max_timeout_seconds=cfg.max_timeout_seconds,
        )
        store.start(parsed.request_id)
        try:
            response = await execution.run(parsed, spec)
        except ExecutorBusy as exc:
            store.abort(parsed.request_id, exc)
            raise HTTPException(status_code=409, detail="verifier is at capacity") from None
        except BaseException as exc:
            store.abort(parsed.request_id, exc)
            raise
        store.finish(parsed.request_id, response)
        return response

    return app
