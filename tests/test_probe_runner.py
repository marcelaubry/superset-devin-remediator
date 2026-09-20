"""Local runner containment (the code that only ever runs inside the verifier container) and
the verifier <-> worker boundary. No external repository is touched: every probe runs against
a throw-away git repository created in tmp_path."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import remediator.probes.runner as runner_module
import remediator.verifier as verifier_module
from remediator.models import ProbeTarget
from remediator.probes import build_probe_runner
from remediator.probes.registry import ApprovedProbe, load_approved_probe, write_probe
from remediator.probes.remote import RemoteProbeRunner, VerifierBusyError, new_request_id
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
from remediator.verifier.protocol import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    VERIFIER_PROTOCOL_VERSION,
    Isolation,
    sign,
    verify_signature,
)

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

SECRET = "verifier-test-hmac-key-with-at-least-32-characters"
REGISTRY = "registry"


def _signed(body: bytes, secret: str = SECRET, stamp: str | None = None) -> dict[str, str]:
    stamp = stamp or str(int(time.time()))
    return {
        TIMESTAMP_HEADER: stamp,
        SIGNATURE_HEADER: sign(secret, stamp, body),
        "Content-Type": "application/json",
    }


def _verifier_config(tmp_path: Path, **env: str) -> VerifierConfig:
    (tmp_path / REGISTRY).mkdir(exist_ok=True)
    cfg = VerifierConfig(
        {
            "VERIFIER_HMAC_KEY": SECRET,
            "VERIFIER_CLONE_URL_FORMAT": "https://example.invalid/{repository}.git",
            "VERIFIER_WORKSPACE_ROOT": str(tmp_path),
            "VERIFIER_PROBE_ROOT": str(tmp_path / REGISTRY),
            "VERIFIER_MAX_TIMEOUT_SECONDS": "30",
            "VERIFIER_REPOSITORY_ALLOWLIST": "acme/demo",
            # RLIMIT_NPROC counts every process of the UID; outside the container that
            # is the whole test session.
            "VERIFIER_MAX_PROCESSES": "",
            **env,
        }
    )
    cfg.clone_url_format = f"file://{tmp_path}/origin/{{repository}}.git"
    return cfg


def _verifier_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str
) -> httpx.AsyncClient:
    # The test process is not credential-free; the boundary itself is covered by
    # test_local_runner_refuses_inside_credentials and test_health_reports_this_processes_boundary.
    monkeypatch.setattr(runner_module, "credential_exposure", lambda: [])
    # The post-run sweep is UID-wide; on a shared host it would kill pytest itself.
    # test_verifier_kills_escaped_probe_children re-enables it scoped to the escapee.
    monkeypatch.setattr(verifier_module, "kill_stray_processes", lambda: 0)
    app = create_app(_verifier_config(tmp_path, **env))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://verifier")


async def _register(
    tmp_path: Path, sha: str, script: str, *, issue_number: int = 7, **kwargs: object
) -> ApprovedProbe:
    write_probe(
        tmp_path / REGISTRY,
        repository="acme/demo",
        issue_number=issue_number,
        base_sha=sha,
        script=script,
        expected_base_exit_code=1,
        expected_head_exit_code=0,
        tools=("bash",),
        **kwargs,  # type: ignore[arg-type]
    )
    return await load_approved_probe(tmp_path / REGISTRY, "acme/demo", issue_number)


def _request(probe: ApprovedProbe, sha: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "protocol_version": VERIFIER_PROTOCOL_VERSION,
        "request_id": new_request_id(),
        "repository": probe.repository,
        "issue_number": probe.issue_number,
        "commit_sha": sha,
        "target": "BASE",
        "probe_identifier": probe.identifier,
        "script_hash": probe.script_hash,
        "manifest_hash": probe.manifest_hash,
        "timeout_seconds": 20,
        "max_output_bytes": 4096,
    }
    return {**body, **overrides}


async def _post(client: httpx.AsyncClient, body: dict[str, object], **kw: object) -> httpx.Response:
    raw = json.dumps(body).encode()
    return await client.post("/probe", content=raw, headers=_signed(raw, **kw))  # type: ignore[arg-type]


def test_verifier_config_requires_https_clone_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="https"):
        VerifierConfig(
            {
                "VERIFIER_HMAC_KEY": SECRET,
                "VERIFIER_REPOSITORY_ALLOWLIST": "acme/demo",
                "VERIFIER_CLONE_URL_FORMAT": "file:///{repository}",
            }
        )


def test_verifier_config_requires_key_and_allowlist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="VERIFIER_HMAC_KEY"):
        VerifierConfig({"VERIFIER_REPOSITORY_ALLOWLIST": "acme/demo"})
    with pytest.raises(ValueError, match="at least 32"):
        VerifierConfig({"VERIFIER_HMAC_KEY": "short", "VERIFIER_REPOSITORY_ALLOWLIST": "acme/demo"})
    with pytest.raises(ValueError, match="ALLOWLIST"):
        VerifierConfig({"VERIFIER_HMAC_KEY": SECRET})
    with pytest.raises(ValueError, match="owner/repo"):
        VerifierConfig({"VERIFIER_HMAC_KEY": SECRET, "VERIFIER_REPOSITORY_ALLOWLIST": "nope"})
    key_file = tmp_path / "key"
    key_file.write_text(SECRET + "\n")
    cfg = VerifierConfig(
        {"VERIFIER_HMAC_KEY_FILE": str(key_file), "VERIFIER_REPOSITORY_ALLOWLIST": "acme/demo"}
    )
    assert cfg.hmac_key == SECRET


def test_verifier_config_scrubs_key_from_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERIFIER_HMAC_KEY", SECRET)
    monkeypatch.setenv("VERIFIER_REPOSITORY_ALLOWLIST", "acme/demo")
    cfg = VerifierConfig()
    assert cfg.hmac_key == SECRET
    assert "VERIFIER_HMAC_KEY" not in os.environ, "probe children must not inherit the key"


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


def test_signature_verification_rejects_stale_and_forged_requests() -> None:
    body = b'{"x":1}'
    stamp = str(int(time.time()))
    assert verify_signature(SECRET, stamp, sign(SECRET, stamp, body), body) is None
    assert verify_signature("", stamp, sign(SECRET, stamp, body), body)
    assert verify_signature(SECRET, None, None, body) == "missing signature headers"
    assert verify_signature(SECRET, "abc", "sha256=00", body) == "malformed timestamp"
    old = str(int(time.time()) - 3600)
    assert "window" in str(verify_signature(SECRET, old, sign(SECRET, old, body), body))
    assert verify_signature(SECRET, stamp, sign("other" * 8, stamp, body), body) == (
        "signature mismatch"
    )
    assert verify_signature(SECRET, stamp, sign(SECRET, stamp, body), body + b" ") == (
        "signature mismatch"
    )


async def test_verifier_requires_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sha = _git_repo(tmp_path)
    probe = await _register(tmp_path, sha, "exit 1\n")
    body = _request(probe, sha)
    async with _verifier_client(tmp_path, monkeypatch) as client:
        anonymous = await client.post("/probe", json=body)
        assert anonymous.status_code == 401
        forged = await _post(client, body, secret="not-the-key-" * 4)
        assert forged.status_code == 401
        stale = await _post(client, body, stamp=str(int(time.time()) - 3600))
        assert stale.status_code == 401
        caps = await client.get("/capabilities")
        assert caps.status_code == 401
        caps = await client.get("/capabilities", headers=_signed(b""))
        assert caps.status_code == 200
        assert caps.json()["repository_allowlist"] == ["acme/demo"]
        assert caps.json()["registry_present"] is True
        health_response = await client.get("/health")
        assert health_response.status_code == 200
    assert not list(tmp_path.glob("probe-*")), "rejected requests must not touch the workspace"


async def test_verifier_rejects_wrong_protocol_and_malformed_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sha = _git_repo(tmp_path)
    probe = await _register(tmp_path, sha, "exit 1\n")
    async with _verifier_client(tmp_path, monkeypatch) as client:
        response = await _post(client, _request(probe, sha, protocol_version="verifier.v1"))
        assert response.status_code == 400
        for bad in (
            {"commit_sha": "main"},
            {"commit_sha": sha[:39]},
            {"commit_sha": sha.upper()},
            {"repository": "../../etc"},
            {"repository": "https://evil.example/x.git"},
            {"request_id": "not-hex"},
            {"script_hash": "abc"},
            {"max_output_bytes": 10},
            {"script_content": "exit 0"},
        ):
            response = await _post(client, _request(probe, sha, **bad))
            assert response.status_code in (400, 422), bad
            assert "exit 0" not in response.text
        oversized = json.dumps(_request(probe, sha, probe_identifier="x" * 70_000)).encode()
        response = await client.post("/probe", content=oversized, headers=_signed(oversized))
        assert response.status_code == 413
    assert not list(tmp_path.glob("probe-*"))


async def test_verifier_runs_only_registry_probes_matching_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requests name a probe; they never carry one. A request for a repository outside the
    allowlist, an unregistered issue, or hashes that differ from the registry is refused."""
    _, sha = _git_repo(tmp_path)
    probe = await _register(tmp_path, sha, "exit 1\n")
    async with _verifier_client(tmp_path, monkeypatch) as client:
        ok = await _post(client, _request(probe, sha))
        assert ok.status_code == 200, ok.text
        payload = ok.json()
        assert payload["exit_code"] == 1
        assert payload["repository"] == "acme/demo" and payload["commit_sha"] == sha
        assert payload["script_hash"] == probe.script_hash
        assert payload["replayed"] is False
        assert payload["failure_stage"] is None
        assert payload["tool_versions"].get("bash")

        other = await _post(client, _request(probe, sha, repository="acme/other"))
        assert other.status_code == 403
        unregistered = await _post(client, _request(probe, sha, issue_number=8))
        assert unregistered.status_code == 422
        wrong_script = await _post(client, _request(probe, sha, script_hash="0" * 64))
        assert wrong_script.status_code == 409
        wrong_manifest = await _post(client, _request(probe, sha, manifest_hash="0" * 64))
        assert wrong_manifest.status_code == 409
        wrong_identifier = await _post(client, _request(probe, sha, probe_identifier="acme/demo#7"))
        assert wrong_identifier.status_code == 409

        # Tampering with the registry after approval must fail closed.
        (tmp_path / REGISTRY / "acme" / "demo" / "7" / "probe.sh").write_text("exit 0\n")
        tampered = await _post(client, _request(probe, sha))
        assert tampered.status_code == 422
    assert not list(tmp_path.glob("probe-*"))


