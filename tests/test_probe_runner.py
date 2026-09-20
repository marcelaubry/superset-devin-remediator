"""Local runner containment (the code that only ever runs inside the verifier container) and
the verifier <-> worker boundary. No external repository is touched: every probe runs against
a throw-away git repository created in tmp_path."""

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from remediator.models import ProbeTarget
from remediator.probes import build_probe_runner
from remediator.probes.remote import RemoteProbeRunner
from remediator.probes.runner import (
    PROBE_RUN_MARKER,
    FakeProbeRunner,
    LocalProbeRunner,
    ProbeResourceLimits,
    ProbeRunSpec,
    command_identity,
    credential_exposure,
    marked_pids,
)
from remediator.verifier import VerifierConfig, create_app, health
from remediator.verifier.protocol import VERIFIER_PROTOCOL_VERSION, Health

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None, reason="git/bash required"
)


def _git_repo(root: Path) -> tuple[Path, str]:
    """A bare-ish local repository the runner can fetch from via file://."""
    repo = root / "origin" / "acme" / "demo.git"
    work = root / "work"
    work.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
    }
    env["GIT_COMMITTER_EMAIL"] = "t@x"
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=work, check=True, env=env)
    (work / "README").write_text("hello\n")
    subprocess.run(["git", "add", "README"], cwd=work, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True, env=env)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()
    repo.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(repo)], check=True, env=env)
    # fetching an arbitrary SHA (not a ref) from a bare repo needs this
    subprocess.run(["git", "config", "uploadpack.allowAnySHA1InWant", "true"], cwd=repo, check=True)
    return repo, sha


def _spec(
    sha: str, script: str, *, timeout: int = 60, target: ProbeTarget = ProbeTarget.BASE
) -> ProbeRunSpec:
    return ProbeRunSpec(
        repository="acme/demo",
        issue_number=7,
        commit_sha=sha,
        target=target,
        probe_identifier="acme/demo#7",
        script_hash=hashlib.sha256(script.encode()).hexdigest(),
        script_content=script,
        timeout_seconds=timeout,
        max_output_bytes=4096,
        required_tools=(),
    )


def _runner(tmp_path: Path, **kwargs: object) -> LocalProbeRunner:
    runner = LocalProbeRunner(
        f"file://{tmp_path}/origin/{{repository}}.git",
        workspace_root=tmp_path,
        **kwargs,  # type: ignore[arg-type]
    )
    # The test process is not credential-free (the repo has a .env, the shell has tokens):
    # the boundary is exercised explicitly in test_local_runner_refuses_inside_credentials.
    runner.credential_check = lambda: []
    return runner


# -- LocalProbeRunner --------------------------------------------------------------------


async def test_local_runner_executes_exact_commit_and_bounds_output(tmp_path: Path) -> None:
    _, sha = _git_repo(tmp_path)
    script = (
        'set -e\ntest "$(cat README)" = hello\n'
        'test "$(git rev-parse HEAD)" = "$PROBE_COMMIT"\nseq 1 5000\nexit 3\n'
    )
    result = await _runner(tmp_path).run(_spec(sha, script))
    assert result.infrastructure_error is None
    assert result.exit_code == 3
    assert result.timed_out is False
    assert result.output_truncated is True
    assert len(result.stdout.encode()) <= 4096
    assert not list(tmp_path.glob("probe-*")), "workspace must be removed"


async def test_timeout_covers_process_exit_not_just_output(tmp_path: Path) -> None:
    """A child that closes stdout/stderr and keeps running must still die at the deadline."""
    _, sha = _git_repo(tmp_path)
    script = "echo started\nexec >&- 2>&-\nsleep 60\n"
    started = time.monotonic()
    result = await _runner(tmp_path).run(_spec(sha, script, timeout=1))
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.stdout == "started\n"
    assert elapsed < 10, f"timeout not enforced: took {elapsed:.1f}s"


