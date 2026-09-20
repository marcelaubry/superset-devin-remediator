# Known limitations (after Phase 4)

## Independent probe execution inside the worker container is not safe

The Phase 4 verifier (`LocalProbeRunner`) is complete: isolated checkout of the
exact SHA, snapshotted script only, fixed argv, minimal environment, timeout,
output caps, process-group kill, cleanup, and "missing dependency = failure".
It is nevertheless **not enabled for production** because the only container
that could host it today is the worker, and the worker holds the Devin API
key, GitHub token, Slack token/signing secret, operator token and database
URL.

Scrubbing the child's environment does not make that safe:

- the probe runs the checked-out commit's own tooling (pytest plugins,
  `conftest.py`, `setup.py`, npm scripts) as the same UID as the worker, so it
  can read `/proc/<worker-pid>/environ`, the worker's `.env` mount, or any
  file the worker can;
- it shares the worker's network namespace, so it can reach PostgreSQL (state
  tampering), the Devin API, and the operator API with credentials it finds;
- Docker secrets (`/run/secrets`) or a mounted Docker socket would be visible
  to it directly.

Because of this the runner refuses to start when any credential-shaped
variable or secret path is visible to the hosting process, and `Settings`
refuses `DEVIN_CLIENT_MODE=live` unless
`PROBE_VERIFIER_ISOLATION=credential_free_container` is set. That flag is an
operator attestation, not a runtime guarantee; setting it while running probes
inside the worker re-creates the exposure described above.

**Phase 5 production path:** a dedicated `verifier` service in the compose
stack with no secrets in its environment, a read-only image containing only
`git`, `bash` and the runtimes probes may declare, egress restricted to
anonymous `https://github.com` clones, no access to the application database
credentials, that consumes probe run requests (snapshot id, target, SHA) via a
narrow queue table or API and returns exit code, duration and bounded output
into `probe_executions`. The worker then never executes repository code at all.
Until that exists, live-mode remediation cannot start, by design.

## Probe runtime availability

- The local runner clones anonymously over HTTPS; private forks or GitHub
  outages surface as `PROBE_INFRASTRUCTURE_BLOCKED` / infrastructure failure,
  never as a pass.
- The remediator image (`python:3.11-slim`) does not contain the Superset
  toolchain. Probes whose `runtime.tools` are absent are infrastructure
  failures. Real Superset probes need the verifier image above.
- Probe timeouts are capped by `PROBE_TIMEOUT_SECONDS`; a test suite slower
  than that cannot be used as a probe.

## Remediation flow

- Only one probe per issue is supported; multi-step or environment-dependent
  reproductions must be composed inside `probe.sh`.
- `no_change_needed` at base halts the case as human-blocked. If Devin was
  never started there is no PR to review; the operator decides whether the
  triage or the probe was wrong.
- Head-probe or CI failure ends the attempt. No follow-up Devin session is
  ever launched automatically; a human must inspect and use the authenticated
  retry, which creates a fresh attempt and operation key.
- The PR validator reads `pull_requests[]` and GitHub only. A PR Devin opened
  but failed to report is not discovered; it remains visible on GitHub for
  humans.
- Corroboration requires the exact head SHA reported in structured output.
  If Devin pushes after reporting, the PR is rejected (SHA mismatch) rather
  than re-verified.
- `compare` status `diverged` (base moved ahead after pinning) is
  human-blocked rather than auto-rebased; an operator decides whether to
  retry against the new base.

## CI tracking

- Check runs are polled (`CI_POLL_INTERVAL_SECONDS`, bounded by
  `CI_TIMEOUT_SECONDS`) rather than ingested from `check_suite` webhooks;
  GitHub API rate limits apply.
- Only the Checks API is consulted. Legacy commit statuses (`/statuses`) are
  not read; repositories relying on them will look `absent`.
- Without `GITHUB_REQUIRED_CHECKS`, "passed" means every check run GitHub
  reports completed with `success` (or a neutral/skipped conclusion) and none
  is pending; branch-protection required-check settings are not read from
  GitHub. With the list set, each named check must exist and succeed.
- CI outcomes are for the verified head SHA only. A later push starts nothing;
  the case stays at its recorded state.

## Human review remains mandatory

`CI_PASSED` ("Ready for human review") is the terminal automated state. The
remediator never merges, never closes the issue, never approves the PR, and
never dismisses reviews. Branch protection requiring human review on the
default branch is assumed and should be enforced independently of this
service.

## Operational

- Live GitHub uses a personal access token identity; PR author verification
  therefore depends on `GITHUB_PR_AUTHOR_LOGINS` matching Devin's GitHub App
  login (`devin-ai-integration[bot]` by default). A GitHub App installation
  identity for the remediator itself is Phase 5.
- Probe output and PR evidence accumulate without retention; bounded per row
  but not pruned.
- Slack updates are best-effort through the outbox; a permanently failing
  Slack channel leaves the case advancing with stale Slack state, visible on
  the dashboard and in `notification_outbox`.
