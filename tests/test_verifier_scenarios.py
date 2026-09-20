"""Phase 5 verifier scenarios against local fixture repositories with known base/head commits.

Every scenario the user-facing guarantee rests on is exercised end to end through the
authenticated HTTP surface (`POST /probe`) with probes loaded only from a registry:
base fail / head pass, base unexpectedly passing, head failing, hash mismatch, wrong
repository or commit, traversal and symlink rejection, timeout and truncation, resource
exhaustion containment, bounded concurrency, idempotency, and a credential-free probe
environment. No public repository is ever cloned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_probe_runner import (
    REGISTRY,
    SECRET,
    _post,
    _register,
    _request,
    _verifier_client,
    _verifier_config,
)

from remediator.probes.registry import ProbeRegistryError, load_approved_probe, write_probe

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None, reason="git/bash required"
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}


def _git(work: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


def fixture_repo(root: Path) -> tuple[str, str]:
    """acme/demo with a `base` commit whose `check.sh` exits 1 and a `head` commit fixing it."""
    work = root / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "check.sh").write_text("#!/usr/bin/env bash\ngrep -q fixed marker.txt\n")
    (work / "marker.txt").write_text("broken\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base: defect present")
    base = _git(work, "rev-parse", "HEAD")
    (work / "marker.txt").write_text("fixed\n")
    _git(work, "commit", "-q", "-am", "head: defect fixed")
    head = _git(work, "rev-parse", "HEAD")
    bare = root / "origin" / "acme" / "demo.git"
    bare.parent.mkdir(parents=True)
    _git(root, "clone", "-q", "--bare", str(work), str(bare))
    _git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    return base, head


PROBE = "bash check.sh\n"


async def test_base_fails_then_head_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, head = fixture_repo(tmp_path)
    probe = await _register(tmp_path, base, PROBE)
    async with _verifier_client(tmp_path, monkeypatch) as client:
        on_base = (await _post(client, _request(probe, base, target="BASE"))).json()
        on_head = (await _post(client, _request(probe, head, target="HEAD"))).json()
    assert on_base["exit_code"] == probe.expected_base_exit_code == 1
    assert on_head["exit_code"] == probe.expected_head_exit_code == 0
    for evidence, sha in ((on_base, base), (on_head, head)):
        assert evidence["infrastructure_error"] is None
        assert evidence["commit_sha"] == sha
        assert evidence["script_hash"] == probe.script_hash
        assert evidence["probe_identifier"] == probe.identifier
        assert evidence["duration_ms"] >= 0
        assert set(evidence["tool_versions"]) >= {"git", "bash"}


async def test_base_unexpectedly_passes_is_reported_as_the_real_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier never interprets: the worker compares exit codes with the snapshot and
    refuses to open a paid session when the base does not reproduce the defect."""
    _, head = fixture_repo(tmp_path)
    probe = await _register(tmp_path, head, PROBE)
    async with _verifier_client(tmp_path, monkeypatch) as client:
        on_base = (await _post(client, _request(probe, head, target="BASE"))).json()
    assert on_base["exit_code"] == 0 != probe.expected_base_exit_code
    assert on_base["infrastructure_error"] is None


async def test_head_still_failing_is_a_product_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = fixture_repo(tmp_path)
    probe = await _register(tmp_path, base, PROBE)
    async with _verifier_client(tmp_path, monkeypatch) as client:
        on_head = (await _post(client, _request(probe, base, target="HEAD"))).json()
    assert on_head["exit_code"] == 1 != probe.expected_head_exit_code
    assert on_head["infrastructure_error"] is None
    assert on_head["failure_stage"] is None


async def test_hash_mismatch_and_altered_registry_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = fixture_repo(tmp_path)
    probe = await _register(tmp_path, base, PROBE)
    async with _verifier_client(tmp_path, monkeypatch) as client:
        wrong_script = await _post(client, _request(probe, base, script_hash="0" * 64))
        wrong_manifest = await _post(client, _request(probe, base, manifest_hash="f" * 64))
        assert wrong_script.status_code == 409
        assert wrong_manifest.status_code == 409
        # Someone edits the registered script after approval: the registry itself refuses.
        (tmp_path / REGISTRY / "acme" / "demo" / "7" / "probe.sh").write_text("exit 0\n")
        altered = await _post(client, _request(probe, base))
        assert altered.status_code == 422
        assert altered.json()["detail"] == "no approved probe in the verifier registry"