async def test_verifier_replays_identical_request_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sha = _git_repo(tmp_path)
    counter = tmp_path / "count"
    counter.write_text("0")
    probe = await _register(
        tmp_path, sha, f"n=$(cat {counter}); echo $((n + 1)) > {counter}; exit 1\n"
    )
    body = _request(probe, sha)
    async with _verifier_client(tmp_path, monkeypatch) as client:
        first = await _post(client, body)
        assert first.status_code == 200, first.text
        again = await _post(client, body)
        assert again.status_code == 200
        assert again.json()["replayed"] is True
        assert again.json()["exit_code"] == first.json()["exit_code"] == 1
        fresh = await _post(client, {**body, "request_id": new_request_id()})
        assert fresh.json()["replayed"] is False
    assert counter.read_text().strip() == "2", "the probe must run once per request id"


async def test_verifier_concurrent_duplicate_waits_for_the_inflight_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sha = _git_repo(tmp_path)
    probe = await _register(tmp_path, sha, "sleep 0.5\nexit 1\n")
    body = _request(probe, sha)
    async with _verifier_client(tmp_path, monkeypatch) as client:
        first = asyncio.create_task(_post(client, body))
        await asyncio.sleep(0.1)
        second = await _post(client, body)
        response = await first
    assert response.status_code == 200 and response.json()["replayed"] is False
    assert second.status_code == 200 and second.json()["replayed"] is True
    assert second.json()["duration_ms"] == response.json()["duration_ms"]


