"""Independent probe runners.

A runner executes the *snapshotted* probe script against one exact commit and reports the
exit code plus bounded output. It never derives anything from the remediation branch, the
issue text or Devin's output; the only inputs are the snapshot taken at dispatch and the
commit SHA under test.

Three implementations exist:

* `FakeProbeRunner` - deterministic, keyed by the shared fixture table. Used by tests and
  fake-mode simulations. It is *not* evidence and live mode refuses to use it.
* `LocalProbeRunner` - fetches the exact commit into a throw-away workspace, writes the
  snapshot script there and runs it with `bash` via `create_subprocess_exec` (argv only, no
  shell interpolation) under a minimal environment, with wall-clock, output and resource
  limits. Missing tools, fetch failures or workspace errors surface as *infrastructure*
  failures, which the pipeline never mistakes for a passing probe. It is what the dedicated
  verifier container runs (`remediator.verifier`); the worker never instantiates it.
* `RemoteProbeRunner` (`remediator.probes.remote`) - what the worker uses: it forwards the
  spec to the verifier container and refuses to trust a verifier that reports a credential
  in its own environment.

Credential boundary
-------------------
A scrubbed child environment is *not* a security boundary: a probe script runs the
repository's own code, which can read `/proc/<worker-pid>/environ`, the worker's mounted
secret files, the Docker socket, `.env` on disk or the local network of whatever process
spawned it. If that process is the credential-bearing worker (Devin key, GitHub token,
Slack tokens, operator token, database URL) the probe can exfiltrate them regardless of
`env=`. `Settings` therefore does not offer a local mode at all: the worker either uses the
fake runner (simulations) or delegates to the verifier container, which holds no
application secret, runs as a dedicated UID on a read-only root filesystem and is bounded
by PID/memory limits (docker-compose.yml, docs/threat-model.md). `credential_exposure` is
the verifier's own last line of defence and is also reported through its health endpoint.

Process containment
-------------------
Every probe child carries a unique `REMEDIATOR_PROBE_RUN` marker in its environment. On
timeout (and after every run) the runner kills the child's process group *and* every process
visible in `/proc` that still carries the marker, so descendants that `setsid` away from the
group cannot outlive the run or race the workspace cleanup. Inside the verifier container
`/proc` is the container's PID namespace, so that sweep is complete; `pids_limit`,
memory limits and the tmpfs workspace size bound what a probe can consume in between.
"""

import asyncio
import hashlib
import os
import resource
import shutil
import signal
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..fixtures import RemediationFixture, remediation_fixture
from ..models import ProbeTarget

_MINIMAL_PATH = "/usr/local/bin:/usr/bin:/bin"
_FETCH_TIMEOUT_SECONDS = 600
_KILL_GRACE_SECONDS = 5
_VERSION_TIMEOUT_SECONDS = 15
_SETUP_STDERR_BYTES = 2000
PROBE_RUN_MARKER = "REMEDIATOR_PROBE_RUN"
# Tools whose `--version` is recorded as evidence when present on the minimal PATH.
VERSIONED_TOOLS = ("git", "bash", "node", "npm", "yarn", "python3")

STAGE_PREFLIGHT = "preflight"
STAGE_FETCH = "fetch"
STAGE_SETUP = "setup"
STAGE_EXECUTE = "execute"

# Environment variables whose presence in the *hosting* process proves it is a
# credential-bearing service. Matched exactly or by suffix so `*_TOKEN`-style secrets
# injected under other names are still caught.
CREDENTIAL_ENV_NAMES = frozenset(
    {
        "DEVIN_API_KEY",
        "GITHUB_TOKEN",
        "GITHUB_WEBHOOK_SECRET",
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "OPERATOR_TOKEN",
        "DATABASE_URL",
    }
)
CREDENTIAL_ENV_SUFFIXES = ("_TOKEN", "_SECRET", "_API_KEY", "_PASSWORD", "_PRIVATE_KEY")
# Files whose presence means the process runs inside a credential-bearing deployment.
CREDENTIAL_PATHS = ("/run/secrets", "/var/run/docker.sock", ".env")
# The verifier *front*'s request-authentication key is the one Compose secret it may hold;
# any other entry under /run/secrets is a provider or database credential and fails the
# boundary. The process that executes probes may hold no secret at all (`allow_own_secret`
# False): repository code runs under its UID and can read whatever it can read.
VERIFIER_OWN_SECRET_FILES = frozenset({"verifier_hmac_key"})


