# Superset Devin Remediator

A production-shaped issue remediation service. It accepts signed GitHub issue
webhooks, evaluates a deterministic zero-ACU rubric, persists an append-only
lifecycle in PostgreSQL, runs a bounded Devin triage session (live or fake),
validates the structured triage result, asks a human in Slack to approve or
reject remediation, dispatches the approval to GitHub as a `devin:remediate`
label, and exposes an authenticated operator dashboard.

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
```

Phase 3 (this release) adds the Slack approval loop and GitHub label dispatch.
GitHub is the remediation dispatch authority: a case becomes
`REMEDIATION_APPROVED` only when GitHub's signed webhook confirms the label.
Slack never calls the Devin API, approval never creates a Devin session, and
remediation sessions/PRs are deferred to Phase 4.

## How it works

- FastAPI verifies the raw GitHub webhook body before parsing and filters
  deliveries by repository, event/action, and required label.
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
| `missing-label` | `issue_missing_label.json` | 4602 | filtered out | Required `devin-candidate` label is absent |
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

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | local `remediator` database | Async SQLAlchemy connection |
| `GITHUB_WEBHOOK_SECRET` | `change-me` | HMAC secret |
| `OPERATOR_TOKEN` | `change-me` | Dashboard/API token |
| `GITHUB_REPOSITORY` | `apache/superset` | Accepted repository |
| `GITHUB_ALLOWED_EVENTS` | `issues` | Comma-separated event allowlist |
| `GITHUB_ALLOWED_ACTIONS` | `opened,labeled` | Comma-separated action allowlist |
| `GITHUB_REQUIRED_LABEL` | `devin-candidate` | Required issue label; empty disables label filtering |
| `WORKER_POLL_INTERVAL_SECONDS` | `1.0` | Worker idle poll interval |
| `WORKER_CONCURRENCY` | `2` | Concurrent worker loops |
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
requires `GITHUB_TOKEN` and an HTTPS API URL.

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
   if the bot is not invited to the channel). Install it and copy the
   **Bot User OAuth Token** into `SLACK_BOT_TOKEN`.
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
Superset fork only, with *Issues: read/write* and *Metadata: read*, is
sufficient (`GITHUB_TOKEN`). For production prefer a **GitHub App** with
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
                   mapping, Draft 7 triage schema, versioned prompt, tags
  github_refs.py   Base commit SHA resolution (GitHub API or fake)
  github/          Webhook signature verification; fake + live issues client
  slack/           Slack request signing, Block Kit builder, fake + live client
  approvals.py     Approval request creation, Slack action processing, label
                   webhook confirmation
  templates/       Jinja2 pages and HTMX partials
  static/          CSS and vendored HTMX
  worker/          Claims, lifecycle processing, DevinRunner, outbox dispatcher
  config.py        Environment settings
  db.py            Async engine/session helpers
  lifecycle.py     States, transitions, and transition audit writes
  models.py        SQLAlchemy models
  rubric.py        Pure deterministic issue evaluation
alembic/            0001 schema, 0002 Phase 2 attempts, 0003 Phase 3 approvals/outbox
fixtures/github/    Simulation webhook payloads
scripts/            Endpoint-only simulator
tests/              Unit and Postgres integration tests
docs/architecture.md
docs/threat-model.md
```

## Scope

Phase 3 stops at `REMEDIATION_APPROVED`. It never creates a Devin remediation
session, opens PRs, or merges; Slack never calls the Devin API. Phase 4 will
consume `REMEDIATION_APPROVED` cases. See
[docs/architecture.md](docs/architecture.md) for component boundaries,
lifecycle semantics, status mapping, and
[docs/threat-model.md](docs/threat-model.md) for the spend and secret
boundaries.
