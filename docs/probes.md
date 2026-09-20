# Probe authoring guide

A probe is the only independent evidence the remediator accepts that an issue
is real (it fails at the pinned base commit) and that a PR fixes it (the same
probe passes at the PR head). Probes are authored by humans, reviewed in this
repository, and are never derived from issue text, Slack, or Devin output.

## Layout

```text
probes/<owner>/<repo>/<issue-number>/
├── probe.yaml   manifest (validated by remediator/probes/registry.py)
└── probe.sh     the script; executed as `bash probe.sh` from the checkout root
```

`PROBE_ROOT` (default `probes`) must be committed to this repository so the
registry commit can be recorded on the snapshot.

## Manifest

```yaml
schema_version: probe.v1
repository: apache/superset            # must be allowlisted and match the path
issue_number: 4702                     # must match the directory name
base_sha: c4f3751792f6a1bb61eca7b54d1aa06a1306bc50   # 40-hex, the commit triage pinned
script: probe.sh                       # relative, inside the probe directory
script_sha256: cfcda9b1...bd61df4      # sha256 of probe.sh; recomputed on every load
expected_exit_codes:
  base: 1                              # exit code that proves the defect at base_sha
  head: 0                              # exit code that proves the fix at the PR head
timeout_seconds: 600                   # capped by PROBE_TIMEOUT_SECONDS
runtime:
  tools: [bash, python3]               # binaries the runner must find; missing = infrastructure failure
description: one line for operators
```

Rules enforced by the registry loader (any violation is `approved probe
unavailable` at dispatch, zero ACUs):

- `repository` and `issue_number` must equal the directory path.
- `base_sha` must be 40 lowercase hex characters. It is the base the probe
  runs against and the base the PR must be ahead of.
- `script` must resolve inside the probe directory (no `..`, no absolute
  paths, no symlinks out).
- `script_sha256` must equal the SHA-256 of the script bytes. Editing the
  script without updating the manifest, or vice versa, invalidates the probe.
- `expected_exit_codes.base != expected_exit_codes.head`.
- `timeout_seconds > 0`; `runtime.tools` is a list of plain binary names.

## Writing `probe.sh`

- Start with `set -euo pipefail`; the runner executes `bash probe.sh` with the
  checkout root as cwd and **no** repository-specific environment.
- Exit with the declared `base` code while the defect is present and the
  declared `head` code once fixed. Prefer `1`/`0`; a focused `pytest -k` or a
  small script that asserts the fixed behaviour is ideal.
- Assume only the tools listed in `runtime.tools` plus `git`; anything else
  must be installed by the script from the checkout (and will count toward
  the timeout).
- Never read secrets or the network beyond what the repository's own tests
  need; the verifier has no credentials to offer.
- Keep output small; stdout/stderr are captured up to `PROBE_MAX_OUTPUT_BYTES`
  each and shown to operators.
- Do not reference the remediation branch, the PR, or Devin. The identical
  script runs at both commits.

## Registering

```bash
# write probe.sh first, then:
uv run python scripts/register_probe.py apache/superset 4702 \
    --base-sha <40-hex> --script probe.sh --base-exit 1 --head-exit 0 \
    --timeout 600 --tool bash --tool python3 --description "..."
# validate an existing probe without writing:
uv run python scripts/register_probe.py apache/superset 4702 --check
```

`register_probe.py` computes `script_sha256`, writes the manifest, and reloads
it through the registry so the committed probe is guaranteed to validate.
Commit both files; the registry commit is stored on the snapshot.

## What happens at dispatch

1. Preconditions pass (approval bound to the exact triage hash, label present,
   nothing active).
2. The manifest and script are loaded and validated; a `probe_snapshots` row
   freezes the manifest, script content, both hashes, and the registry commit.
3. The snapshot runs at `base_sha`. Only `expected_exit_codes.base` allows a
   Devin session. A `head` exit code at base is `no_change_needed`
   (human-blocked); a missing tool is `PROBE_INFRASTRUCTURE_BLOCKED`; anything
   else is `REMEDIATION_FAILED`. None of these spend ACUs.
4. After the PR is validated against GitHub, the **same snapshot** runs at the
   PR head SHA and must return `expected_exit_codes.head`.

Changing the probe after dispatch does not affect an in-flight case: every run
uses the snapshot, and the PR validator rejects PRs that touch `probes/`.

## Runners

| `PROBE_RUNNER_MODE` | Behaviour |
| --- | --- |
| `fake` (default) | No execution. Exit codes are chosen from the fixture issue number (`remediator/fixtures.py`); used by tests and simulations. |
| `local` | `git init` + `fetch --depth 1 origin <sha>` + detached checkout in a temp directory, then `bash <snapshotted script>` with a minimal environment, `min(manifest.timeout, PROBE_TIMEOUT_SECONDS)`, per-stream output caps, process-group kill on timeout, and workspace cleanup. Missing `git`/`bash`/declared tools are reported as an infrastructure failure, never as a pass. |

The local runner refuses to start when the hosting process can see any
credential-shaped environment variable (`DEVIN_API_KEY`, `GITHUB_TOKEN`,
`SLACK_*`, `OPERATOR_TOKEN`, `DATABASE_URL`, anything ending in `_TOKEN`,
`_SECRET`, `_API_KEY`, `_PASSWORD`, `_PRIVATE_KEY`) or `/run/secrets` /
`/var/run/docker.sock`. That is deliberate: see
[known-limitations.md](known-limitations.md) for the verifier-container plan.