def credential_exposure(
    environ: dict[str, str] | None = None,
    paths: tuple[str, ...] = CREDENTIAL_PATHS,
    *,
    allow_own_secret: bool = False,
) -> list[str]:
    """Names of credentials visible to this process; empty means the boundary holds."""
    own = VERIFIER_OWN_SECRET_FILES if allow_own_secret else frozenset()
    env = os.environ if environ is None else environ
    found = sorted(
        name
        for name, value in env.items()
        if value
        and (name in CREDENTIAL_ENV_NAMES or name.upper().endswith(CREDENTIAL_ENV_SUFFIXES))
    )
    for path in paths:
        target = Path(path)
        if not target.exists():
            continue
        if target.is_dir():
            try:
                entries = sorted(e.name for e in target.iterdir())
            except OSError:
                found.append(path)
                continue
            found.extend(f"{path}/{name}" for name in entries if name not in own)
        else:
            found.append(path)
    return found


@dataclass(frozen=True)
class ProbeResourceLimits:
    """Per-process rlimits applied to the probe child (and inherited by its descendants).

    `max_processes` is `RLIMIT_NPROC`, which Linux counts per *UID*; it is only meaningful
    where the probe UID runs nothing else (the verifier container). `None` leaves a limit
    untouched.
    """

    max_processes: int | None = None
    max_file_size_bytes: int | None = None
    max_memory_bytes: int | None = None

    def apply(self) -> None:
        for limit, value in (
            (resource.RLIMIT_NPROC, self.max_processes),
            (resource.RLIMIT_FSIZE, self.max_file_size_bytes),
            (resource.RLIMIT_AS, self.max_memory_bytes),
        ):
            if value is not None:
                resource.setrlimit(limit, (value, value))

    def preexec(self) -> Callable[[], None] | None:
        if self == ProbeResourceLimits():
            return None
        return self.apply


@dataclass(frozen=True)
class ProbeRunSpec:
    repository: str
    issue_number: int
    commit_sha: str
    target: ProbeTarget
    probe_identifier: str
    script_hash: str
    script_content: str
    timeout_seconds: int
    max_output_bytes: int
    required_tools: tuple[str, ...]
    # Dependency installation declared by the approved manifest: argv arrays whose first
    # element must be one of `required_tools`. Run in order, argv only, before the script.
    setup_steps: tuple[tuple[str, ...], ...] = ()
    setup_timeout_seconds: int = 1800
    # Repository-relative lockfiles whose hashes (with tool versions and repository) form
    # the download-cache key. The cache is never consulted for the verdict.
    cache_inputs: tuple[str, ...] = ()
    # Hash of the approved manifest; the verifier refuses when its registry differs.
    manifest_hash: str = ""
    # Idempotency key for the verifier (`ProbeExecution.id` hex); the same execution asked
    # twice runs once.
    request_id: str = ""


@dataclass(frozen=True)
class ProbeRunResult:
    runner_mode: str
    command_identity: str
    exit_code: int | None
    stdout: str
    stderr: str
    output_truncated: bool
    timed_out: bool
    duration_ms: int
    infrastructure_error: str | None = None
    # Which stage produced `infrastructure_error` (preflight/fetch/setup/execute); None for
    # a product verdict.
    failure_stage: str | None = None
    tool_versions: dict[str, str] = field(default_factory=dict)

    @property
    def infrastructure_failed(self) -> bool:
        return self.infrastructure_error is not None


class ProbeRunner(Protocol):
    mode: str

    async def run(self, spec: ProbeRunSpec) -> ProbeRunResult: ...


def command_identity(spec: ProbeRunSpec) -> str:
    """Stable description of *what* ran, stored with every execution for audit."""
    return (
        f"bash probe.sh sha256:{spec.script_hash} @ {spec.repository}@{spec.commit_sha} "
        f"({spec.target.value.lower()})"
    )