async def test_detached_descendants_are_killed_and_workspace_removed(tmp_path: Path) -> None:
    """`setsid` escapes the process group; the run marker in the environment does not."""
    _, sha = _git_repo(tmp_path)
    marker_file = tmp_path / "marker"
    script = (
        f'echo "${PROBE_RUN_MARKER}" > {marker_file}\n'
        "setsid bash -c 'while true; do sleep 1; done' >/dev/null 2>&1 &\n"
        "setsid sleep 300 >/dev/null 2>&1 &\n"
        "sleep 0.2\n"
        "exit 0\n"
    )
    result = await _runner(tmp_path).run(_spec(sha, script))
    assert result.exit_code == 0
    marker = marker_file.read_text().strip()
    assert marker
    for _ in range(20):
        if not marked_pids(marker):
            break
        await asyncio.sleep(0.05)
    assert marked_pids(marker) == [], "detached descendants survived the run"
    assert not list(tmp_path.glob("probe-*"))


async def test_detached_descendant_holding_pipes_does_not_hang_timeout(tmp_path: Path) -> None:
    _, sha = _git_repo(tmp_path)
    # grandchild inherits stdout, so the pipe would never reach EOF without the marker kill
    script = "setsid sleep 300 &\nsleep 60\n"
    started = time.monotonic()
    result = await _runner(tmp_path).run(_spec(sha, script, timeout=1))
    assert result.timed_out is True
    assert time.monotonic() - started < 15


async def test_resource_limits_are_inherited_by_the_probe(tmp_path: Path) -> None:
    _, sha = _git_repo(tmp_path)
    limits = ProbeResourceLimits(max_file_size_bytes=4096)
    script = "ulimit -f\nhead -c 100000 /dev/zero > big 2>/dev/null; echo write=$?\n"
    result = await _runner(tmp_path, limits=limits).run(_spec(sha, script))
    lines = result.stdout.split()
    assert lines[0] == "4", f"RLIMIT_FSIZE (bash reports KiB) not applied: {result.stdout!r}"
    assert lines[1] != "write=0"


async def test_local_runner_refuses_inside_credentials(tmp_path: Path) -> None:
    _, sha = _git_repo(tmp_path)
    runner = LocalProbeRunner(
        f"file://{tmp_path}/origin/{{repository}}.git", workspace_root=tmp_path
    )
    runner.credential_check = lambda: ["DEVIN_API_KEY", ".env"]
    result = await runner.run(_spec(sha, "exit 0\n"))
    assert result.infrastructure_error is not None
    assert "credential-bearing" in result.infrastructure_error
    assert result.exit_code is None


def test_credential_exposure_sees_env_and_files(tmp_path: Path) -> None:
    env = {"DEVIN_API_KEY": "x", "MY_SERVICE_TOKEN": "y", "PATH": "/bin", "EMPTY_TOKEN": ""}
    secret = tmp_path / ".env"
    secret.write_text("x=y")
    assert credential_exposure(env, (str(secret),)) == [
        "DEVIN_API_KEY",
        "MY_SERVICE_TOKEN",
        str(secret),
    ]
    assert credential_exposure({"PATH": "/bin"}, ()) == []


async def test_missing_tool_is_infrastructure_failure(tmp_path: Path) -> None:
    _, sha = _git_repo(tmp_path)
    spec = _spec(sha, "exit 0\n")
    spec = ProbeRunSpec(**{**spec.__dict__, "required_tools": ("definitely-not-installed-tool",)})
    result = await _runner(tmp_path).run(spec)
    assert result.infrastructure_error is not None
    assert "definitely-not-installed-tool" in result.infrastructure_error