async def test_wrong_repository_and_commit_are_refused_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = fixture_repo(tmp_path)
    probe = await _register(tmp_path, base, PROBE)
    async with _verifier_client(tmp_path, monkeypatch) as client:
        other = await _post(client, _request(probe, base, repository="evil/demo"))
        assert other.status_code == 403
        missing = (await _post(client, _request(probe, "e" * 40))).json()
    assert missing["exit_code"] is None
    assert missing["failure_stage"] == "fetch"
    assert "e" * 40 in missing["infrastructure_error"]


async def test_registry_rejects_traversal_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / REGISTRY
    base = "a" * 40
    write_probe(
        root,
        repository="acme/demo",
        issue_number=7,
        base_sha=base,
        script=PROBE,
        expected_base_exit_code=1,
        expected_head_exit_code=0,
    )
    directory = root / "acme" / "demo" / "7"
    manifest = directory / "probe.yaml"
    original = manifest.read_text()

    manifest.write_text(original.replace("script: probe.sh", "script: ../../../etc/passwd"))
    with pytest.raises(ProbeRegistryError, match="inside the probe directory"):
        await load_approved_probe(root, "acme/demo", 7)

    manifest.write_text(original)
    outside = tmp_path / "outside.sh"
    outside.write_text(PROBE)
    (directory / "probe.sh").unlink()
    (directory / "probe.sh").symlink_to(outside)
    with pytest.raises(ProbeRegistryError, match="not found"):
        await load_approved_probe(root, "acme/demo", 7)

    (directory / "probe.sh").unlink()
    (directory / "probe.sh").write_text(PROBE)
    shutil.move(str(directory), str(tmp_path / "moved"))
    directory.symlink_to(tmp_path / "moved")
    with pytest.raises(ProbeRegistryError, match="symlink"):
        await load_approved_probe(root, "acme/demo", 7)

    for bad in ("../acme/demo", "acme/../demo", "acme"):
        with pytest.raises(ProbeRegistryError):
            await load_approved_probe(root, bad, 7)


async def test_timeout_and_output_truncation_are_bounded_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = fixture_repo(tmp_path)
    chatty = await _register(tmp_path, base, "yes | head -c 200000\nexit 1\n", issue_number=7)
    hang = await _register(tmp_path, base, "sleep 30\n", issue_number=8)
    async with _verifier_client(tmp_path, monkeypatch) as client:
        truncated = (await _post(client, _request(chatty, base, max_output_bytes=2048))).json()
        timed_out = (await _post(client, _request(hang, base, timeout_seconds=1))).json()
    assert truncated["output_truncated"] is True
    assert len(truncated["stdout"].encode()) <= 2048
    assert truncated["exit_code"] == 1
    assert timed_out["timed_out"] is True
    assert timed_out["exit_code"] != 0


async def test_resource_exhaustion_is_contained_and_the_verifier_keeps_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RLIMIT_FSIZE / RLIMIT_AS apply to the probe process tree only: a probe that tries to
    fill the disk or allocate unbounded memory fails on its own, and the next request runs."""
    base, head = fixture_repo(tmp_path)
    disk_hog = await _register(
        tmp_path, base, "head -c 4000000 /dev/zero > big.bin; echo rc=$?\nexit 1\n", issue_number=7
    )
    mem_hog = await _register(
        tmp_path,
        base,
        "python3 -c 'x = bytearray(900 * 1024 * 1024)' && exit 0\nexit 1\n",
        issue_number=8,
        tools=("bash", "python3"),
    )
    honest = await _register(tmp_path, base, PROBE, issue_number=9)
    async with _verifier_client(
        tmp_path,
        monkeypatch,
        VERIFIER_MAX_FILE_SIZE_BYTES=str(1024 * 1024),
        VERIFIER_MAX_MEMORY_BYTES=str(512 * 1024 * 1024),
    ) as client:
        disk = (await _post(client, _request(disk_hog, base))).json()
        mem = (await _post(client, _request(mem_hog, base))).json()
        after = (await _post(client, _request(honest, head, target="HEAD"))).json()
    assert disk["exit_code"] == 1 and "rc=" in disk["stdout"] and "rc=0" not in disk["stdout"]
    assert mem["exit_code"] == 1
    assert after["exit_code"] == 0 and after["infrastructure_error"] is None


async def test_concurrent_jobs_serialize_on_the_single_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One probe at a time: the post-run UID-wide sweep would kill a concurrent run, so the
    verifier refuses to be configured for more and answers 409 while the slot is taken."""
    base, _ = fixture_repo(tmp_path)
    probes = [
        await _register(tmp_path, base, "sleep 1.5\nexit 1\n", issue_number=n) for n in (7, 8)
    ]
    with pytest.raises(ValueError, match="VERIFIER_MAX_CONCURRENT must be 1"):
        _verifier_config(tmp_path, VERIFIER_MAX_CONCURRENT="2")
    async with _verifier_client(tmp_path, monkeypatch) as client:
        first_task = asyncio.create_task(_post(client, _request(probes[0], base)))
        await asyncio.sleep(0.3)
        second = await _post(client, _request(probes[1], base))
        assert second.status_code == 409
        first = await first_task
        assert first.status_code == 200
        again = await _post(client, _request(probes[1], base))
        assert again.status_code == 200


