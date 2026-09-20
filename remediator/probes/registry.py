"""Versioned, content-addressed probe registry.

Layout (relative to ``PROBE_ROOT``)::

    <owner>/<repo>/<issue-number>/probe.yaml
    <owner>/<repo>/<issue-number>/probe.sh

The manifest pins the repository, issue, base SHA, expected exit codes, timeout, runtime
requirements and the SHA-256 of the script. Anything that does not validate is treated as
"no approved probe": the case is blocked before any ACU is spent. Probe commands that
arrive in issue text, Slack messages or Devin output are never consulted here.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROBE_SCHEMA_VERSION = "probe.v1"
MANIFEST_FILENAME = "probe.yaml"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
_MAX_SCRIPT_BYTES = 256 * 1024
_MIN_TIMEOUT = 1
_MAX_TIMEOUT = 6 * 3600
_MAX_SETUP_STEPS = 8
_MAX_ARGV_ITEMS = 32
_MAX_ARG_LENGTH = 256
_MAX_CACHE_INPUTS = 8
_MAX_RESOURCE_KEYS = 8
RESOURCE_KEY_MAX_LENGTH = 128
_REL_PATH_RE = re.compile(r"^[A-Za-z0-9_.@-]+(/[A-Za-z0-9_.@-]+)*$")


class ProbeRegistryError(Exception):
    """The probe for this issue is missing, malformed or does not match its hash."""


@dataclass(frozen=True)
class ApprovedProbe:
    repository: str
    issue_number: int
    base_sha: str
    manifest_path: str
    script_path: str
    manifest: dict[str, Any]
    manifest_hash: str
    script_hash: str
    script_content: str
    expected_base_exit_code: int
    expected_head_exit_code: int
    timeout_seconds: int
    runtime: dict[str, Any]
    registry_commit: str | None

    @property
    def identifier(self) -> str:
        return probe_identifier(self.repository, self.issue_number, self.script_hash)

    @property
    def required_tools(self) -> tuple[str, ...]:
        tools = self.runtime.get("tools", [])
        return tuple(str(t) for t in tools)


def validate_resource_key(key: str) -> str:
    """Resource keys come from the approved probe manifest; keep them boring."""
    key = key.strip().lower()
    if not key or len(key) > RESOURCE_KEY_MAX_LENGTH:
        raise ValueError("resource key must be 1-128 characters")
    if not all(ch.isalnum() or ch in "-_./:" for ch in key):
        raise ValueError(f"resource key {key!r} has characters outside [a-z0-9-_./:]")
    if ".." in key or key.startswith("/"):
        raise ValueError(f"resource key {key!r} may not look like a path")
    return key


def _validate_relative_path(value: Any, what: str, manifest_path: Path) -> str:
    if not isinstance(value, str) or not _REL_PATH_RE.match(value) or ".." in value.split("/"):
        raise ProbeRegistryError(
            f"{manifest_path}: {what} {value!r} must be a plain repository-relative path"
        )
    return value


def _validate_setup(manifest: dict[str, Any], tools: list[str], manifest_path: Path) -> None:
    """`setup` is a list of argv arrays; every argv[0] must be a declared runtime tool so a
    manifest can only install dependencies with the toolchain it already requires."""
    steps = manifest.get("setup", [])
    if not isinstance(steps, list) or len(steps) > _MAX_SETUP_STEPS:
        raise ProbeRegistryError(f"{manifest_path}: setup must be a list of at most 8 argv arrays")
    for step in steps:
        if (
            not isinstance(step, list)
            or not step
            or len(step) > _MAX_ARGV_ITEMS
            or not all(isinstance(a, str) and 0 < len(a) <= _MAX_ARG_LENGTH for a in step)
        ):
            raise ProbeRegistryError(f"{manifest_path}: setup step {step!r} is not a valid argv")
        if step[0] not in tools or "/" in step[0]:
            raise ProbeRegistryError(
                f"{manifest_path}: setup step {step[0]!r} is not one of runtime.tools"
            )
        if any("\0" in a or "\n" in a for a in step):
            raise ProbeRegistryError(f"{manifest_path}: setup argv may not contain NUL/newline")
    if "setup_timeout_seconds" in manifest:
        _require_int(manifest, "setup_timeout_seconds", lo=_MIN_TIMEOUT, hi=_MAX_TIMEOUT)
    inputs = manifest.get("cache_inputs", [])
    if not isinstance(inputs, list) or len(inputs) > _MAX_CACHE_INPUTS:
        raise ProbeRegistryError(f"{manifest_path}: cache_inputs must be a short list of paths")
    for item in inputs:
        _validate_relative_path(item, "cache_inputs entry", manifest_path)
    keys = manifest.get("resource_keys", [])
    if not isinstance(keys, list) or len(keys) > _MAX_RESOURCE_KEYS:
        raise ProbeRegistryError(f"{manifest_path}: resource_keys must be a short list")
    for key in keys:
        try:
            validate_resource_key(str(key))
        except ValueError as exc:
            raise ProbeRegistryError(f"{manifest_path}: {exc}") from exc


def manifest_setup_steps(manifest: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(str(a) for a in step) for step in manifest.get("setup", []) or [])


def manifest_cache_inputs(manifest: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(p) for p in manifest.get("cache_inputs", []) or [])


def manifest_setup_timeout(manifest: dict[str, Any]) -> int:
    value = manifest.get("setup_timeout_seconds", 1800)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 1800


def probe_identifier(repository: str, issue_number: int, script_hash: str) -> str:
    return f"{repository}#{issue_number}@sha256:{script_hash[:16]}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_manifest_hash(manifest: dict[str, Any]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256_hex(canonical.encode())


def probe_directory(root: Path, repository: str, issue_number: int) -> Path:
    owner, _, name = repository.partition("/")
    if not owner or not name or "/" in name or ".." in repository:
        raise ProbeRegistryError(f"repository {repository!r} is not owner/name")
    return root / owner / name / str(issue_number)


def _require_int(manifest: dict[str, Any], key: str, *, lo: int, hi: int) -> int:
    value = manifest.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProbeRegistryError(f"manifest field {key!r} must be an integer")
    if not lo <= value <= hi:
        raise ProbeRegistryError(f"manifest field {key!r} must be within [{lo}, {hi}]")
    return value


def validate_manifest(
    manifest: Any, *, repository: str, issue_number: int, manifest_path: Path
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ProbeRegistryError(f"{manifest_path} must be a mapping")
    if manifest.get("schema_version") != PROBE_SCHEMA_VERSION:
        raise ProbeRegistryError(
            f"{manifest_path}: schema_version must be {PROBE_SCHEMA_VERSION!r}"
        )
    if manifest.get("repository") != repository:
        raise ProbeRegistryError(
            f"{manifest_path}: repository {manifest.get('repository')!r} != {repository!r}"
        )
    if manifest.get("issue_number") != issue_number:
        raise ProbeRegistryError(
            f"{manifest_path}: issue_number {manifest.get('issue_number')!r} != {issue_number}"
        )
    base_sha = manifest.get("base_sha")
    if not isinstance(base_sha, str) or not _SHA_RE.match(base_sha):
        raise ProbeRegistryError(f"{manifest_path}: base_sha must be a 40-hex commit SHA")
    script = manifest.get("script")
    if (
        not isinstance(script, str)
        or not script
        or "/" in script
        or script.startswith(".")
        or script in {MANIFEST_FILENAME}
    ):
        raise ProbeRegistryError(
            f"{manifest_path}: script must name a file inside the probe directory"
        )
    codes = manifest.get("expected_exit_codes")
    if not isinstance(codes, dict):
        raise ProbeRegistryError(f"{manifest_path}: expected_exit_codes must be a mapping")
    base_code = _require_int(codes, "base", lo=0, hi=255)
    head_code = _require_int(codes, "head", lo=0, hi=255)
    if base_code == head_code:
        raise ProbeRegistryError(
            f"{manifest_path}: expected base and head exit codes must differ, otherwise the "
            "probe cannot distinguish a fix from no change"
        )
    _require_int(manifest, "timeout_seconds", lo=_MIN_TIMEOUT, hi=_MAX_TIMEOUT)
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        raise ProbeRegistryError(f"{manifest_path}: runtime must be a mapping")
    tools = runtime.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ProbeRegistryError(f"{manifest_path}: runtime.tools must list required executables")
    for tool in tools:
        if not isinstance(tool, str) or not _TOOL_RE.match(tool):
            raise ProbeRegistryError(f"{manifest_path}: runtime.tools entry {tool!r} is invalid")
    digest = manifest.get("script_sha256")
    if not isinstance(digest, str) or not _HEX256_RE.match(digest):
        raise ProbeRegistryError(f"{manifest_path}: script_sha256 must be a 64-hex digest")
    _validate_setup(manifest, [str(t) for t in tools], manifest_path)
    return manifest


async def registry_commit_for(root: Path, relative: Path) -> str | None:
    """Commit in the registry's own git history that last touched the probe, if any."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            "log",
            "-1",
            "--format=%H",
            "--",
            str(relative),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (FileNotFoundError, PermissionError):
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except TimeoutError:
        proc.kill()
        return None
    sha = stdout.decode(errors="replace").strip()
    return sha if proc.returncode == 0 and _SHA_RE.match(sha) else None


