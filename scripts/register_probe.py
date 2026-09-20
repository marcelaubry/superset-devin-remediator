"""Author or re-validate an immutable probe in the registry.

    uv run python scripts/register_probe.py apache/superset 4702 \
        --base-sha <40-hex> --script probe.sh --base-exit 1 --head-exit 0 \
        --timeout 600 --tool bash --tool python3

Writes `probes/<owner>/<repo>/<issue>/probe.yaml` with the SHA-256 of the script that is
already in that directory, then re-loads the probe through the registry so the result is
guaranteed to validate. Run with `--check` to only validate an existing probe.
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from remediator.probes.registry import (  # noqa: E402
    MANIFEST_FILENAME,
    PROBE_SCHEMA_VERSION,
    ProbeRegistryError,
    load_approved_probe,
    probe_directory,
    sha256_hex,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("issue_number", type=int)
    parser.add_argument("--root", default="probes")
    parser.add_argument("--check", action="store_true", help="validate only; write nothing")
    parser.add_argument("--base-sha")
    parser.add_argument("--script", default="probe.sh")
    parser.add_argument("--base-exit", type=int, default=1)
    parser.add_argument("--head-exit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--tool", action="append", default=[])
    parser.add_argument("--description", default="")
    parser.add_argument(
        "--setup",
        action="append",
        default=[],
        metavar="ARGV",
        help="dependency install step as one shell-quoted argv, e.g. 'npm ci --ignore-scripts'; "
        "argv[0] must be a --tool. Repeatable, run in order, no shell.",
    )
    parser.add_argument("--setup-timeout", type=int, default=None)
    parser.add_argument(
        "--cache-input",
        action="append",
        default=[],
        help="repository-relative lockfile hashed into the download-cache key (repeatable)",
    )
    parser.add_argument(
        "--resource-key",
        action="append",
        default=[],
        help="shared resource this remediation conflicts on, e.g. lockfile:superset-frontend "
        "(repeatable); conflicting cases queue instead of running concurrently",
    )
    args = parser.parse_args()

    root = Path(args.root)
    directory = probe_directory(root, args.repository, args.issue_number)
    if not args.check:
        if not args.base_sha:
            parser.error("--base-sha is required unless --check is given")
        script_path = directory / args.script
        if not script_path.is_file():
            parser.error(f"script {script_path} does not exist; write it first")
        manifest = {
            "schema_version": PROBE_SCHEMA_VERSION,
            "repository": args.repository,
            "issue_number": args.issue_number,
            "base_sha": args.base_sha,
            "script": args.script,
            "script_sha256": sha256_hex(script_path.read_bytes()),
            "expected_exit_codes": {"base": args.base_exit, "head": args.head_exit},
            "timeout_seconds": args.timeout,
            "runtime": {"tools": args.tool or ["bash"]},
            "description": args.description,
        }
        if args.setup:
            manifest["setup"] = [shlex.split(step) for step in args.setup]
        if args.setup_timeout is not None:
            manifest["setup_timeout_seconds"] = args.setup_timeout
        if args.cache_input:
            manifest["cache_inputs"] = list(args.cache_input)
        if args.resource_key:
            manifest["resource_keys"] = list(args.resource_key)
        (directory / MANIFEST_FILENAME).write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
    try:
        probe = asyncio.run(
            load_approved_probe(root, args.repository, args.issue_number, allow_smoke=True)
        )
    except ProbeRegistryError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"OK {probe.identifier}")
    print(f"  manifest: {probe.manifest_path}")
    print(f"  script:   {probe.script_path}")
    print(f"  base:     {probe.base_sha} expects exit {probe.expected_base_exit_code}")
    print(f"  head:     expects exit {probe.expected_head_exit_code}")
    print(f"  commit:   {probe.registry_commit or '(uncommitted)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