async def test_verifier_reports_wrong_commit_as_fetch_stage_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sha = _git_repo(tmp_path)
    probe = await _register(tmp_path, sha, "exit 1\n")
    async with _verifier_client(tmp_path, monkeypatch) as client:
        response = await _post(client, _request(probe, "0" * 40))
    assert response.status_code == 200
    payload = response.json()
    assert payload["exit_code"] is None
    assert payload["failure_stage"] == "fetch"
    assert "fetch" in payload["infrastructure_error"]


def _capabilities_json(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "protocol_version": VERIFIER_PROTOCOL_VERSION,
        "isolation": {
            "uid": 65534,
            "root_writable": False,
            "no_new_privs": True,
            "effective_capabilities": "0000000000000000",
            "pids_limit": 256,
            "memory_limit_bytes": 4 * 1024**3,
            "cpu_quota": "200000 100000",
            "docker_socket_present": False,
            "credential_exposure": [],
        },
        "tools": {"git": True, "bash": True, "node": True},
        "tool_versions": {"git": "git version 2.x", "bash": "5", "node": "v24.16.0"},
        "repository_allowlist": ["acme/demo"],
        "registry_present": True,
        "max_concurrent": 1,
        "max_timeout_seconds": 3600,
        "cache_enabled": False,
        "in_flight": 0,
    }
    merged = {**base, **overrides}
    if "isolation_overrides" in overrides:
        iso = dict(base["isolation"])  # type: ignore[arg-type]
        iso.update(overrides["isolation_overrides"])  # type: ignore[arg-type]
        merged["isolation"] = iso
        merged.pop("isolation_overrides")
    return merged