class FakeProbeRunner:
    mode = "fake"

    def __init__(self, overrides: dict[tuple[int, ProbeTarget], int | None] | None = None) -> None:
        self.overrides = dict(overrides or {})
        self.calls: list[ProbeRunSpec] = []

    async def run(self, spec: ProbeRunSpec) -> ProbeRunResult:
        self.calls.append(spec)
        identity = command_identity(spec)
        fixture = remediation_fixture(spec.issue_number)
        key = (spec.issue_number, spec.target)
        if key in self.overrides:
            code = self.overrides[key]
            if code is None:
                return self._infra(identity, "simulated: runner override infrastructure failure")
            return self._done(identity, code, spec)
        if fixture == RemediationFixture.PROBE_INFRASTRUCTURE:
            return self._infra(identity, "simulated: required tool 'pytest' is not installed")
        if spec.target == ProbeTarget.BASE:
            code = 0 if fixture == RemediationFixture.PROBE_BASE_PASSES else 1
        else:
            code = 1 if fixture == RemediationFixture.PROBE_HEAD_FAILS else 0
        return self._done(identity, code, spec)

    @staticmethod
    def _done(identity: str, code: int, spec: ProbeRunSpec) -> ProbeRunResult:
        return ProbeRunResult(
            runner_mode="fake",
            command_identity=identity,
            exit_code=code,
            stdout=f"[fake probe] {spec.probe_identifier} @ {spec.commit_sha[:12]} -> {code}\n",
            stderr="",
            output_truncated=False,
            timed_out=False,
            duration_ms=1,
        )

    @staticmethod
    def _infra(identity: str, reason: str) -> ProbeRunResult:
        return ProbeRunResult(
            runner_mode="fake",
            command_identity=identity,
            exit_code=None,
            stdout="",
            stderr=reason + "\n",
            output_truncated=False,
            timed_out=False,
            duration_ms=1,
            infrastructure_error=reason,
        )


