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
  tools: [bash, node, npm]             # binaries the runner must find; missing = infrastructure failure
description: one line for operators
# Optional (Phase 5): dependency installation declared here, never inside probe.sh.
setup:                                 # <= 8 argv arrays, run in order before probe.sh
  - [npm, ci, --ignore-scripts, --prefix, superset-frontend]
setup_timeout_seconds: 1800            # budget for all setup steps together (default 1800)
cache_inputs:                          # <= 8 repository-relative lockfiles
  - superset-frontend/package-lock.json
resource_keys:                         # <= 8 shared resources this remediation conflicts on
  - lockfile:superset-frontend
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
- `timeout_seconds` between 1 and 21600; `runtime.tools` is a list of plain
  binary names drawn from what the verifier ships (`git`, `bash`, `python3`,
  `node`, `npm`, `yarn`).
- `setup` steps are argv arrays, not shell strings: no interpolation, no
  pipes, no NUL/newline bytes, and `argv[0]` must be one of `runtime.tools`.
  Package-manager lifecycle scripts run as the (credential-free) verifier UID;
  prefer `--ignore-scripts` unless the probe genuinely needs them.
- `cache_inputs` are repository-relative regular files (no `..`, no symlinks
  out of the checkout). They only affect *where* npm/yarn/pip download caches
  live: the key is `sha256(repository, tool versions, lockfile hashes)`, so a
  stale or poisoned cache can make installation fail (integrity is checked
  against the lockfile) but never make a probe pass.
- `resource_keys` are short opaque strings. Before a remediation session is
  created the worker takes a database lease per key (`capacity_leases`,
  kind `RESOURCE`, limit 1 per key); a second case declaring the same key
  parks (its `waiting_for` column names the key) at zero ACUs until the
  first releases it.
  Use them for lockfiles, migrations and shared configuration.

## Writing `probe.sh`

- Start with `set -euo pipefail`; the runner executes `bash probe.sh` with the
  checkout root as cwd and **no** repository-specific environment.
- Exit with the declared `base` code while the defect is present and the
  declared `head` code once fixed. Prefer `1`/`0`; a focused `pytest -k` or a
  small script that asserts the fixed behaviour is ideal.
- Assume only the tools listed in `runtime.tools` plus `git`. Install
  dependencies through `setup` steps in the manifest (reviewed, argv-only,
  cacheable) rather than from inside the script; anything the script installs
  itself counts toward `timeout_seconds` and gets no cache.
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
# Node probe with declared dependency installation, cache key and a conflict key:
uv run python scripts/register_probe.py apache/superset 4702 \
    --base-sha <40-hex> --tool bash --tool node --tool npm \
    --setup 'npm ci --ignore-scripts --prefix superset-frontend' \
    --cache-input superset-frontend/package-lock.json \
    --resource-key lockfile:superset-frontend
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
| `remote` | The worker sends an HMAC-signed `verifier.v2` request (`POST /probe` on `PROBE_VERIFIER_URL`) naming the repository, exact SHA, probe id, script hash and manifest hash after checking the signed `GET /capabilities`. The script itself is **not** sent: the verifier loads it from its own read-only registry mount and refuses when the hashes differ. A verifier that can see any credential-shaped variable or secret path, runs as UID 0, has a writable root, lacks a required tool or (with `PROBE_VERIFIER_REQUIRE_ISOLATION`, forced in live mode) cannot show `no-new-privileges`, empty capability sets and cgroup PID/memory limits is refused as an infrastructure failure. The result's request id, repository, SHA, script hash and `command_identity` must match what was requested. |

There is deliberately **no worker-side `local` mode**: the worker holds every
application credential and a scrubbed child environment is not a security
boundary (see [threat-model.md](threat-model.md)). Inside the verifier the
`LocalProbeRunner` does `git init` + `fetch --depth 1 origin <sha>` + detached
checkout in a temp directory, verifies `HEAD` is the requested SHA and the
remote is the allowlisted clone URL, runs the manifest's `setup` argv steps
(with download caches keyed as described above), then `bash <registry script>`
with a minimal environment, `min(manifest.timeout, PROBE_TIMEOUT_SECONDS,
VERIFIER_MAX_TIMEOUT_SECONDS)` covering process exit (not just output), rlimits
(`VERIFIER_MAX_PROCESSES`, `VERIFIER_MAX_FILE_SIZE_BYTES`, optional
`VERIFIER_MAX_MEMORY_BYTES`), per-stream output caps, process-group kill plus a
marker sweep that also kills `setsid`-detached descendants, a post-run sweep of
every remaining process of the probe UID (`VERIFIER_MAX_CONCURRENT` probes per
verifier, default 1; a busy verifier answers `409` and the worker retries),
and workspace cleanup. Requests are idempotent per request id: an exact
repeat is answered from memory (`replayed: true`), the same id with different
semantics is `409`, and after a verifier restart the id is simply executed
again, so a worker that lost the answer never gets a stale or fabricated one. Missing `git`/`bash`/declared tools are an infrastructure failure,
never a pass. The verifier image ships `git`, `bash`, `python3`, `node`, `npm`
and `yarn`; declare what the script needs in `runtime.tools`.

The verifier's runner also refuses to start when *its own* process can see any
credential-shaped environment variable (`DEVIN_API_KEY`, `GITHUB_TOKEN`,
`SLACK_*`, `OPERATOR_TOKEN`, `DATABASE_URL`, anything ending in `_TOKEN`,
`_SECRET`, `_API_KEY`, `_PASSWORD`, `_PRIVATE_KEY`), a `.env` file,
`/run/secrets` or `/var/run/docker.sock`; that state is also reported on
`/health` so the worker never trusts such a verifier. Run it locally with
`docker compose up verifier` or `python -m remediator.verifier` from a shell
with no secrets exported.