class _Stub:
    def __init__(
        self,
        caps_json: dict[str, object],
        probe_json: dict[str, object] | None = None,
        *,
        caps_status: int = 200,
    ):
        self.caps_json = caps_json
        self.caps_status = caps_status
        self.probe_json = probe_json
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/capabilities":
            return httpx.Response(self.caps_status, json=self.caps_json)
        if self.probe_json is None:
            return httpx.Response(500)
        return httpx.Response(200, json=self.probe_json)


def _remote(stub: _Stub, **kwargs: object) -> RemoteProbeRunner:
    client = httpx.AsyncClient(transport=httpx.MockTransport(stub.handler), base_url="http://v")
    return RemoteProbeRunner("http://v", SECRET, client=client, **kwargs)  # type: ignore[arg-type]


def _remote_spec(sha: str = "a" * 40, script: str = "exit 1\n") -> ProbeRunSpec:
    spec = _spec(sha, script)
    return ProbeRunSpec(
        **{**spec.__dict__, "manifest_hash": "b" * 64, "request_id": new_request_id()}
    )


@pytest.mark.parametrize(
    ("caps_json", "reason"),
    [
        (
            _capabilities_json(isolation_overrides={"credential_exposure": ["GITHUB_TOKEN"]}),
            "can see credentials",
        ),
        (_capabilities_json(isolation_overrides={"uid": 0}), "runs as root"),
        (_capabilities_json(isolation_overrides={"root_writable": True}), "writable"),
        (_capabilities_json(protocol_version="verifier.v1"), "expected verifier.v2"),
        (_capabilities_json(registry_present=False), "no probe registry"),
        (_capabilities_json(isolation_overrides={"docker_socket_present": True}), "docker socket"),
        (_capabilities_json(isolation_overrides={"no_new_privs": False}), "no-new-privileges"),
        (
            _capabilities_json(isolation_overrides={"effective_capabilities": "000001ffffffffff"}),
            "capabilities",
        ),
        (_capabilities_json(isolation_overrides={"pids_limit": None}), "pids limit"),
        (_capabilities_json(isolation_overrides={"memory_limit_bytes": None}), "memory limit"),
        (_capabilities_json(repository_allowlist=["acme/other"]), "not in the verifier allowlist"),
    ],
)
async def test_remote_runner_refuses_untrustworthy_verifier(
    caps_json: dict[str, object], reason: str
) -> None:
    stub = _Stub(caps_json, probe_json={"should": "never be requested"})
    result = await _remote(stub).run(_remote_spec())
    assert result.infrastructure_error is not None
    assert reason in result.infrastructure_error
    assert result.exit_code is None
    assert result.failure_stage == "preflight"
    assert all(r.url.path == "/capabilities" for r in stub.requests), "probe must not be forwarded"