async def test_unknown_commit_is_infrastructure_failure(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    result = await _runner(tmp_path).run(_spec("0" * 40, "exit 0\n"))
    assert result.infrastructure_error is not None
    assert "fetch" in result.infrastructure_error


# -- verifier service and RemoteProbeRunner -----------------------------------------------


def _verifier_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    cfg = VerifierConfig(
        {
            "VERIFIER_CLONE_URL_FORMAT": "https://example.invalid/{repository}.git",
            "VERIFIER_WORKSPACE_ROOT": str(tmp_path),
            "VERIFIER_MAX_TIMEOUT_SECONDS": "30",
        }
    )
    app = create_app(cfg)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://verifier")


def test_verifier_config_requires_https_clone_url() -> None:
    with pytest.raises(ValueError, match="https"):
        VerifierConfig({"VERIFIER_CLONE_URL_FORMAT": "file:///{repository}"})


def test_verifier_package_does_not_import_settings() -> None:
    """pydantic-settings is what turns `.env` into secrets; the verifier must never load it."""
    code = (
        "import sys, remediator.verifier, remediator.verifier.__main__\n"
        "assert 'remediator.config' not in sys.modules, 'verifier must not load Settings/.env'\n"
        "assert 'pydantic_settings' not in sys.modules\n"
        "assert 'dotenv' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_health_reports_this_processes_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_TOKEN", "leaked")
    report = health()
    assert "SOME_TOKEN" in report.credential_exposure
    assert report.credential_free is False
    assert set(report.tools) >= {"git", "bash", "node"}


async def test_verifier_rejects_wrong_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _verifier_client(tmp_path, monkeypatch) as client:
        body = {
            "protocol_version": "verifier.v0",
            **{k: v for k, v in _spec("a" * 40, "exit 0\n").__dict__.items()},
        }
        body["target"] = "BASE"
        response = await client.post("/probe", json=body)
    assert response.status_code == 400


def _health_json(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "protocol_version": VERIFIER_PROTOCOL_VERSION,
        "credential_exposure": [],
        "uid": 65534,
        "root_writable": False,
        "tools": {"git": True, "bash": True},
    }
    return {**base, **overrides}


class _Stub:
    def __init__(self, health_json: dict[str, object], probe_json: dict[str, object] | None = None):
        self.health_json = health_json
        self.probe_json = probe_json
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json=self.health_json)
        if self.probe_json is None:
            return httpx.Response(500)
        return httpx.Response(200, json=self.probe_json)


def _remote(stub: _Stub) -> RemoteProbeRunner:
    client = httpx.AsyncClient(transport=httpx.MockTransport(stub.handler), base_url="http://v")
    return RemoteProbeRunner("http://v", client=client)


@pytest.mark.parametrize(
    ("health_json", "reason"),
    [
        (_health_json(credential_exposure=["GITHUB_TOKEN"]), "can see credentials"),
        (_health_json(uid=0), "runs as root"),
        (_health_json(root_writable=True), "writable"),
        (_health_json(protocol_version="verifier.v9"), "expected verifier.v1"),
    ],
)
async def test_remote_runner_refuses_untrustworthy_verifier(
    health_json: dict[str, object], reason: str
) -> None:
    stub = _Stub(health_json, probe_json={"should": "never be requested"})
    result = await _remote(stub).run(_spec("a" * 40, "exit 0\n"))
    assert result.infrastructure_error is not None
    assert reason in result.infrastructure_error
    assert result.exit_code is None
    assert all(r.url.path == "/health" for r in stub.requests), "probe must not be forwarded"


async def test_remote_runner_forwards_spec_and_checks_identity() -> None:
    spec = _spec("a" * 40, "exit 1\n")

    good = {
        "protocol_version": VERIFIER_PROTOCOL_VERSION,
        "runner_mode": "local",
        "command_identity": command_identity(spec),
        "exit_code": 1,
        "stdout": "x",
        "stderr": "",
        "output_truncated": False,
        "timed_out": False,
        "duration_ms": 5,
        "infrastructure_error": None,
    }
    stub = _Stub(_health_json(), probe_json=good)
    result = await _remote(stub).run(spec)
    assert result.exit_code == 1 and result.infrastructure_error is None
    assert result.runner_mode == "remote"
    sent = stub.requests[-1]
    assert sent.url.path == "/probe"
    assert b'"script_hash":"' + spec.script_hash.encode() in sent.content.replace(b" ", b"")

    stub = _Stub(_health_json(), probe_json={**good, "command_identity": "something else"})
    result = await _remote(stub).run(spec)
    assert result.infrastructure_error is not None
    assert "different command identity" in result.infrastructure_error


async def test_remote_runner_unreachable_is_infrastructure_not_verdict() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="http://v")
    result = await RemoteProbeRunner("http://v", client=client).run(_spec("a" * 40, "exit 0\n"))
    assert result.infrastructure_error is not None
    assert "unreachable" in result.infrastructure_error


def test_worker_factory_offers_only_fake_or_remote() -> None:
    assert isinstance(build_probe_runner("fake", None), FakeProbeRunner)
    assert isinstance(build_probe_runner("remote", "http://verifier:8080"), RemoteProbeRunner)
    with pytest.raises(ValueError, match="'local'"):
        build_probe_runner("local", None)


def test_health_model_credential_free_property() -> None:
    assert Health.model_validate(_health_json()).credential_free is True
    assert Health.model_validate(_health_json(uid=0)).credential_free is False
