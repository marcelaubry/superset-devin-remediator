# Known limitations (after Phase 5)

## Probe execution boundary: the verifier container

The worker holds the Devin API key, GitHub token, Slack token/signing secret,
operator token and database URL, so it **never executes repository code**:
there is no worker-side `local` probe mode, only `fake` (simulation) and
`remote`. Repository code runs in the dedicated `verifier` service
(`docker/verifier/Dockerfile`, `remediator.verifier`): no `env_file`, no
import of `remediator.config` (so no `.env` is ever read), dedicated non-root
UID, read-only root filesystem, bounded `/tmp` tmpfs, `cap_drop: ALL`,
`no-new-privileges`, PID/memory/CPU limits, isolated network, HMAC-signed
`verifier.v2` protocol. The worker checks the signed `GET /capabilities`
before every probe and refuses a verifier that can see any credential, runs as
root, has a writable root, lacks a declared tool or cannot show the compose
hardening (`no-new-privileges`, empty capability sets, cgroup PID and memory
limits) when `PROBE_VERIFIER_REQUIRE_ISOLATION` is on (forced in live mode).

What is still a limitation:

- **Isolation is verified from inside the container, not from the host.** The
  verifier reads `/proc/self/status` (`NoNewPrivs`, `Cap*` masks) and its
  cgroup `pids.max` / `memory.max`. Docker Desktop (macOS/Windows) runs a
  Linux VM whose cgroup v2 layout usually exposes these; where it does not,
  the worker fails closed with `PROBE_INFRASTRUCTURE_BLOCKED` and the readiness
  command reports `verifier.isolation FAIL`. Set
  `PROBE_VERIFIER_REQUIRE_ISOLATION=false` only for local fake-mode
  experiments; live mode ignores that flag. Seccomp and AppArmor profiles are
  Docker's defaults and are not introspected.
- **Verifier image CVE posture.** `make audit` (Trivy, HIGH/CRITICAL,
  `--ignore-unfixed`) is clean for Python and Node packages after pinning
  npm 11.19.1 and removing `setuptools`/`wheel`, but the Debian 13 base still
  carries OS-level findings with no fixed package available (notably
  `linux-libc-dev`, a header-only package). They are tracked, not
  suppressed: rebuild the image on every base refresh and re-run `make audit`
  before a release.
- **Idempotency is process-local.** The verifier remembers request ids in
  memory only; after a restart the same id runs again (deterministically, from
  the registry). The cost is a repeated probe run, never a stale verdict.
- **Egress is documented, not enforced, by compose.** The verifier network has
  no route to PostgreSQL or the worker, but Docker's default bridge still allows
  outbound internet (needed for the anonymous `https://github.com` clone).
  Production should pin egress to GitHub with a network policy / egress proxy;
  a malicious probe can otherwise exfiltrate the repository contents it already
  has (it has no credentials to exfiltrate).
- **One probe at a time per verifier** (`VERIFIER_MAX_CONCURRENT`, default 1).
  A second request gets `409`, which the worker treats as transient and
  retries on its next claim, and after every run the verifier SIGKILLs every remaining
  process of the probe UID, so a descendant that dropped the run marker and
  `setsid`-escaped cannot outlive its probe or touch the next one's workspace.
  `init: true` (tini) reaps whatever exits as an orphan. The cost is
  throughput: probe verification is sequential per verifier instance; scale by
  running more verifier containers behind distinct `PROBE_VERIFIER_URL`s, not
  by lifting the lock.
- **The verifier runs on the stdlib asyncio loop, not uvloop.** With uvloop
  (uvicorn's default when installed) a probe that leaves any detached
  descendant keeps the child's stdio socketpair open, `Process.wait()` never
  resolves and the probe is misreported as a timeout. `remediator.verifier`
  pins `loop="asyncio"`; keep it that way.
- `RLIMIT_NPROC` is per UID, so it is only meaningful because the verifier UID
  runs nothing else. `RLIMIT_AS` is off by default (`VERIFIER_MAX_MEMORY_BYTES`)
  because Node/JVM toolchains reserve large address spaces; the container
  `mem_limit` is the effective memory bound.
- The verifier is reached over plain HTTP on an internal Docker network.
  Requests and capability reads are HMAC-signed with a ±300 s window and
  responses are checked against the request, so an on-path attacker cannot
  submit work or replay a mutated request, but the channel is not encrypted
  and responses are not signed: a network peer who can rewrite traffic could
  still alter a verdict. Production should add TLS (or mTLS) on that hop.

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

## Concurrency and capacity

- Capacity leases are per worker id and heartbeaten every loop; a worker that
  is alive but wedged keeps its lease until `CAPACITY_LEASE_GRACE_SECONDS`
  elapses without a heartbeat. A too-small grace can let two workers hold the
  same slot after a long GC pause; the default is 600 s.
- Cancelling a running remediation keeps its lease until Devin confirms
  termination (`TERMINATION_PENDING`). Under a Devin outage, cancelled jobs
  therefore continue to occupy capacity; this is deliberate (a live session
  is still spending) and visible in `active_jobs`.
- Resource keys are opaque strings declared by probe manifests; the
  remediator does not derive them from changed files, so two remediations
  that conflict on an undeclared file are not serialized.
- Waiting cases retry on `CAPACITY_WAIT_BACKOFF_SECONDS`; there is no fairness
  guarantee beyond claim order, so a starved case is possible under sustained
  saturation. `cases_waiting_for_capacity` and `capacity_denied_total` make
  that visible.

## Metrics and ACU reporting

- `/metrics` is a per-process Prometheus registry. With `WORKER_CONCURRENCY`
  loops in one process counters are shared; with several API/worker
  processes each must be scraped separately (gauges are refreshed from the
  database on scrape and therefore agree).
- `mode="live"` requires Devin, GitHub **and** Slack to be live; the mixed
  real-Slack/fake-GitHub/fake-Devin configuration is `simulated`.
- ACU figures come only from the official consumption endpoint and are read
  once after a session ends. Plans or service users without access show
  `Unavailable`; nothing is estimated, and a reporting failure never changes
  the case outcome.

## Operational

- `make readiness` is read-only by default and never creates a session or
  mutates a repository or channel. `--allow-mutations` currently adds one
  Slack test message; there is no GitHub write check because none is
  reversible without a trace.
- Operator rate limits and CSRF origin checks are per process and in memory;
  behind several API replicas the effective limit multiplies.

- Live GitHub uses a personal access token identity; PR author verification
  therefore depends on `GITHUB_PR_AUTHOR_LOGINS` matching Devin's GitHub App
  login (`devin-ai-integration[bot]` by default). A GitHub App installation
  identity for the remediator itself is Phase 5.
- Probe output and PR evidence accumulate without retention; bounded per row
  but not pruned.
- Slack updates are best-effort through the outbox; a permanently failing
  Slack channel leaves the case advancing with stale Slack state, visible on
  the dashboard and in `notification_outbox`.