async def test_remote_runner_can_relax_cgroup_checks_only_when_told() -> None:
    """Docker Desktop may hide cgroup limits; the operator must opt out explicitly and the
    credential/root/socket checks are never relaxed."""
    relaxed = _capabilities_json(isolation_overrides={"pids_limit": None, "no_new_privs": None})
    stub = _Stub(relaxed, probe_json=None)
    result = await _remote(stub, require_isolation=False).run(_remote_spec())
    assert result.infrastructure_error is not None
    assert "verifier returned HTTP 500" in result.infrastructure_error
    stub = _Stub(_capabilities_json(isolation_overrides={"uid": 0}))
    result = await _remote(stub, require_isolation=False).run(_remote_spec())
    assert result.infrastructure_error is not None
    assert "runs as root" in result.infrastructure_error


async def test_remote_runner_rejected_signature_is_infrastructure() -> None:
    stub = _Stub({"detail": "signature mismatch"}, caps_status=401)
    result = await _remote(stub).run(_remote_spec())
    assert result.infrastructure_error is not None
    assert "PROBE_VERIFIER_SHARED_SECRET" in result.infrastructure_error


async def test_remote_runner_forwards_identity_not_script_and_checks_the_answer() -> None:
    spec = _remote_spec(script="exit 1 # super secret probe body\n")

    def good(**overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "protocol_version": VERIFIER_PROTOCOL_VERSION,
            "request_id": spec.request_id,
            "repository": spec.repository,
            "commit_sha": spec.commit_sha,
            "probe_identifier": spec.probe_identifier,
            "script_hash": spec.script_hash,
            "runner_mode": "local",
            "command_identity": command_identity(spec),
            "exit_code": 1,
            "stdout": "x",
            "stderr": "",
            "output_truncated": False,
            "timed_out": False,
            "duration_ms": 5,
            "infrastructure_error": None,
            "failure_stage": None,
            "tool_versions": {"bash": "5"},
        }
        return {**base, **overrides}

    stub = _Stub(_capabilities_json(), probe_json=good())
    result = await _remote(stub).run(spec)
    assert result.exit_code == 1 and result.infrastructure_error is None
    assert result.runner_mode == "remote"
    assert result.tool_versions == {"bash": "5"}
    sent = stub.requests[-1]
    assert sent.url.path == "/probe"
    assert sent.headers[SIGNATURE_HEADER].startswith("sha256=")
    assert (
        verify_signature(
            SECRET, sent.headers[TIMESTAMP_HEADER], sent.headers[SIGNATURE_HEADER], sent.content
        )
        is None
    )
    payload = json.loads(sent.content)
    assert payload["script_hash"] == spec.script_hash
    assert payload["manifest_hash"] == spec.manifest_hash
    assert payload["request_id"] == spec.request_id
    assert "script_content" not in payload
    assert b"super secret probe body" not in sent.content

    for bad in (
        {"command_identity": "something else"},
        {"request_id": new_request_id()},
        {"commit_sha": "b" * 40},
        {"repository": "acme/other"},
        {"script_hash": "0" * 64},
    ):
        stub = _Stub(_capabilities_json(), probe_json=good(**bad))
        result = await _remote(stub).run(spec)
        assert result.infrastructure_error is not None, bad
        assert "different execution" in result.infrastructure_error


async def test_remote_runner_requires_request_id_and_manifest_hash() -> None:
    stub = _Stub(_capabilities_json(), probe_json={"should": "never be requested"})
    result = await _remote(stub).run(_spec("a" * 40, "exit 1\n"))
    assert result.infrastructure_error is not None
    assert "manifest_hash/request_id" in result.infrastructure_error
    assert all(r.url.path == "/capabilities" for r in stub.requests)


