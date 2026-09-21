# Superset Devin Remediator

A production-shaped issue remediation service. It accepts signed GitHub issue
webhooks, evaluates a deterministic zero-ACU rubric, persists an append-only
lifecycle in PostgreSQL, runs a bounded Devin triage session (live or fake),
validates the structured triage result, asks a human in Slack to approve or
reject remediation, dispatches the approval to GitHub as a `devin:remediate`
label, reproduces the defect with an immutable probe, runs a bounded Devin
remediation session, independently verifies the resulting PR (GitHub, probe at
head, CI), and exposes an authenticated operator dashboard. It never merges.

```text
GitHub issue opened
→ deterministic zero-ACU eligibility filter (context completeness only)
→ context-complete issue automatically queues Devin triage
→ durable create intent
→ bounded Devin session
→ schema-validated triage result (any recommendation)
→ AWAITING_REMEDIATION_APPROVAL (Slack shows Devin's recommendation, evidence,
  blocking questions and a warning when it is not remediation_candidate)
→ authorized Slack user approves or rejects
→ approval queues `devin:remediate` label + audit comment through the outbox
→ signed GitHub `issues/labeled` webhook → REMEDIATION_APPROVED
→ zero-ACU dispatch preconditions (approval ↔ exact triage hash, label on the
  exact issue, pinned base SHA, immutable probe registered, nothing active)
→ immutable probe executed at BASE (must fail as declared; else zero ACUs)
→ durable remediation create intent → bounded Devin remediation session
→ schema-validated structured output → PR discovered from `pull_requests[]`
→ PR validated against GitHub → identical probe executed at HEAD (must pass)
→ GitHub check runs for the verified head SHA → CI_PASSED
→ ready for human review (never auto-merged, issue never auto-closed)
```

Phase 5 (this release) moves probe execution out of every credential-bearing
process into an HMAC-authenticated, isolated verifier service, adds
database-backed concurrency and spend limits, authenticated low-cardinality
metrics, optional official ACU reporting, a read-only live-readiness command
and request-level hardening. Phase 4 added the remediation session and the
independent evidence chain behind it. Structured output, a PR URL, or session exit are
evidence, never authority: the case reaches `PR_VALIDATED` only after GitHub
corroborates the PR and the immutable probe passes at the exact head SHA, and
`CI_PASSED` only after GitHub's check runs for that SHA succeed. Every failed
precondition, a probe that already passes at base, and a missing probe runtime
spend zero ACUs.

## How it works

- FastAPI verifies the raw GitHub webhook body before parsing and filters
  deliveries by repository, event/action, and an optional opt-in intake label
  (off by default so every newly opened issue reaches the zero-ACU eligibility filter).
- PostgreSQL stores webhook payloads, current cases, attempts, transitions, and
  notification intents.
