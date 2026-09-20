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
→ deterministic zero-ACU eligibility filter
→ eligible issue automatically queues Devin triage
→ durable create intent
→ bounded Devin session
→ schema-validated triage result
→ AWAITING_REMEDIATION_APPROVAL (Slack notification, only for remediation_candidate)
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

Phase 4 (this release) adds the remediation session and the independent
evidence chain behind it. Structured output, a PR URL, or session exit are
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
- A zero-ACU deterministic eligibility filter rejects non-eligible issues
  (no Devin session is ever created for them) and queues eligible issues for
  triage.
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
- A validated `remediation_candidate` verdict creates one `approval_requests`
  row and a Slack outbox row. The worker posts a Block Kit message (escaped,
  length-capped, no raw issue body, no secrets) with `Approve remediation`,
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
- Repository code executes in exactly one place: the **verifier container**
  (`docker/verifier/Dockerfile`, `remediator.verifier`). It holds no
  application credential (no `env_file`, never imports `remediator.config`),
  runs as a dedicated non-root UID on a read-only root filesystem with a bounded
  `/tmp`, `cap_drop: ALL`, `no-new-privileges`, PID/memory limits and its own
  network, and ships git/bash/python3/node/npm/yarn for probe runtimes. The
  worker only ever uses `PROBE_RUNNER_MODE=fake` (simulation) or `remote`, and
  before each probe checks the verifier's `/health`: any visible credential,
  UID 0 or a writable root makes the run an *infrastructure* failure, never a
  verdict. There is no worker-side `local` mode. Inside the verifier the runner
  clones the exact commit, runs the snapshotted script with
  `create_subprocess_exec` (no shell), a minimal environment, rlimits, a deadline
  that covers process exit (not just output), and kills every descendant carrying
  the run marker (including `setsid` escapees) before removing the workspace.
  See [docs/probes.md](docs/probes.md), [docs/threat-model.md](docs/threat-model.md)
  and [docs/known-limitations.md](docs/known-limitations.md).
- Slack updates for every milestone (queued/running, session link, PR found,
  probe base/head, CI state, failure/blocked reason, "Ready for human review")
  go through the same outbox and never change remediation state.

## Run

```bash
cp .env.example .env
docker compose up --build
```

Open <http://localhost:8000> and sign in with `OPERATOR_TOKEN`.

## Scenarios

| Scenario | Fixture | Issue | Expected outcome | Why |
| --- | --- | ---: | --- | --- |
| `good` | `issue_good_candidate.json` | 4213 | `AWAITING_REMEDIATION_APPROVAL` | Complete reproduction, objective checks, acceptance criteria, and existing-pattern evidence |
| `needs-scoping` | `issue_needs_scoping.json` | 4321 | `POLICY_REJECTED` without Devin session | Missing reproducibility and acceptance details |
| `deterministic` | `issue_deterministic.json` | 4422 | `POLICY_REJECTED` without Devin session | Dependency/version-bump work is deterministic automation |
| `human-led` | `issue_human_led.json` | 4501 | `POLICY_REJECTED` without Devin session | Architecture and breaking-change reasoning requires human ownership |
| `triage-infeasible` | `issue_triage_infeasible.json` | 4533 | `POLICY_REJECTED` after triage | Fake triage reports that remediation requires a product decision |
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
| `GITHUB_REQUIRED_LABEL` | *(empty)* | Optional opt-in intake label; empty (default) evaluates every opened issue |
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
| `DEVIN_REPOS_FORMAT` | `https://github.com/{repository}` | How the allowlisted repository is passed in `repos`. **Unverified against the live API** — the v3 spec types `repos` as `array[string]` without documenting the entry format; confirm with one minimal live session (or Devin support) before enabling live mode |
| `GITHUB_BASE_REF` | `master` | Ref resolved to the exact base SHA pinned in each session |
| `RECONCILE_MAX_ATTEMPTS` | `3` | Bounded list-by-tag lookups after an uncertain create |
| `MAX_ATTEMPTS_PER_KIND` | `3` | Per-case triage/remediation spend cap |
| `DEVIN_REMEDIATION_MAX_ACU` | `15` | `max_acu_limit` sent on every remediation session |
| `DEVIN_REMEDIATION_TIMEOUT_SECONDS` | `5400` | Absolute remediation deadline; final GET then `DELETE`; live mode enforces `>= 600` |
| `DEVIN_REMEDIATION_BRANCH_PREFIX` | `devin/` | Required prefix of the PR head branch |
| `PROBE_RUNNER_MODE` | `fake` | `fake` (deterministic by issue number) or `remote` (credential-free verifier container); the worker has no `local` mode |
| `PROBE_VERIFIER_URL` | *(unset)* | Verifier base URL, required with `remote`; the worker refuses a verifier whose `/health` shows credentials, UID 0 or a writable root |
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
setting alone is not trusted: the worker re-checks the verifier's `/health`
before every single probe.

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
                   protocol, fake + local (isolated clone) runners
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
                    PR evidence, CI snapshots
fixtures/github/    Simulation webhook payloads
probes/             Immutable probe registry (probe.yaml + probe.sh per issue)
scripts/            Endpoint-only simulator, register_probe.py
tests/              Unit and Postgres integration tests
docs/architecture.md
docs/threat-model.md
docs/probes.md           Probe authoring guide
docs/simulation.md       Simulation guide
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

Phase 4 stops at `CI_PASSED` / "Ready for human review". It never merges,
never closes the issue, never asks Devin to fix a failing probe or CI, and
never trusts Devin-reported probe results. See
[docs/architecture.md](docs/architecture.md) for component boundaries,
lifecycle semantics, and status mapping;
[docs/threat-model.md](docs/threat-model.md) for the spend, secret, and probe
isolation boundaries; and [docs/known-limitations.md](docs/known-limitations.md)
for what remains for Phase 5 (notably enforced egress control and
authentication for the verifier link).