async def load_approved_probe(root: Path, repository: str, issue_number: int) -> ApprovedProbe:
    root = root.resolve()
    directory = probe_directory(root, repository, issue_number)
    manifest_path = directory / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ProbeRegistryError(f"no approved probe registered at {manifest_path}")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProbeRegistryError(f"{manifest_path}: unreadable manifest ({exc})") from exc
    manifest = validate_manifest(
        raw, repository=repository, issue_number=issue_number, manifest_path=manifest_path
    )
    script_path = (directory / manifest["script"]).resolve()
    if script_path.parent != directory.resolve() or not script_path.is_file():
        raise ProbeRegistryError(f"{manifest_path}: script {manifest['script']!r} not found")
    try:
        script_bytes = script_path.read_bytes()
    except OSError as exc:
        raise ProbeRegistryError(f"{script_path}: unreadable script ({exc})") from exc
    if len(script_bytes) > _MAX_SCRIPT_BYTES:
        raise ProbeRegistryError(f"{script_path}: script exceeds {_MAX_SCRIPT_BYTES} bytes")
    script_hash = sha256_hex(script_bytes)
    if script_hash != manifest["script_sha256"]:
        raise ProbeRegistryError(
            f"{script_path}: content hash {script_hash[:12]} does not match manifest "
            f"script_sha256 {manifest['script_sha256'][:12]}; probe was altered"
        )
    try:
        script_content = script_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeRegistryError(f"{script_path}: script must be UTF-8") from exc
    commit = await registry_commit_for(root, directory.relative_to(root))
    return ApprovedProbe(
        repository=repository,
        issue_number=issue_number,
        base_sha=manifest["base_sha"],
        manifest_path=str(manifest_path),
        script_path=str(script_path),
        manifest=manifest,
        manifest_hash=canonical_manifest_hash(manifest),
        script_hash=script_hash,
        script_content=script_content,
        expected_base_exit_code=manifest["expected_exit_codes"]["base"],
        expected_head_exit_code=manifest["expected_exit_codes"]["head"],
        timeout_seconds=manifest["timeout_seconds"],
        runtime=manifest["runtime"],
        registry_commit=commit,
    )