class _BoundedCapture:
    """Drains a pipe fully (so the child never blocks) while keeping only the first N bytes."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buffer = bytearray()
        self.truncated = False

    async def drain(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            room = self.limit - len(self.buffer)
            if room > 0:
                self.buffer += chunk[:room]
            if len(chunk) > room:
                self.truncated = True

    def text(self) -> str:
        return self.buffer.decode("utf-8", errors="replace")


class _TailCapture:
    """Drains a pipe fully while keeping only the last N bytes: package managers print
    the actual error after pages of warnings."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buffer = bytearray()

    async def drain(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            self.buffer += chunk
            if len(self.buffer) > self.limit:
                del self.buffer[: len(self.buffer) - self.limit]

    def text(self) -> str:
        return self.buffer.decode("utf-8", errors="replace")


class LocalProbeRunner:
    mode = "local"

    def __init__(
        self,
        clone_url_format: str,
        *,
        workspace_root: Path | None = None,
        fetch_timeout_seconds: int = _FETCH_TIMEOUT_SECONDS,
        limits: ProbeResourceLimits | None = None,
        proc_root: Path = Path("/proc"),
        cache_root: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        if "{repository}" not in clone_url_format:
            raise ValueError("clone_url_format must contain {repository}")
        self.clone_url_format = clone_url_format
        self.workspace_root = workspace_root
        self.fetch_timeout_seconds = fetch_timeout_seconds
        self.limits = limits or ProbeResourceLimits()
        self.proc_root = proc_root
        self.cache_root = cache_root
        # Routing variables (egress proxy) added to every child; never a secret.
        self.extra_env = dict(extra_env or {})
        self.credential_check = credential_exposure

    def clone_url(self, repository: str) -> str:
        return self.clone_url_format.format(repository=repository)

    async def run(self, spec: ProbeRunSpec) -> ProbeRunResult:
        identity = command_identity(spec)
        started = time.monotonic()
        exposed = self.credential_check()
        if exposed:
            return self._infra(
                identity,
                started,
                "refusing to execute probe inside a credential-bearing process "
                f"(visible: {exposed}); run the verifier in a credential-free container",
                STAGE_PREFLIGHT,
            )
        missing = [tool for tool in ("git", "bash", *spec.required_tools) if _which(tool) is None]
        if missing:
            return self._infra(
                identity, started, f"required tools not installed: {missing}", STAGE_PREFLIGHT
            )
        bad_step = _invalid_setup_step(spec)
        if bad_step is not None:
            return self._infra(identity, started, bad_step, STAGE_PREFLIGHT)
        try:
            workspace = Path(
                tempfile.mkdtemp(
                    prefix="probe-",
                    dir=str(self.workspace_root) if self.workspace_root else None,
                )
            )
        except OSError as exc:
            return self._infra(
                identity, started, f"cannot create workspace: {exc}", STAGE_PREFLIGHT
            )
        try:
            repo_dir = workspace / "repo"
            home_dir = workspace / "home"
            repo_dir.mkdir()
            home_dir.mkdir()
            env = {**_minimal_env(home_dir), **self.extra_env}
            marker = uuid.uuid4().hex
            env[PROBE_RUN_MARKER] = marker
            versions = await tool_versions(env, ("git", "bash", *spec.required_tools))
            fetch_error = await self._fetch_exact_commit(repo_dir, spec, env)
            if fetch_error is not None:
                return self._infra(identity, started, fetch_error, STAGE_FETCH, versions)
            identity_error = await self._verify_identity(repo_dir, spec, env)
            if identity_error is not None:
                return self._infra(identity, started, identity_error, STAGE_FETCH, versions)
            script_path = workspace / "probe.sh"
            script_path.write_text(spec.script_content, encoding="utf-8")
            script_path.chmod(0o500)
            env.update(
                {
                    "PROBE_TARGET": spec.target.value,
                    "PROBE_COMMIT": spec.commit_sha,
                    "PROBE_REPOSITORY": spec.repository,
                    "PROBE_IDENTIFIER": spec.probe_identifier,
                }
            )
            env.update(self._cache_env(repo_dir, spec, versions))
            setup_error = await self._run_setup(repo_dir, spec, env)
            if setup_error is not None:
                return self._infra(identity, started, setup_error, STAGE_SETUP, versions)
            return await self._execute(
                identity, started, script_path, repo_dir, spec, env, versions
            )
        finally:
            # Nothing spawned by this run (fetch, setup or the probe) may survive it or keep
            # writing into the workspace we are about to remove.
            kill_marked_processes(marker, self.proc_root)
            shutil.rmtree(workspace, ignore_errors=True)

    async def _verify_identity(
        self, repo_dir: Path, spec: ProbeRunSpec, env: dict[str, str]
    ) -> str | None:
        """The checked-out commit and the remote must be exactly what was requested; a
        redirecting or lying remote is an infrastructure failure, never a verdict."""
        head = await _capture(("git", "rev-parse", "--verify", "HEAD^{commit}"), repo_dir, env)
        if head != spec.commit_sha:
            return f"checked-out commit {head[:12] or '?'} is not the requested {spec.commit_sha}"
        remote = await _capture(("git", "remote", "get-url", "origin"), repo_dir, env)
        if remote != self.clone_url(spec.repository):
            return "origin remote does not match the allowlisted clone URL"
        return None

    def _cache_env(
        self, repo_dir: Path, spec: ProbeRunSpec, versions: dict[str, str]
    ) -> dict[str, str]:
        """Point package managers' *download* caches at a directory keyed by repository,
        tool versions and lockfile hashes. A stale or poisoned cache can only make
        installation fail (integrity is checked against the lockfile), never pass."""
        if self.cache_root is None or not spec.cache_inputs:
            return {}
        digest = hashlib.sha256()
        digest.update(spec.repository.encode())
        for tool in sorted(versions):
            digest.update(f"\0{tool}={versions[tool]}".encode())
        for rel in spec.cache_inputs:
            path = (repo_dir / rel).resolve()
            if not path.is_relative_to(repo_dir.resolve()) or not path.is_file():
                return {}
            digest.update(b"\0" + rel.encode() + b"=" + sha256_file(path).encode())
        key = digest.hexdigest()
        base = self.cache_root / key
        try:
            for sub in ("npm", "yarn", "pip"):
                (base / sub).mkdir(parents=True, exist_ok=True)
        except OSError:
            return {}
        return {
            "npm_config_cache": str(base / "npm"),
            "YARN_CACHE_FOLDER": str(base / "yarn"),
            "PIP_CACHE_DIR": str(base / "pip"),
        }

    async def _run_setup(
        self, repo_dir: Path, spec: ProbeRunSpec, env: dict[str, str]
    ) -> str | None:
        if not spec.setup_steps:
            return None
        deadline = time.monotonic() + spec.setup_timeout_seconds
        for argv in spec.setup_steps:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return f"setup exceeded {spec.setup_timeout_seconds}s before {argv[0]}"
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(repo_dir),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    preexec_fn=self.limits.preexec(),
                )
            except OSError as exc:
                return f"cannot start setup step {argv[0]}: {exc}"
            err = _TailCapture(_SETUP_STDERR_BYTES)
            work = asyncio.gather(err.drain(proc.stderr), proc.wait())
            try:
                await asyncio.wait_for(asyncio.shield(work), timeout=remaining)
            except TimeoutError:
                _kill_group(proc)
                kill_marked_processes(env[PROBE_RUN_MARKER], self.proc_root)
                await proc.wait()
                work.cancel()
                return f"setup step {' '.join(argv[:2])} timed out"
            if proc.returncode != 0:
                detail = err.text().strip()[-500:]
                return f"setup step {' '.join(argv[:2])} exited {proc.returncode}: {detail}"
        return None

    async def _fetch_exact_commit(
        self, repo_dir: Path, spec: ProbeRunSpec, env: dict[str, str]
    ) -> str | None:
        url = self.clone_url(spec.repository)
        steps: tuple[tuple[str, ...], ...] = (
            ("git", "init", "--quiet"),
            ("git", "remote", "add", "origin", url),
            ("git", "fetch", "--quiet", "--depth", "1", "origin", spec.commit_sha),
            ("git", "checkout", "--quiet", "--detach", spec.commit_sha),
        )
        for argv in steps:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(repo_dir),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                return f"cannot run {' '.join(argv[:2])}: {exc}"
            err = _TailCapture(_SETUP_STDERR_BYTES)
            work = asyncio.gather(err.drain(proc.stderr), proc.wait())
            try:
                await asyncio.wait_for(asyncio.shield(work), timeout=self.fetch_timeout_seconds)
            except TimeoutError:
                # `git fetch` forks git-remote-https; kill the whole session and any marked
                # descendant, not just the front process.
                _kill_group(proc)
                kill_marked_processes(env[PROBE_RUN_MARKER], self.proc_root)
                await proc.wait()
                work.cancel()
                return f"timed out running {' '.join(argv[:2])} for {spec.commit_sha}"
            if proc.returncode != 0:
                detail = err.text().strip()[-500:]
                return f"{' '.join(argv[:2])} failed for {spec.commit_sha}: {detail}"
        return None

    async def _execute(
        self,
        identity: str,
        started: float,
        script_path: Path,
        repo_dir: Path,
        spec: ProbeRunSpec,
        env: dict[str, str],
        versions: dict[str, str] | None = None,
    ) -> ProbeRunResult:
        marker = env[PROBE_RUN_MARKER]
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                str(script_path),
                cwd=str(repo_dir),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=self.limits.preexec(),
            )
        except OSError as exc:
            return self._infra(
                identity, started, f"cannot start probe: {exc}", STAGE_EXECUTE, versions
            )
        out = _BoundedCapture(spec.max_output_bytes)
        err = _BoundedCapture(spec.max_output_bytes)
        # The deadline covers the child's *exit*, not just its output: a probe that closes
        # stdout/stderr and keeps running is still killed at `timeout_seconds`.
        work = asyncio.gather(out.drain(proc.stdout), err.drain(proc.stderr), proc.wait())
        timed_out = False
        try:
            await asyncio.wait_for(asyncio.shield(work), timeout=spec.timeout_seconds)
        except TimeoutError:
            timed_out = True
            _kill_group(proc)
            kill_marked_processes(marker, self.proc_root)
            await proc.wait()
            try:
                # Pipes may stay open while a killed grandchild is reaped; bound that too.
                await asyncio.wait_for(work, timeout=_KILL_GRACE_SECONDS)
            except (TimeoutError, asyncio.CancelledError):
                work.cancel()
        duration_ms = int((time.monotonic() - started) * 1000)
        return ProbeRunResult(
            runner_mode=self.mode,
            command_identity=identity,
            exit_code=None if timed_out else proc.returncode,
            stdout=out.text(),
            stderr=err.text(),
            output_truncated=out.truncated or err.truncated,
            timed_out=timed_out,
            duration_ms=duration_ms,
            tool_versions=dict(versions or {}),
        )

    def _infra(
        self,
        identity: str,
        started: float,
        reason: str,
        stage: str = STAGE_PREFLIGHT,
        versions: dict[str, str] | None = None,
    ) -> ProbeRunResult:
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
            failure_stage=stage,
            tool_versions=dict(versions or {}),
        )


