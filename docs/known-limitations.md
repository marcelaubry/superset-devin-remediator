# Known limitations (after Phase 4)

## Probe execution boundary: the verifier container

The worker holds the Devin API key, GitHub token, Slack token/signing secret,
operator token and database URL, so it **never executes repository code**:
there is no worker-side `local` probe mode, only `fake` (simulation) and
`remote`. Repository code runs in the dedicated `verifier` service
(`docker/verifier/Dockerfile`, `remediator.verifier`): no `env_file`, no
import of `remediator.config` (so no `.env` is ever read), dedicated non-root
UID, read-only root filesystem, bounded `/tmp` tmpfs, `cap_drop: ALL`,
`no-new-privileges`, PID/memory limits, isolated network. The worker checks
`GET /health` before every probe and refuses a verifier that can see any
credential, runs as root or has a writable root.

What is still a limitation:

- **Egress is documented, not enforced, by compose.** The verifier network has
  no route to PostgreSQL or the worker, but Docker's default bridge still allows
  outbound internet (needed for the anonymous `https://github.com` clone).
  Production should pin egress to GitHub with a network policy / egress proxy;
  a malicious probe can otherwise exfiltrate the repository contents it already
  has (it has no credentials to exfiltrate).
- **Containment is per container, not per probe.** Descendants that both
  clear their environment (dropping the run marker) and `setsid` out of the
  process group survive until the container's `pids_limit`/restart. One probe
  run at a time per verifier is the intended deployment; a compromised probe
  can interfere with a concurrent one in the same container.
- `RLIMIT_NPROC` is per UID, so it is only meaningful because the verifier UID
  runs nothing else. `RLIMIT_AS` is off by default (`VERIFIER_MAX_MEMORY_BYTES`)
  because Node/JVM toolchains reserve large address spaces; the container
  `mem_limit` is the effective memory bound.
- The verifier is reached over plain HTTP on an internal network. Nothing
  secret crosses it (the probe script is public repository content and the
  result is evidence, not authority), but an attacker on that network could
  feed the worker false verdicts. Production should authenticate the link
  (mTLS or a shared-nothing sidecar).

## Probe runtime availability

- The verifier clones anonymously over HTTPS; private forks or GitHub
  outages surface as `PROBE_INFRASTRUCTURE_BLOCKED` / infrastructure failure,
  never as a pass.
- The verifier image ships `git`, `bash`, `python3`, `node`, `npm` and `yarn`,
  so Node-based probes (`runtime.tools: [node, npm]`) are supported there. The
  worker image contains none of this and cannot run probes at all. Probes
  whose `runtime.tools` are absent from the verifier are infrastructure
  failures, never a pass. The full Superset toolchain (Python deps, a browser)
  is still not preinstalled; a probe must install what it needs inside its own
  timeout or the verifier image must be extended.
- Probe timeouts are capped by `PROBE_TIMEOUT_SECONDS` and, independently, by
  the verifier's `VERIFIER_MAX_TIMEOUT_SECONDS`; a test suite slower than that
  cannot be used as a probe.

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