def write_probe(
    root: Path,
    *,
    repository: str,
    issue_number: int,
    base_sha: str,
    script: str,
    expected_base_exit_code: int,
    expected_head_exit_code: int,
    timeout_seconds: int = 600,
    tools: tuple[str, ...] = ("bash",),
    description: str = "",
    setup: tuple[tuple[str, ...], ...] = (),
    setup_timeout_seconds: int | None = None,
    cache_inputs: tuple[str, ...] = (),
    resource_keys: tuple[str, ...] = (),
) -> Path:
    """Author a probe (used by tests, simulations and `scripts/probe_tool.py`)."""
    directory = probe_directory(root, repository, issue_number)
    directory.mkdir(parents=True, exist_ok=True)
    script_bytes = script.encode("utf-8")
    (directory / "probe.sh").write_bytes(script_bytes)
    manifest: dict[str, Any] = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "repository": repository,
        "issue_number": issue_number,
        "base_sha": base_sha,
        "script": "probe.sh",
        "expected_exit_codes": {"base": expected_base_exit_code, "head": expected_head_exit_code},
        "timeout_seconds": timeout_seconds,
        "runtime": {"tools": list(tools), "description": description},
        "script_sha256": sha256_hex(script_bytes),
    }
    if setup:
        manifest["setup"] = [list(step) for step in setup]
    if setup_timeout_seconds is not None:
        manifest["setup_timeout_seconds"] = setup_timeout_seconds
    if cache_inputs:
        manifest["cache_inputs"] = list(cache_inputs)
    if resource_keys:
        manifest["resource_keys"] = list(resource_keys)
    manifest_path = directory / MANIFEST_FILENAME
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return manifest_path
