"""Independent probe runners.

A runner executes the *snapshotted* probe script against one exact commit and reports the
exit code plus bounded output. It never derives anything from the remediation branch, the
issue text or Devin's output; the only inputs are the snapshot taken at dispatch and the
commit SHA under test.

Two implementations exist:

* `FakeProbeRunner` - deterministic, keyed by the shared fixture table. Used by tests and
  fake-mode simulations. It is *not* evidence and live mode refuses to use it.
* `LocalProbeRunner` - fetches the exact commit into a throw-away workspace, writes the
  snapshot script there and runs it with `bash` via `create_subprocess_exec` (argv only, no
  shell interpolation) under a minimal environment, with wall-clock and output limits.
  Missing tools, fetch failures or workspace errors surface as *infrastructure* failures,
  which the pipeline never mistakes for a passing probe.

Credential boundary
-------------------
A scrubbed child environment is *not* a security boundary: a probe script runs the
repository's own code, which can read `/proc/<worker-pid>/environ`, the worker's mounted
secret files, the Docker socket or the local network of whatever process spawned it. If that
process is the credential-bearing worker (Devin key, GitHub token, Slack tokens, operator
token, database URL) the probe can exfiltrate them regardless of `env=`. The local runner
therefore refuses to spawn anything while the *hosting process* can see a credential, and
`Settings` refuses live mode unless the operator declares a credential-free verifier boundary
(`PROBE_VERIFIER_ISOLATION=credential_free_container`). The Phase 5 production path is a
dedicated verifier container that holds no application secrets; see docs/threat-model.md.
"""

import asyncio
import os
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..fixtures import RemediationFixture, remediation_fixture
from ..models import ProbeTarget

_MINIMAL_PATH = "/usr/local/bin:/usr/bin:/bin"
_FETCH_TIMEOUT_SECONDS = 600

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
CREDENTIAL_PATHS = ("/run/secrets", "/var/run/docker.sock")


def credential_exposure(
    environ: dict[str, str] | None = None, paths: tuple[str, ...] = CREDENTIAL_PATHS
) -> list[str]:
    """Names of credentials visible to this process; empty means the boundary holds."""
    env = os.environ if environ is None else environ
    found = sorted(
        name
        for name, value in env.items()
        if value
        and (name in CREDENTIAL_ENV_NAMES or name.upper().endswith(CREDENTIAL_ENV_SUFFIXES))
    )
    found.extend(path for path in paths if Path(path).exists())
    return found


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


class LocalProbeRunner:
    mode = "local"

    def __init__(
        self,
        clone_url_format: str,
        *,
        workspace_root: Path | None = None,
        fetch_timeout_seconds: int = _FETCH_TIMEOUT_SECONDS,
    ) -> None:
        if "{repository}" not in clone_url_format:
            raise ValueError("clone_url_format must contain {repository}")
        self.clone_url_format = clone_url_format
        self.workspace_root = workspace_root
        self.fetch_timeout_seconds = fetch_timeout_seconds
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
            )
        missing = [tool for tool in ("git", "bash", *spec.required_tools) if _which(tool) is None]
        if missing:
            return self._infra(identity, started, f"required tools not installed: {missing}")
        try:
            workspace = Path(
                tempfile.mkdtemp(
                    prefix="probe-",
                    dir=str(self.workspace_root) if self.workspace_root else None,
                )
            )
        except OSError as exc:
            return self._infra(identity, started, f"cannot create workspace: {exc}")
        try:
            repo_dir = workspace / "repo"
            home_dir = workspace / "home"
            repo_dir.mkdir()
            home_dir.mkdir()
            env = _minimal_env(home_dir)
            fetch_error = await self._fetch_exact_commit(repo_dir, spec, env)
            if fetch_error is not None:
                return self._infra(identity, started, fetch_error)
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
            return await self._execute(identity, started, script_path, repo_dir, spec, env)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

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
                )
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.fetch_timeout_seconds
                )
            except TimeoutError:
                proc.kill()
                return f"timed out running {' '.join(argv[:2])} for {spec.commit_sha}"
            except OSError as exc:
                return f"cannot run {' '.join(argv[:2])}: {exc}"
            if proc.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()[:500]
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
    ) -> ProbeRunResult:
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
            )
        except OSError as exc:
            return self._infra(identity, started, f"cannot start probe: {exc}")
        out = _BoundedCapture(spec.max_output_bytes)
        err = _BoundedCapture(spec.max_output_bytes)
        drain = asyncio.gather(out.drain(proc.stdout), err.drain(proc.stderr))
        timed_out = False
        try:
            await asyncio.wait_for(asyncio.shield(drain), timeout=spec.timeout_seconds)
            await proc.wait()
        except TimeoutError:
            timed_out = True
            _kill_group(proc)
            await proc.wait()
            try:
                await asyncio.wait_for(drain, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                pass
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


def _which(tool: str) -> str | None:
    return shutil.which(tool, path=_MINIMAL_PATH)


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


def build_probe_runner(mode: str, clone_url_format: str) -> ProbeRunner:
    if mode == "local":
        return LocalProbeRunner(clone_url_format)
    return FakeProbeRunner()