async def test_repeated_request_id_replays_without_re_executing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = fixture_repo(tmp_path)
    counter = tmp_path / "runs"
    probe = await _register(tmp_path, base, f"echo x >> {counter}\nexit 1\n")
    body = _request(probe, base)
    async with _verifier_client(tmp_path, monkeypatch) as client:
        first = (await _post(client, body)).json()
        second = (await _post(client, body)).json()
        # Same id with a different body is a protocol violation, not a replay.
        tampered = await _post(client, {**body, "target": "HEAD"})
    assert first["replayed"] is False and second["replayed"] is True
    assert second["exit_code"] == first["exit_code"] == 1
    assert counter.read_text().count("x") == 1
    assert tampered.status_code == 409


async def test_probe_environment_filesystem_and_logs_carry_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Even when the verifier *process* is polluted, the probe child inherits nothing but a
    minimal environment, its workspace, and no open descriptors beyond stdio."""
    planted = {
        "DEVIN_API_KEY": "devin-planted-secret-value",
        "GITHUB_TOKEN": "ghp_plantedsecretvalue",
        "DATABASE_URL": "postgresql://user:plantedpassword@db/x",
        "SLACK_BOT_TOKEN": "xoxb-planted",
    }
    for key, value in planted.items():
        monkeypatch.setenv(key, value)
    base, _ = fixture_repo(tmp_path)
    probe = await _register(
        tmp_path,
        base,
        "env\necho ---FDS---\nls -l /proc/self/fd\necho ---FS---\n"
        "ls -a / /run/secrets 2>/dev/null; cat /proc/self/status | grep -i uid\nexit 1\n",
    )
    caplog.set_level(logging.DEBUG)
    async with _verifier_client(tmp_path, monkeypatch) as client:
        evidence = (await _post(client, _request(probe, base, max_output_bytes=65536))).json()
    stdout: str = evidence["stdout"]
    for value in planted.values():
        assert value not in stdout
    for key in planted:
        assert f"{key}=" not in stdout
    env_block = stdout.split("---FDS---")[0]
    assert "PATH=" in env_block and "HOME=" in env_block
    fds = stdout.split("---FDS---")[1].split("---FS---")[0]
    # `ls` itself holds one descriptor on the directory it lists; nothing else is inherited.
    descriptors = [line for line in fds.splitlines() if "->" in line and "/fd" not in line]
    assert len(descriptors) == 3 and all(
        any(f" {n} ->" in d for n in ("0", "1", "2")) for d in descriptors
    ), fds
    joined = "\n".join(r.getMessage() for r in caplog.records) + caplog.text
    for value in (SECRET, *planted.values()):
        assert value not in joined
    assert "/.env" not in stdout


async def test_workspace_is_removed_after_every_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = fixture_repo(tmp_path)
    where = tmp_path / "where"
    probe = await _register(tmp_path, base, f"pwd > {where}\ntouch leftover\nexit 1\n")
    async with _verifier_client(tmp_path, monkeypatch) as client:
        assert (await _post(client, _request(probe, base))).status_code == 200
    workspace = Path(where.read_text().strip())
    assert workspace.is_relative_to(tmp_path)
    assert not workspace.exists()
