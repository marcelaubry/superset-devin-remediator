# Known limitations (after Phase 5)

## Probe execution boundary: front, runner and egress proxy

The worker holds the Devin API key, GitHub token, Slack token/signing secret,
operator token and database URL, so it **never executes repository code**:
there is no worker-side `local` probe mode, only `fake` (simulation) and
`remote`. Repository code runs in the `verifier-runner` service, which is
reached only through the `verifier` front:

| service | UID | holds | reaches |
|---|---|---|---|
| `verifier` (front, `remediator.verifier`) | 65533 | the HMAC key (Docker secret), the read-only registry | worker network, `verifier-internal` |
| `verifier-runner` (`remediator.verifier.executor`) | 65534 | the read-only registry, a disk-backed `/workspace` | `verifier-internal` only |
| `egress-proxy` (`remediator.verifier.egress_proxy`) | 65532 | nothing | `verifier-internal`, `egress` (internet) |

No service mounts `.env` or imports `remediator.config`; every one runs with a
read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, PID/memory/CPU
limits and `init: true`. The runner is on an `internal: true` network, so the
only way out is `CONNECT host:443` through the proxy, which accepts exact
lower-case hostnames from `EGRESS_ALLOWED_HOSTS` (GitHub, the npm/yarn
registries and `cdn.sheetjs.com` — Superset's lockfile resolves `xlsx` from
the SheetJS CDN — by default) and rejects wildcards, IP literals, other ports and
plain HTTP. The runner reports `direct_egress` (a bounded TCP attempt to a
non-allowlisted public host) and `execution=runner`; the worker's signed
`GET /capabilities` check refuses, in live mode, a verifier that executes
in-process, that can see any credential (including the HMAC key on the runner
side), runs as root, has a writable root, lacks a declared tool, cannot show
the compose hardening, or has unrestricted direct egress.
`python -m remediator.readiness --verifier-smoke` runs the real
`apache/superset#0` Jest smoke probe (pinned SHA, `npm ci --ignore-scripts`)
through that whole path.

What is still a limitation:

- **Isolation is verified from inside the container, not from the host.** The
  runner reads `/proc/self/status` (`NoNewPrivs`, `Cap*` masks) and its
  cgroup `pids.max` / `memory.max`. Docker Desktop (macOS/Windows) runs a
  Linux VM whose cgroup v2 layout usually exposes these; where it does not,
  the worker fails closed with `PROBE_INFRASTRUCTURE_BLOCKED` and the readiness
  command reports `verifier.isolation FAIL`. Set
  `PROBE_VERIFIER_REQUIRE_ISOLATION=false` only for local fake-mode
  experiments; live mode ignores that flag. Seccomp and AppArmor profiles are
  Docker's defaults and are not introspected.
- **Egress enforcement is the compose network topology plus the proxy.** The
  `direct_egress` self-check proves the runner cannot open a TCP connection to
  a public host at check time; it cannot prove the absence of every side
  channel (DNS is resolved by the proxy, not the runner, but Docker's embedded
  DNS still answers for service names). The proxy allowlist is by hostname:
  anything the allowlisted hosts serve (any GitHub repository, any npm
  package) is reachable, so **package `postinstall` scripts are the residual
  supply-chain risk**; approved manifests use `--ignore-scripts` and a probe
  that needs native builds must be reviewed for it.
- **The front and runner trust each other over plain HTTP on the internal
  network.** The front authenticates the worker (HMAC) and forwards a
  registry-bound request; the runner trusts the front because nothing else can
  reach `verifier-internal`. There is no second authentication on that hop and
  responses are not signed; production should add TLS (or mTLS) on both hops.
- **Verifier image CVE posture.** `make audit` (Trivy, HIGH/CRITICAL,
  `--ignore-unfixed`) is clean for Python and Node packages after pinning
  npm 11.19.1 and removing `setuptools`/`wheel`, but the Debian 13 base still
  carries OS-level findings with no fixed package available (notably
  `linux-libc-dev`, a header-only package). They are tracked, not
  suppressed: rebuild the image on every base refresh and re-run `make audit`
  before a release.
- **Idempotency is process-local.** The front remembers request ids in
  memory only; after a restart the same id runs again (deterministically, from
  the registry). The cost is a repeated probe run, never a stale verdict.
- **Exactly one probe at a time per runner.** `VERIFIER_MAX_CONCURRENT` must
  be `1` and the runner refuses to start otherwise: after every run it
  SIGKILLs every remaining process of the probe UID (so a descendant that
  dropped the run marker and `setsid`-escaped cannot outlive its probe or
  touch the next one's workspace), and that sweep is UID-wide, so a second
  concurrent run could be killed by the first one's cleanup. A second request
  gets `409`, which the worker treats as transient and retries on its next
  claim. Scale by running more runner/front pairs behind distinct
  `PROBE_VERIFIER_URL`s, not by lifting the lock.
- **The runner workspace is a Docker volume, not tmpfs.** A real
  `npm ci` for Superset's frontend needs several GB, more than a sane tmpfs;
  `/workspace` is a named volume (and the dependency cache lives under it).
  Every run's checkout is removed afterwards and cache entries are keyed by
  repository, lockfile hash and Node version, but the volume persists across
  restarts: prune it (`docker volume rm`) when rotating the image.
- **The verifier runs on the stdlib asyncio loop, not uvloop.** With uvloop
  (uvicorn's default when installed) a probe that leaves any detached
  descendant keeps the child's stdio socketpair open, `Process.wait()` never
  resolves and the probe is misreported as a timeout. `remediator.verifier`
  pins `loop="asyncio"`; keep it that way.
- `RLIMIT_NPROC` is per UID, so it is only meaningful because the runner UID
  runs nothing else. `RLIMIT_AS` is off by default (`VERIFIER_MAX_MEMORY_BYTES`)
  because Node/JVM toolchains reserve large address spaces; the container
  `mem_limit` is the effective memory bound.
- **`GET /health` on the front is unauthenticated** (Compose health check). It
  returns only `{"status","protocol_version"}`; uid, toolchain, credential and
  boundary state are served exclusively by the signed `/capabilities`. The
  runner's `/health` is detailed but only reachable from `verifier-internal`.

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
- Waiting cases are admitted FIFO by `waiting_since`: a freed slot is refused
  to any case while an older live waiter for the same global limit exists
  (`CapacityDenied.queued_behind`, label `... behind N`), and the worker
  claims waiting cases oldest first. Ordering is per global limit only; cases
  parked on a per-repository limit do not hold up other repositories, and a
  waiter that reaches a terminal state drops out of the queue. Admission still
  happens on the waiter's next retry (`CAPACITY_WAIT_BACKOFF_SECONDS`), so a
  freed slot can sit idle for up to one backoff interval.

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
  mutates a repository or channel. `--allow-mutations` adds one Slack test
  message and additionally requires `--confirm-channel <id>` matching
  `SLACK_CHANNEL_ID` (otherwise `mutations.confirmation FAIL` and nothing is
  posted); there is no GitHub write check because none is reversible without
  a trace. `--verifier-smoke` is opt-in because it takes minutes and needs
  the egress proxy; it still mutates nothing outside the runner.
- `alembic check` is clean (models and migrations agree) and enforced by
  `tests/integration/test_migrations.py`; the ORM declares the indexes and
  the `webhook_events_delivery_id_key` constraint exactly as the migrations
  created them, so no schema change was needed.
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