async def test_remote_runner_busy_verifier_is_retried_not_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capabilities":
            return httpx.Response(200, json=_capabilities_json())
        return httpx.Response(409, json={"detail": "verifier is at capacity"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://v")
    with pytest.raises(VerifierBusyError):
        await RemoteProbeRunner("http://v", SECRET, client=client).run(_remote_spec())


async def test_verifier_serializes_probes_and_kills_unmarked_escapees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1: one probe in flight per verifier (409 otherwise) and, after each run, every
    remaining process of the probe UID is killed even if it dropped the run marker."""
    _, sha = _git_repo(tmp_path)
    escapee = tmp_path / "escapee.pid"

    def sweep_only_the_escapee() -> int:
        # Outside the verifier container this UID owns the whole test session, so the real
        # sweep is scoped to the one process the probe leaked.
        target = int(escapee.read_text().strip())
        keep = frozenset(int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) != target)
        return runner_module.kill_stray_processes(keep=keep)

    probe = await _register(
        tmp_path,
        sha,
        f"env -u {PROBE_RUN_MARKER} setsid bash -c 'echo $$ > {escapee}; sleep 300' "
        "</dev/null >/dev/null 2>&1 &\n"
        "sleep 0.3\nexit 1\n",
    )
    async with _verifier_client(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(verifier_module, "kill_stray_processes", sweep_only_the_escapee)
        first = asyncio.create_task(_post(client, _request(probe, sha)))
        await asyncio.sleep(0.1)
        second = await _post(client, _request(probe, sha))
        assert second.status_code == 409
        assert second.json()["detail"] == "verifier is at capacity"
        response = await first
    assert response.status_code == 200, response.text
    assert response.json()["exit_code"] == 1
    pid = int(escapee.read_text().strip())
    for _ in range(40):
        if not _alive(pid):
            break
        await asyncio.sleep(0.05)
    assert not _alive(pid), "unmarked setsid escapee survived the probe"


def _alive(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return False
    return "State:\tZ" not in status


async def test_remote_runner_unreachable_is_infrastructure_not_verdict() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="http://v")
    result = await RemoteProbeRunner("http://v", SECRET, client=client).run(_remote_spec())
    assert result.infrastructure_error is not None
    assert "unreachable" in result.infrastructure_error


def test_worker_factory_offers_only_fake_or_remote() -> None:
    assert isinstance(build_probe_runner("fake", None), FakeProbeRunner)
    assert isinstance(
        build_probe_runner("remote", "http://verifier:8080", SECRET), RemoteProbeRunner
    )
    with pytest.raises(ValueError, match="SHARED_SECRET|shared secret"):
        build_probe_runner("remote", "http://verifier:8080")
    with pytest.raises(ValueError, match="SHARED_SECRET|shared secret"):
        build_probe_runner("remote", "http://verifier:8080", "short")
    with pytest.raises(ValueError, match="'local'"):
        build_probe_runner("local", None)


def test_isolation_model_lists_unenforced_properties() -> None:
    iso = Isolation.model_validate(_capabilities_json()["isolation"])
    assert iso.credential_free is True
    assert iso.unenforced() == []
    weak = Isolation.model_validate(
        _capabilities_json(isolation_overrides={"uid": 0, "pids_limit": None})["isolation"]
    )
    assert weak.credential_free is False
    assert "runs as root" in weak.unenforced()
    assert any("pids" in p for p in weak.unenforced())


def test_verifier_entrypoint_pins_the_stdlib_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under uvloop a detached probe descendant keeps the child's stdio socketpair open and
    `Process.wait()` never resolves, so every such probe would be misreported as a timeout."""
    import uvicorn

    from remediator.verifier import __main__ as entrypoint

    monkeypatch.setenv("VERIFIER_HMAC_KEY", SECRET)
    monkeypatch.setenv("VERIFIER_REPOSITORY_ALLOWLIST", "acme/demo")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    entrypoint.main()
    assert calls and calls[0]["loop"] == "asyncio"