def _which(tool: str) -> str | None:
    return shutil.which(tool, path=_MINIMAL_PATH)


def _invalid_setup_step(spec: ProbeRunSpec) -> str | None:
    """Setup steps may only invoke tools the manifest declared; no shell, no paths."""
    for argv in spec.setup_steps:
        if not argv or not all(isinstance(a, str) and a for a in argv):
            return "setup step must be a non-empty argv array"
        if "/" in argv[0] or argv[0] not in spec.required_tools:
            return f"setup step {argv[0]!r} is not one of the manifest's runtime.tools"
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _capture(
    argv: tuple[str, ...], cwd: Path | None, env: dict[str, str], timeout: float = 30
) -> str:
    """First line of stdout of a short argv command, or '' on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return ""
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return ""
    if proc.returncode != 0:
        return ""
    lines = stdout.decode("utf-8", errors="replace").strip().splitlines()
    return lines[0] if lines else ""


async def tool_versions(
    env: dict[str, str] | None = None, tools: tuple[str, ...] = VERSIONED_TOOLS
) -> dict[str, str]:
    """`<tool> --version` for every installed tool in `tools` (bounded, argv only)."""
    env = env or _minimal_env(Path("/tmp"))
    found: dict[str, str] = {}
    for tool in dict.fromkeys(tools):
        if _which(tool) is None:
            continue
        line = await _capture((tool, "--version"), None, env, _VERSION_TIMEOUT_SECONDS)
        found[tool] = line[:120]
    return found


def _minimal_env(home_dir: Path) -> dict[str, str]:
    """No inherited variables: application secrets never reach the probe."""
    return {
        "PATH": _MINIMAL_PATH,
        "HOME": str(home_dir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "CI": "1",
    }


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def marked_pids(marker: str, proc_root: Path = Path("/proc")) -> list[int]:
    """PIDs whose environment carries `REMEDIATOR_PROBE_RUN=<marker>`, regardless of session
    or process group. Processes owned by other UIDs are invisible (their environ is
    unreadable), which is why the verifier runs probes under a dedicated UID."""
    needle = f"{PROBE_RUN_MARKER}={marker}".encode()
    found: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            found.append(int(entry.name))
    return found


def stray_pids(proc_root: Path = Path("/proc"), keep: frozenset[int] = frozenset()) -> list[int]:
    """Every live (non-zombie) process owned by this UID other than the caller, its parent
    and `keep`. In the verifier container nothing else runs under the probe UID, so anything
    left after a probe returned is a descendant that escaped the process group and dropped
    the run marker."""
    spare = set(keep) | {os.getpid(), os.getppid(), 1}
    uid = os.getuid()
    found: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) in spare:
            continue
        try:
            status = (entry / "status").read_text()
        except OSError:
            continue
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        if fields.get("State", "").strip().startswith("Z"):
            continue
        owner = fields.get("Uid", "").split()
        if owner and int(owner[0]) == uid:
            found.append(int(entry.name))
    return found


def kill_stray_processes(
    proc_root: Path = Path("/proc"), keep: frozenset[int] = frozenset()
) -> int:
    """SIGKILL every stray process of this UID (see `stray_pids`); returns how many were hit.
    Never runs as root: there the UID does not scope the sweep to probe descendants (and the
    verifier refuses to serve as root anyway)."""
    if os.getuid() == 0:
        return 0
    killed = 0
    for _ in range(10):
        pids = stray_pids(proc_root, keep)
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                continue
        time.sleep(0.05)
    return killed


def kill_marked_processes(marker: str, proc_root: Path = Path("/proc")) -> int:
    """SIGKILL every process still carrying the run marker; returns how many were signalled.
    Repeats until a sweep finds nothing so a forking child cannot outrun it."""
    killed = 0
    for _ in range(10):
        pids = marked_pids(marker, proc_root)
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                continue
        time.sleep(0.05)
    return killed