- A zero-ACU deterministic eligibility filter answers one question only: *is
  there enough context for bounded code-aware triage?* It rejects an issue
  (no Devin session is ever created) only when the description lacks a
  concrete problem, an expected outcome, or any investigation signal
  (reproduction, current-vs-expected example, error/log, sample I/O, affected
  component/file/endpoint, test, or reference link), and it names the missing
  elements in the transition, logs, dashboard and simulation output. It does
  **not** answer *should this work be autonomously remediated?* — dependency,
  migration, architecture and other category hints are recorded as an advisory
  recommendation and never reject a sufficiently detailed issue.
  See [docs/architecture.md](docs/architecture.md#eligibility-rubric).
- Before any `POST /sessions`, the worker commits an `attempts` row with a
  unique `operation_key`; that key is sent as an exact session tag. An
  uncertain create is reconciled by tag and is never re-sent.
- Sessions are bounded by `max_acu_limit` and an absolute deadline. On the
  deadline the worker does a final `GET`, then `DELETE`s the remote session;
  `TIMED_OUT` is recorded only once termination is confirmed.
- Devin must return `structured_output` matching a self-contained Draft 7
  schema; anything missing, malformed, or unrecognized fails the attempt.
- The dashboard shows live session status/detail, elapsed time, deadline,
  Devin URL, validated result, and blocked/reconciliation reasons.
- The fake client emits the same v3 `status`/`status_detail` vocabulary and
  deterministically picks a scenario from the issue number.
- Every schema-valid triage result — `remediation_candidate`, `needs_human`,
  `deterministic_automation`, `no_change_needed` or `invalid_issue` — creates
  one `approval_requests` row and a Slack outbox row; the recommendation is
  preserved exactly, never rewritten. The worker posts a Block Kit message
  (escaped, length-capped, no raw issue body, no secrets) showing Devin's
  recommendation, summary/evidence, acceptance criteria, blocking questions,
  affected files, estimated scope and risks, plus the warning *"Devin did not
  recommend autonomous remediation. Approval explicitly accepts this risk and
  authorizes the bounded remediation attempt."* for non-candidates, with
  `Approve remediation`,
  `Reject`, an optional rejection-reason select, `View issue`, and
  `View evidence/dashboard`. Buttons carry an opaque random token; only its
  SHA-256 is stored.
- `POST /webhooks/slack/actions` verifies the raw body (timestamp window, then
  `v0:{ts}:{body}` HMAC-SHA256 with constant-time compare) before parsing,
  then checks token, expiry, approver allowlist, case state, and dedupes on
  `(token, action_ts, user)`. It answers within the request with a short
  DB transaction; all Slack/GitHub HTTP happens in the worker's outbox
  dispatcher.
- Approval records actor/time/action id/triage hash/intended label operation
  and enqueues `apply_remediation_label`; the worker re-checks the issue,
  allowlist, and triage hash, applies the label exactly once (idempotent), and
  posts one append-only audit comment. Rejection records actor/time/reason,
  moves to `REMEDIATION_REJECTED`, and only posts a comment.
- Outbox rows retry with bounded exponential backoff; after
  `OUTBOX_MAX_ATTEMPTS` they are `FAILED` with `last_error` visible in the
  dashboard, and the label operation moves the case to
  `APPROVAL_DELIVERY_FAILED`. `POST /operator/outbox/{id}/retry` re-queues them
  without any re-approval.
- `REMEDIATION_APPROVED` is consumed by `RemediationPipeline`
  (`remediator/worker/remediation.py`). Before any `POST /sessions` it checks
  the allowlist, the recorded approval (must reference the current triage
  result's exact SHA-256), the label on the exact issue, the pinned default
  branch, that no remediation attempt is active and no verified PR exists, and
  that an approved immutable probe is registered under `probes/<owner>/<repo>/<n>/`.
  The manifest, script content, hashes and registry commit are frozen into a
  `probe_snapshots` row; the probe then runs at the pinned base SHA. Only the
  declared failing exit code permits a create intent; a passing base is
  `no_change_needed` (human-blocked), a missing runtime is
  `PROBE_INFRASTRUCTURE_BLOCKED`, and neither creates an attempt.
- The remediation session reuses the Phase 2 runner: durable attempt +
  operation key `op:<case>:REMEDIATION:<triage-hash-prefix>:<base-sha>:<n>`
  sent as the exact session tag, reconcile-by-tag on uncertain create,
  `max_acu_limit=DEVIN_REMEDIATION_MAX_ACU`, absolute deadline, final GET then
  `DELETE`, `REMEDIATION_TERMINATION_PENDING` until termination is confirmed.
  The prompt (`prompts/remediation_v1.md`) carries only the allowlisted repo,
  base SHA, fenced issue text, validated triage, approved acceptance criteria,
  the probe identifier/hash/invocation, scope, and approval metadata.
- Output must match `remediation.v1` (`outcome`, `base_sha`, `head_sha`,
  `branch`, `pr_url`, `issue_reference`, `changed_files`, `commits`,
  `tests_run`, `probe_identifier`, `probe_hash`, `risks`, `blocking_questions`).
  Candidate PRs come only from Devin's `pull_requests[]`; chat is never scraped.
  GitHub must agree: open/draft and unmerged in the allowlisted repo, base is
  the default branch, head uses `DEVIN_REMEDIATION_BRANCH_PREFIX`, head SHA
  equals the structured output, `compare` shows the branch is ahead of the
  pinned base, the closing reference resolves to exactly this issue (timeline
  cross-reference first, exact `owner/repo#N` parse as fallback, `#1` ≠ `#10`),
  the author is in `GITHUB_PR_AUTHOR_LOGINS`, and no changed file is under
  `probes/`, `.github/`, or repository settings. Scope expansion is
  human-blocked; any contradiction fails the attempt.
- The identical probe snapshot (same hash) runs at the PR head SHA and must
  return the declared passing code; a failing head is `REMEDIATION_FAILED` with
  no automatic fix chain. Every run persists command identity, hashes, commit,
  exit code, duration and bounded stdout/stderr in `probe_executions`.
- CI is read from GitHub check runs for the exact head SHA with bounded polling
  (`CI_POLL_INTERVAL_SECONDS`, `CI_TIMEOUT_SECONDS`) into `ci_snapshots`;
  pending/passed/failed/cancelled/skipped/timed-out are distinguished, absent
  checks never pass, `GITHUB_REQUIRED_CHECKS` names the required ones.
  `CI_PASSED` still requires human PR review; nothing merges or closes.
- Repository code executes in exactly one place: the **verifier runner**
  (`verifier-runner` service, `remediator.verifier.executor`), behind a
  key-holding front and an exact-host egress proxy.

  ```text
  API / worker (Devin, GitHub, Slack, operator, DB credentials)
      │  HMAC-SHA256 signed verifier.v2 request:
      │  repository, exact 40-hex SHA, probe id, script + manifest hashes
      │  (never the script, never a URL, never a credential)
      ▼
  verifier (front, UID 65533; only secret: the HMAC key; runs no repo code)
      │  signature + allowlist + registry hash check, forwarded over an
      │  internal-only network
      ▼
  verifier-runner (UID 65534; NO secret at all, not even the HMAC key)
      │  anonymous fetch of the exact SHA → manifest setup argv (npm ci …)
      │  → probe.sh, no shell; internet only via
      ▼                    egress-proxy: CONNECT :443 to github.com,
  bounded evidence          registry.npmjs.org, … (exact hosts, no IPs)
  ```

  No verifier service has `.env`, a Docker socket, a provider or database
  credential, or an import of `remediator.config`; each runs as its own
  non-root UID on a read-only root filesystem with `cap_drop: ALL`,
  `no-new-privileges`, PID/memory/CPU limits. The runner's only network is
  `internal: true`, its workspace is a disk-backed volume wiped after every
  run, and the image ships pinned Git, Bash, Python 3, Node 24, npm and Yarn
  for the Superset frontend probes. The worker only ever uses
  `PROBE_RUNNER_MODE=fake` (simulation) or `remote`; before each probe it
  reads the signed `/capabilities` and `execution != runner`, any visible
  credential, UID 0, writable root, missing tool, possible direct egress or
  (in live mode) an unproven isolation property makes the run an
  *infrastructure* failure, never a verdict. Requests carry a request id and
  are idempotent per front process. Cache keys include the lockfile hash,
  runtime versions and repository identity, and cache contents can never
  decide a verdict. `make readiness-smoke` runs the real pinned Superset Jest
  probe (`probes/apache/superset/0`) through the whole path. See
  [docs/probes.md](docs/probes.md), [docs/threat-model.md](docs/threat-model.md)
  and [docs/known-limitations.md](docs/known-limitations.md).
- **Concurrency and spend:** `MAX_CONCURRENT_TRIAGE`,
  `MAX_CONCURRENT_REMEDIATION`, `MAX_CONCURRENT_PROBES`, a per-repository
  remediation limit, one active remediation per case and manifest
  `resource_keys` are PostgreSQL leases shared by every worker. A case that
  finds a limit full is parked at zero ACUs and admitted FIFO; leases heartbeat,
  expire and reconcile after crashes; cancel keeps the slot until Devin
  confirms termination. See [docs/concurrency.md](docs/concurrency.md).
- **Metrics:** `GET /metrics` (operator token) exposes low-cardinality
  Prometheus series labelled `mode="live"` or `mode="simulated"`; session
  completion, accepted output, PR discovered, PR validated, probe passed and
  CI passed are separate milestones. See [docs/metrics.md](docs/metrics.md).
- **ACU reporting:** optional (`DEVIN_ACU_REPORTING_ENABLED`) read of the
  official consumption endpoint; 401/403/404, unsupported plans or transport
  errors show `Unavailable`, never an estimate, and never fail a remediation.
- **Readiness:** `make readiness` runs a read-only redacted pass/fail report
  (config placeholders, database/migrations, Devin identity and session-list
  permission, GitHub identity/default branch/labels/permissions, Slack
  identity/channel/approvers, verifier key separation/isolation/egress and
  Node capability, webhook base URL, allowlists, limits).
  `make readiness-smoke` adds the real Superset probe run;
  `make readiness-mutating CONFIRM_CHANNEL=<SLACK_CHANNEL_ID>` additionally
  posts a Slack test message. See [docs/readiness.md](docs/readiness.md).
- **Controlled live canary:** `LIVE_CANARY=true` is a fail-closed envelope
  for the first human-authorized live run (one repository, opt-in intake
  label, every concurrency limit at 1, remote verifier, secure cookies, no fake
  evidence next to a live session). Activation order, emergency stop and the
  evidence checklist are in [docs/canary-runbook.md](docs/canary-runbook.md).
- **Emergency canary probe override:** `LIVE_CANARY_ALLOW_MISSING_PROBE=true`
  (default `false`, only valid inside the canary envelope with
  `MAX_CONCURRENT_REMEDIATION=1`) lets the human Slack approval start one
  bounded remediation session when no immutable probe is registered. It skips
  only probe registration and the BASE/HEAD probe runs; PR validation and
  exact-head CI still gate the case, no probe evidence is recorded, and the
  case, dashboard, Slack updates, metrics and the append-only
  `CANARY_PROBE_OVERRIDE` audit row all disclose that behavioural correctness
  was not verified.
- **Request hardening:** body-size limit (`413`), Origin/CSRF checks and a
  rate limit on operator mutations, security headers, SSRF validation of
  provider/verifier URLs, secret redaction in nested errors, allowlisted-host
  URL rendering in Slack and the dashboard. `make audit` runs pip-audit, Trivy
  and gitleaks.
- Slack updates for every milestone (queued/running, session link, PR found,
  probe base/head, CI state, failure/blocked reason, "Ready for human review")
  go through the same outbox and never change remediation state.

## Run

```bash
cp .env.example .env
docker compose up --build
```

Open <http://localhost:8000> and sign in with `OPERATOR_TOKEN`.

### Host compatibility

Every Compose image builds natively for `linux/amd64` and `linux/arm64`
(Apple Silicon, Graviton). The verifier image picks the pinned Node tarball
from BuildKit's `TARGETARCH` (`amd64` → Node `x64`, `arm64` → Node `arm64`),
verifies it against the SHA-256 digests pinned in `docker/verifier/Dockerfile`,
and fails the build for any other architecture rather than producing an image
whose `node` cannot exec. Never set `DOCKER_DEFAULT_PLATFORM=linux/amd64` or a
`platform:` on the stack: the emulated verifier would run Superset's Jest suite
under Rosetta/QEMU. `tests/test_phase6_canary.py` guards the Dockerfiles against
reintroducing a single-architecture download. The `linux/arm64` image has so far
been built and exercised under QEMU emulation on an x86_64 host; run
`docker compose up --build -d` and `make readiness-smoke` once on a native arm64
host (Apple Silicon Docker Desktop, Graviton) before relying on it there.
Cross-building for review:

```bash
docker buildx build --platform linux/arm64 -f docker/verifier/Dockerfile .
docker buildx build --platform linux/amd64 -f docker/verifier/Dockerfile .
```

## Scenarios

| Scenario | Fixture | Issue | Expected outcome | Why |
| --- | --- | ---: | --- | --- |
| `good` | `issue_good_candidate.json` | 4213 | `AWAITING_REMEDIATION_APPROVAL` | Complete reproduction, objective checks, acceptance criteria, and existing-pattern evidence |
| `needs-scoping` | `issue_needs_scoping.json` | 4321 | `POLICY_REJECTED` without Devin session | Body is a one-liner: transition names `Missing concrete problem statement`, `Missing expected outcome`, `Missing reproduction/example/affected-component signal` |
| `deterministic` | `issue_deterministic.json` | 4422 | `AWAITING_REMEDIATION_APPROVAL` | Context-complete dependency bump (package, version, reason, validation); advisory `USE_DETERMINISTIC_AUTOMATION` is recorded but does not gate; the fake triage returns `remediation_candidate` |
| `human-led` | `issue_human_led.json` | 4502 | `AWAITING_REMEDIATION_APPROVAL` | Context-complete architecture issue; advisory `HUMAN_LED` is recorded but does not gate; the fake triage returns `remediation_candidate` |
| `digit-only-temporal` | `issue_digit_only_temporal.json` | 4171 | `AWAITING_REMEDIATION_APPROVAL` | Focused bug with reproduction, current/expected values and acceptance criteria |
| `triage-infeasible` | `issue_triage_infeasible.json` | 4533 | `AWAITING_REMEDIATION_APPROVAL` with warning | Fake triage returns `needs_human`; Slack shows the recommendation, blocking questions and the non-candidate warning; a human decides |
| `failure` | `issue_fake_failure.json` | 4515 | `FAILED` | Fake session ends `status=error` for issue numbers divisible by five |
| `blocked` | `issue_human_blocked.json` | 4529 | `HUMAN_BLOCKED` | Fake session reports `waiting_for_user`; session URL retained, no replacement |
| `wrong-repo` | `issue_wrong_repo.json` | 4601 | filtered out | Repository does not match `GITHUB_REPOSITORY` |
| `missing-label` | `issue_missing_label.json` | 4601 | filtered out only when `GITHUB_REQUIRED_LABEL` is set | Unlabeled issue; evaluated by default (no intake label required) |
| `malformed-output` | `issue_malformed_output.json` | 4611 | `FAILED` | Session finishes but `structured_output` fails schema validation |
| `uncertain-create` | `issue_uncertain_create.json` | 4622 | same as `good`, via `RECONCILING_CREATE` (`create_state=RECONCILED`) | Create POST fails at transport level after the session exists; reconciled by exact tag, no second POST |
| `quota-failure` | `issue_quota_failure.json` | 4633 | `FAILED` | `suspended` / `out_of_credits` is a terminal failure |
| `timeout` | `issue_timeout.json` | 4644 | `TIMED_OUT` | Session never finishes; final GET, remote DELETE, then timed out |
| `missing-output` | `issue_missing_output.json` | 4655 | `FAILED` | Session finishes without structured output |
| `unknown-status` | `issue_unknown_status.json` | 4666 | `TIMED_OUT` | Undocumented status enters reconciliation and stays bounded by the deadline |
| `create-rejected` | `issue_create_rejected.json` | 4677 | `FAILED` | Definitive API error on create, recorded as `create_state=API_ERROR` |
| `idle-with-output` | `issue_idle_with_output.json` | 4811 | `AWAITING_REMEDIATION_APPROVAL` | Session reports `waiting_for_user` ("awaiting instructions") *after* submitting valid `structured_output`; the output wins, one approval + one Slack card, never `HUMAN_BLOCKED` |
| `idle-malformed-output` | `issue_idle_malformed_output.json` | 4822 | `FAILED` | Same idle status with schema-invalid output: fails like `malformed-output`, not `HUMAN_BLOCKED` |

Phase 3 scenarios drive the same fake adapters through the production
endpoints (`/webhooks/github`, `/webhooks/slack/actions`, operator API). The
Phase-1 `SIMULATION_AUTO_APPROVE_REMEDIATION` shortcut was removed: nothing in
this release enters `REMEDIATION_CREATE_INTENT`.

| Scenario | Issue | What it proves |
| --- | ---: | --- |
| `approve` | 4213 | Slack post → signed approve click → worker applies label (state still waiting) → signed `labeled` webhook → `REMEDIATION_APPROVED`; no Devin remediation attempt exists |
| `reject` | 4219 | Signed reject click with a reason → `REMEDIATION_REJECTED`, GitHub comment queued, no label operation |
| `slack-negative` | 4217 | Bad signature, stale timestamp, missing headers (401); outsider and unknown token acknowledged with 200 `ok: false` (Slack shows the reason via `response_url`); then duplicate/late clicks are no-ops and exactly one label op is queued |
| `expired-token` | 4216 | Operator expires the token; the click is acknowledged with 200 `outcome: expired_token` and the case is untouched |
| `slack-delivery-failure` | 4699 | Fake Slack fails all attempts; triage result survives, outbox shows `FAILED`, authenticated retry delivers |
| `github-label-failure` | 4688 | Fake GitHub fails all attempts; approval retained, case `APPROVAL_DELIVERY_FAILED`, retry applies the label, webhook confirms |

Run the scenarios through the real endpoints (the simulator signs requests
with the configured secrets and never prints them):

```bash
uv run python scripts/simulate.py --scenario all --wait      # Phase 1/2 + Phase 3
uv run python scripts/simulate.py --scenario phase3 --wait
uv run python scripts/simulate.py --scenario approve --wait
uv run python scripts/simulate.py --scenario good --bad-signature
```

Phase 3 scenarios each decide their fixture issue exactly once, like production;
re-running them against a database that already holds those decisions fails
fast with a message to reset (`docker compose down -v && docker compose up -d`).
Because Phase 3 fixtures register no probe, `approve` and
`github-label-failure` now end in `REMEDIATION_HUMAN_BLOCKED` (approved probe
unavailable) with zero ACUs spent, which the simulator asserts.

Phase 4 scenarios continue through the whole fake remediation chain (Slack
approve → label webhook → probe at base → fake session → PR → probe at head →
CI). Fixture probes live under `probes/apache/superset/<issue>/`; the fake
probe runner and fake GitHub/Devin adapters pick their behaviour from the issue
number (`remediator/fixtures.py`).

| Scenario | Issue | What it proves |
| --- | ---: | --- |
| `remediate` | 4702 | Probe fails at base → one session → PR from `pull_requests[]` corroborated by GitHub → probe passes at head → check runs succeed → `CI_PASSED`, "Ready for human review"; nothing merged |
| `remediate-base-passes` | 4744 | Probe already passes at base → `no_change_needed`, human-blocked, no attempt, zero ACUs |
| `remediate-probe-infra` | 4748 | Runner reports a missing tool → `PROBE_INFRASTRUCTURE_BLOCKED`, zero ACUs; operator `retry-probe` re-runs base |
| `remediate-head-fails` | 4747 | PR verified but probe still fails at head → `REMEDIATION_FAILED`, no automatic fix chain |
| `remediate-forbidden-files` | 4738 | PR touches `.github/workflows` → verification fails closed |
| `remediate-ci-failed` | 4751 | Required check fails for the verified head → `CI_FAILED`; `retry-ci` re-reads the same head, no new session |
| `remediate-uncertain-create` | 4706 | Create POST outcome unknown → tag reconciliation attaches the single session; one paid session |
| `remediate-unlabeled-intake` | 4222 | Unlabeled `issues/opened` is evaluated; an unrelated label and a premature `devin:remediate` never remediate |

```bash
uv run python scripts/simulate.py --scenario phase4 --wait
uv run python scripts/simulate.py --scenario remediate --wait
```

See [docs/simulation.md](docs/simulation.md) for the full walkthrough and the
unit/integration scenarios that cover contradictory PR URLs/SHAs, `#1` vs `#10`
linkage, wrong repository/base branch, malformed output, session timeout with
failed termination, concurrent retries, and worker restart in every state.

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | local `remediator` database | Async SQLAlchemy connection |
| `GITHUB_WEBHOOK_SECRET` | `change-me` | HMAC secret |
| `OPERATOR_TOKEN` | `change-me` | Dashboard/API token |
| `GITHUB_REPOSITORY` | `apache/superset` | Accepted repository |
| `GITHUB_ALLOWED_EVENTS` | `issues` | Comma-separated event allowlist |
| `GITHUB_ALLOWED_ACTIONS` | `opened,labeled` | Comma-separated action allowlist |
| `GITHUB_REQUIRED_LABEL` | *(empty)* | Optional opt-in intake label; empty (default) evaluates every opened issue. Readiness fails without one once Devin is live; `LIVE_CANARY` requires it |
| `GITHUB_BASE_REF` | `master` | Branch whose current tip is resolved and pinned as the base SHA of every live session; no SHA is ever configured |
| `GITHUB_BASE_SHA_REFERENCE` | *(empty)* | Readiness-only: last human-verified base SHA; `make readiness` warns when the live tip differs. Never used by the pipeline |
| `LIVE_CANARY` | `false` | Opt-in fail-closed envelope for the first live run: one allowlisted repository, intake label set and distinct from the remediation label, every `MAX_CONCURRENT_*` = 1, remote verifier, secure cookies with live providers, GitHub + Slack live whenever Devin is (see `docs/canary-runbook.md`) |
| `LIVE_CANARY_ALLOW_MISSING_PROBE` | `false` | Emergency canary-only: accept a missing probe registration and skip the BASE/HEAD probe runs for an already Slack-approved case. Refuses to start unless `LIVE_CANARY=true`, `GITHUB_REQUIRED_LABEL` is set and `MAX_CONCURRENT_REMEDIATION=1`; every other gate, PR validation and exact-head CI still apply and the result is disclosed as behaviourally unverified |
| `WORKER_POLL_INTERVAL_SECONDS` | `1.0` | Worker idle poll interval |
| `WORKER_CONCURRENCY` | `2` | Concurrent worker loops. Safe at 2+: case processor and outbox dispatcher share one lock order (case → approval request → outbox row, all `FOR NO KEY UPDATE`), and a deadlock/serialization failure releases the lease for retry instead of failing the case |
| `WORKER_LEASE_SECONDS` | `300` | Case and webhook ownership lease |
| `WORKER_SHUTDOWN_TIMEOUT_SECONDS` | `30` | Maximum in-flight drain time |
| `EVENT_MAX_ATTEMPTS` | `3` | Event reclaim limit |
| `DEVIN_CLIENT_MODE` | `fake` | `fake` or `live` |
| `DEVIN_API_KEY` | unset | Devin service-user API key; required in live mode, never logged or persisted |
| `DEVIN_ORG_ID` | unset | Devin organization id; required in live mode |
| `DEVIN_API_BASE_URL` | `https://api.devin.ai/v3` | Devin v3 API base; must be HTTPS in live mode |
| `DEVIN_TRIAGE_MAX_ACU` | `5` | `max_acu_limit` sent on every triage session |
| `DEVIN_TRIAGE_TIMEOUT_SECONDS` | `1800` | Absolute deadline (anchored after create) after which the session is terminated remotely; live mode enforces `>= 300` |
| `DEVIN_POLL_INTERVAL_SECONDS` | `15` | Poll interval; live mode enforces `>= 10` (10–30 recommended) |
| `DEVIN_HTTP_TIMEOUT_SECONDS` | `30` | Per-request HTTP timeout |
| `DEVIN_HTTP_MAX_RETRIES` | `3` | Bounded retries with backoff and jitter for GET/list only |
| `DEVIN_REPOS_FORMAT` | `{repository}` | Template for the single `repos[]` entry on `POST /v3/organizations/{org}/sessions`: the `owner/repo` repository path. The create schema types `repos` as `array[string]` without stating the format; `owner/repo` is inferred from the other v3 repository surfaces (repository listing `repo_path`, the `repo_names` session filter) and confirmed per deployment by the readiness check `devin.repository_access`. Must contain `{repository}` exactly once; the URL form stays configurable. Readiness reports the value and, with live Devin, checks the organization's repository listing for the allowlisted path |
| `GITHUB_BASE_REF` | `master` | Ref resolved to the exact base SHA pinned in each session |
| `RECONCILE_MAX_ATTEMPTS` | `3` | Bounded list-by-tag lookups after an uncertain create |
| `MAX_ATTEMPTS_PER_KIND` | `3` | Per-case triage/remediation spend cap |
| `DEVIN_REMEDIATION_MAX_ACU` | `15` | `max_acu_limit` sent on every remediation session |
| `DEVIN_REMEDIATION_TIMEOUT_SECONDS` | `5400` | Absolute remediation deadline; final GET then `DELETE`; live mode enforces `>= 600` |
| `DEVIN_REMEDIATION_BRANCH_PREFIX` | `devin/` | Required prefix of the PR head branch |
| `PROBE_RUNNER_MODE` | `fake` | `fake` (deterministic by issue number) or `remote` (credential-free verifier container); the worker has no `local` mode |
| `PROBE_VERIFIER_URL` | *(unset)* | Verifier base URL, required with `remote`; the worker refuses a verifier whose `/capabilities` shows credentials, UID 0, a writable root or a missing tool. One probe runs at a time per verifier (`409` while busy → retried) |
| `PROBE_VERIFIER_SHARED_SECRET` | unset | HMAC key (>= 32 chars) shared only with the verifier; required with `remote`. Mounted into the verifier as a Docker secret from `docker/secrets/verifier_hmac_key` |
| `PROBE_VERIFIER_REQUEST_TIMEOUT_SECONDS` | `30` | HTTP timeout for capability checks (probe requests use the probe timeout) |
| `PROBE_VERIFIER_REQUIRE_ISOLATION` | `true` | Refuse a verifier that cannot prove `no-new-privileges`, empty capability sets and cgroup limits; forced on in live mode |
| `PROBE_SMOKE_PROBE` | `apache/superset#0` | Reserved registry slot (issue 0 is never a case) run by `readiness --verifier-smoke` |
| `MAX_CONCURRENT_TRIAGE` / `MAX_CONCURRENT_REMEDIATION` / `MAX_CONCURRENT_PROBES` | `2` / `1` / `1` | Global lease limits shared by all workers |
| `MAX_CONCURRENT_REMEDIATION_PER_REPOSITORY` | `1` | Remediation leases per repository |
| `CAPACITY_WAIT_BACKOFF_SECONDS` | `5` | Re-check delay for a case parked on a full limit |
| `CAPACITY_LEASE_GRACE_SECONDS` | `600` | Lease expiry without heartbeat (crashed worker) |
| `HUMAN_BLOCKED_RECONCILE_INTERVAL_SECONDS` | `300` | How often the worker re-reads (GET) the retained session of a `HUMAN_BLOCKED` triage case for late `structured_output` |
| `MAX_REQUEST_BODY_BYTES` | `1048576` | Requests larger than this get `413` before parsing |
| `OPERATOR_RATE_LIMIT_PER_MINUTE` | `60` | Per-client limit on operator mutations |
| `OPERATOR_CSRF_TRUSTED_ORIGINS` | *(empty)* | Extra Origins allowed for cookie-authenticated mutations |
| `PUBLIC_BASE_URL` | *(empty)* | Externally reachable API base (webhook/tunnel), checked by readiness |
| `DEVIN_ACU_REPORTING_ENABLED` | `false` | Read official per-session ACU consumption; `Unavailable` on any failure |
| `PROBE_ROOT` | `probes` | Immutable probe registry root; must exist in live mode |
| `PROBE_TIMEOUT_SECONDS` | `900` | Upper bound for one probe run (manifest may be shorter) |
| `PROBE_MAX_OUTPUT_BYTES` | `65536` | Per-stream stdout/stderr capture cap |
| `VERIFIER_*` | see `docker-compose.yml` | Verifier-container-only knobs (`VERIFIER_CLONE_URL_FORMAT`, `VERIFIER_MAX_TIMEOUT_SECONDS`, `VERIFIER_MAX_PROCESSES`, `VERIFIER_MAX_FILE_SIZE_BYTES`, `VERIFIER_MAX_MEMORY_BYTES`); read from plain environment, never from `.env` |
| `CI_POLL_INTERVAL_SECONDS` | `60` | Check-run poll interval; live mode enforces `>= 30` |
| `CI_TIMEOUT_SECONDS` | `14400` | After this `CI_PENDING` becomes `CI_FAILED` (`timed_out`) |
| `GITHUB_PR_AUTHOR_LOGINS` | `devin-ai-integration[bot]` | Accepted PR author identities |
| `GITHUB_REQUIRED_CHECKS` | *(empty)* | Required check-run names; empty = every reported check must pass and at least one must exist |
| `REMEDIATION_MAX_CHANGED_FILES` | `25` | Larger PRs are human-blocked as scope expansion |
| `GITHUB_CLIENT_MODE` | `fake` | `fake` or `live` GitHub issues/labels/comments client |
| `GITHUB_TOKEN` | unset | GitHub token (also used to resolve the base SHA); required in live mode, never logged or persisted |
| `GITHUB_API_BASE_URL` | `https://api.github.com` | Must be HTTPS in live mode |
| `GITHUB_REMEDIATION_LABEL` | `devin:remediate` | Label applied on approval and awaited from the webhook |
| `GITHUB_FAKE_FAIL_LABELS` | `false` | Fake-only: fail every label call (fixture #4688 fails regardless) |
| `SLACK_CLIENT_MODE` | `fake` | `fake` (messages stored in Postgres) or `live` |
| `SLACK_BOT_TOKEN` | unset | Bot token for `chat.postMessage`/`chat.update`; required in live mode |
| `SLACK_SIGNING_SECRET` | `change-me-slack-signing` | Verifies `X-Slack-Signature`; placeholder refused in live mode |
| `SLACK_CHANNEL_ID` | `C0000000000` | Approval channel |
| `SLACK_APPROVER_USER_IDS` | `U0000000001` | Comma-separated Slack user ids allowed to approve/reject |
| `SLACK_MAX_TIMESTAMP_SKEW_SECONDS` | `300` | Replay window for `X-Slack-Request-Timestamp` |
| `SLACK_ACTION_TOKEN_TTL_SECONDS` | `604800` | Lifetime of the opaque action token |
| `SLACK_API_BASE_URL` | `https://slack.com/api` | Must be HTTPS in live mode |
| `SLACK_FAKE_FAIL_POSTS` | `false` | Fake-only: fail every post (fixture #4699 fails regardless) |
| `DASHBOARD_BASE_URL` | `http://localhost:8000` | Base for the dashboard links placed in Slack/GitHub |
| `OUTBOX_MAX_ATTEMPTS` | `5` | Bounded outbox retries before terminal `FAILED` |
| `OUTBOX_BASE_BACKOFF_SECONDS` / `OUTBOX_MAX_BACKOFF_SECONDS` | `2` / `300` | Exponential backoff bounds |
| `OUTBOX_LEASE_SECONDS` | `120` | Outbox row claim lease |
| `LOG_LEVEL` | `INFO` | Application log level |
| `COOKIE_SECURE` | `false` | Set `true` when dashboard traffic is behind TLS |

`.env.example` uses very short fake-mode timeout and poll values so the
simulation finishes in seconds and placeholder `change-me` secrets. Live mode
fails closed on startup when the API key or org id is missing, the base URL is
not HTTPS, the poll interval is under 10 seconds, the triage timeout is under
300 seconds, or `GITHUB_WEBHOOK_SECRET`/`OPERATOR_TOKEN` are `change-me` or
shorter than 16 characters. `SLACK_CLIENT_MODE=live` additionally requires a
non-placeholder `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID`,
at least one approver, and an HTTPS API URL; `GITHUB_CLIENT_MODE=live`
requires `GITHUB_TOKEN` and an HTTPS API URL. `DEVIN_CLIENT_MODE=live`
additionally requires `GITHUB_CLIENT_MODE=live`, `PROBE_RUNNER_MODE=remote`
with a `PROBE_VERIFIER_URL`, an existing `PROBE_ROOT`, a remediation timeout
`>= 600`, and a CI poll interval `>= 30`, so a real session can never be
verified by fake evidence or by a probe running next to the credentials. The
setting alone is not trusted: the worker re-checks the verifier's
`/capabilities` before every single probe. Run `make readiness` before
switching any mode to live; it never creates a session or mutates a repository.

A live session is never left without a local owner: worker errors, operator
cancel (including mid-poll), and retries all go through `TERMINATION_PENDING`
and a remote `DELETE` before the case becomes terminal or a new session is
created. Retrying a case whose create outcome is unresolved requires
`POST /operator/cases/{id}/retry?confirm_no_session=true`.

### Live mode

```bash
DEVIN_CLIENT_MODE=live
DEVIN_API_KEY=...            # service-user key
DEVIN_ORG_ID=...
DEVIN_TRIAGE_MAX_ACU=5
DEVIN_TRIAGE_TIMEOUT_SECONDS=1800
DEVIN_POLL_INTERVAL_SECONDS=15
```

Each session is created with exactly one allowlisted repository at the exact
base SHA, `max_acu_limit`, the operation key as an exact tag plus
`repo:`/`issue:`/`kind:`/`case:`/`attempt:` correlation tags,
`structured_output_required=true`, and the versioned prompt in
`remediator/devin/prompts/triage_v1.md`. Issue content is fenced as untrusted
data inside the prompt. No automated test calls the real API.

The worker's Docker `stop_grace_period` must exceed
`WORKER_SHUTDOWN_TIMEOUT_SECONDS` so in-flight jobs can drain before SIGKILL.

### Slack setup (live)

1. Create a Slack app; add bot scopes `chat:write` (and `chat:write.public`
   if the bot is not invited to the channel) plus `channels:history` for a
   public approval channel or `groups:history` for a private one. The history
   scope is what lets the worker recover from a crash between `chat.postMessage`
   and its database commit: it looks the message up by metadata through
   `conversations.history` instead of posting a duplicate. Without it that
   recovery fails closed (`missing_scope`): the outbox row is marked `FAILED`
   with the reason, the request shows notification `FAILED`, nothing is
   reposted, and an operator retry reconciles once the scope is granted.
   Install the app and copy the **Bot User OAuth Token** into `SLACK_BOT_TOKEN`.
2. Copy **Basic Information → Signing Secret** into `SLACK_SIGNING_SECRET`.
3. Enable **Interactivity & Shortcuts** and set the request URL to
   `https://<host>/webhooks/slack/actions`. Slack signs each request with
   `X-Slack-Signature` / `X-Slack-Request-Timestamp` over
   `v0:{timestamp}:{raw body}`; the endpoint verifies before parsing and
   rejects requests outside `SLACK_MAX_TIMESTAMP_SKEW_SECONDS`.
4. Set `SLACK_CHANNEL_ID` to the approval channel and
   `SLACK_APPROVER_USER_IDS` to the member ids allowed to decide. Anyone else
   clicking gets an ephemeral "not an authorized approver" reply and an audit
   event; the case is unchanged.
5. The endpoint returns a JSON body Slack ignores for `block_actions`; the
   message itself is updated asynchronously through `chat.update` by the
   worker (awaiting → approved/dispatch pending → label applied / rejected /
   delivery failed / expired), and the decision buttons are removed after a
   terminal decision.

### GitHub setup (live)

For this take-home a **fine-grained personal access token** scoped to the
Superset fork only (`GITHUB_TOKEN`) with these repository permissions is
sufficient:

| Permission | Access | Used for |
|---|---|---|
| Issues | read/write | audit comment, `devin:remediate` label, issue read-back |
| Metadata | read | required by GitHub for any fine-grained token |
| Pull requests | read | PR validation (`GET /pulls/{n}`, `/files`), timeline cross-references |
| Contents | read | `GET /commits/{ref}` (base SHA pinning) and `compare/{base}...{head}` |
| Checks | read | check runs for the exact head SHA (`CI_PENDING`/`CI_PASSED`/`CI_FAILED`) |

No `Contents: write`, `Workflows`, `Administration` or merge permission is
needed: the remediator never pushes, merges or closes anything (Devin's own
GitHub integration opens the PR). For production prefer a **GitHub App** with
short-lived installation tokens: the App identity shows up in the audit
comment, tokens expire hourly, and permissions are granted per installation
rather than per user. The client refuses any repository outside
`GITHUB_REPOSITORY` before making a request.

Add `labeled` to `GITHUB_ALLOWED_ACTIONS` (default already includes it) so the
`issues/labeled` webhook for `GITHUB_REMEDIATION_LABEL` can confirm delivery.
The label is created in the repository by the client if missing.

## Development and test database

```bash
make fmt lint typecheck
docker compose up -d db
cp .env.example .env
make test
```

The Postgres init script creates both `remediator` and `remediator_test`.
The test fixture automatically runs Alembic against `TEST_DATABASE_URL`,
defaulting to
`postgresql+asyncpg://remediator:remediator@localhost:5432/remediator_test`.
They require the local test database and skip clearly if it is unavailable.

No automated test calls Slack, GitHub, or Devin. `tests/test_phase3_units.py`
covers the Slack signature algorithm against a reference HMAC, Block Kit
escaping/limits, live-mode fail-closed validation, GitHub allowlisting and
idempotent labels, and a secret-hygiene scan of tracked files, fixtures, and
images. `tests/integration/test_phase3_approval.py` runs the full approval,
rejection, replay, duplicate, failure/retry, webhook-gating, and restart
scenarios against Postgres with the fake adapters.

## Project layout

```text
remediator/
  api/             FastAPI app, webhook, dashboard, auth, operator routes
  devin/           DevinClient protocol, live (httpx) + fake clients, status
                   mapping, Draft 7 triage + remediation schemas, versioned
                   prompts (triage_v1, remediation_v1), tags, pull_requests[]
  probes/          Immutable probe registry loader/validator, ProbeRunner
                   protocol, fake + remote (signed verifier request) runners
  verifier/        Credential-free verifier service: verifier.v2 protocol,
                   capabilities, isolated clone/setup/probe execution
  capacity.py      PostgreSQL capacity leases (concurrency + resource keys)
  metrics.py       Low-cardinality Prometheus metrics
  readiness.py     Read-only live-readiness command
  safe_urls.py     Allowlisted URL rendering and SSRF validators
  fixtures.py      Deterministic fake scenario selection by issue number
  github_refs.py   Base commit SHA resolution (GitHub API or fake)
  github/          Webhook signature verification; fake + live issues client
  slack/           Slack request signing, Block Kit builder, fake + live client
  approvals.py     Approval request creation, Slack action processing, label
                   webhook confirmation
  templates/       Jinja2 pages and HTMX partials
  static/          CSS and vendored HTMX
  worker/          Claims, lifecycle processing, DevinRunner, outbox dispatcher,
                   RemediationPipeline (preconditions, probe base, session,
                   output/PR validation, probe head, CI tracking)
  config.py        Environment settings
  db.py            Async engine/session helpers
  lifecycle.py     States, transitions, and transition audit writes
  models.py        SQLAlchemy models
  rubric.py        Pure deterministic issue evaluation
alembic/            0001 schema, 0002 Phase 2 attempts, 0003 Phase 3 approvals/outbox,
                    0004 Phase 4 remediation states, probe snapshots/executions,
                    PR evidence, CI snapshots, 0005 attempt ordinal,
                    0006 Phase 5 capacity leases
fixtures/github/    Simulation webhook payloads
probes/             Immutable probe registry (probe.yaml + probe.sh per issue)
scripts/            Endpoint-only simulator, register_probe.py
tests/              Unit and Postgres integration tests
docs/architecture.md
docs/threat-model.md
docs/probes.md           Probe authoring guide
docs/simulation.md       Simulation guide
docs/concurrency.md      Capacity leases and queueing behaviour
docs/metrics.md          Metric definitions
docs/readiness.md        Live-readiness checks and minimal permissions
docs/canary-runbook.md   Controlled live canary: activation order, emergency stop, checklist
docs/known-limitations.md
```

## Operator actions

All require the operator bearer token or signed-in cookie and are exposed in
the case detail page.

| Action | Route | Allowed from | Effect |
| --- | --- | --- | --- |
| Cancel | `POST /operator/cases/{id}/cancel` | any active state | Routes a live session through `REMEDIATION_TERMINATION_PENDING` → `DELETE` → `REMEDIATION_CANCELLED` |
| Retry remediation | `POST /operator/cases/{id}/retry` | `REMEDIATION_FAILED`, `REMEDIATION_TIMED_OUT`, `REMEDIATION_HUMAN_BLOCKED` | New attempt and operation key from `REMEDIATION_APPROVED`; preconditions and probe base run again; an unresolved create needs `confirm_no_session=true` |
| Retry CI sync | `POST /operator/cases/{id}/retry-ci` | `CI_FAILED`, `REMEDIATION_FAILED` at stage `ci` | Re-reads check runs for the same verified head SHA; no new session |
| Retry probe verification | `POST /operator/cases/{id}/retry-probe` | `PROBE_INFRASTRUCTURE_BLOCKED`, infrastructure-class head failure | Re-runs the identical snapshot; refused when the probe itself produced the verdict |

Row locks and the one-unfinished-attempt-per-kind index make concurrent
retries produce exactly one new attempt.

## Scope

The pipeline stops at `CI_PASSED` / "Ready for human review". It never merges,
never closes the issue, never asks Devin to fix a failing probe or CI, and
never trusts Devin-reported probe results. See
[docs/architecture.md](docs/architecture.md) for component boundaries,
lifecycle semantics, and status mapping;
[docs/threat-model.md](docs/threat-model.md) for the spend, secret, and probe
isolation boundaries; and [docs/known-limitations.md](docs/known-limitations.md)
for what remains (notably TLS on the internal verifier hops, one probe at a
time per runner, and unresolved Debian base-image CVEs). `MERGED` and
`MERGE_VERIFIED` stay documented future states; nothing files regression
issues automatically.
