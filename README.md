# Superset Devin Remediator

A production-shaped issue remediation service. It accepts signed GitHub issue
webhooks, evaluates a deterministic zero-ACU rubric, persists an append-only
lifecycle in PostgreSQL, runs a bounded Devin triage session (live or fake),
validates the structured triage result, and exposes an authenticated operator
dashboard. Notification outbox rows are written but not dispatched yet.

```text
GitHub issue opened
→ deterministic zero-ACU eligibility filter
→ eligible issue automatically queues Devin triage
→ durable create intent
→ bounded Devin session
→ schema-validated triage result
→ awaiting remediation approval
```

Phase 2 (this release) implements the live and fake Devin clients for **triage
only**. Slack approval, remediation sessions, PR creation, and merging are not
implemented.

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

## Run

```bash
cp .env.example .env
docker compose up --build
```

Open <http://localhost:8000> and sign in with `OPERATOR_TOKEN`.

## Scenarios

| Scenario | Fixture | Issue | Expected outcome | Why |
| --- | --- | ---: | --- | --- |
| `good` | `issue_good_candidate.json` | 4213 | `CI_PASSED` | Complete reproduction, objective checks, acceptance criteria, and existing-pattern evidence |
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

`good` reaches `CI_PASSED` only because `SIMULATION_AUTO_APPROVE_REMEDIATION=true`
drives the fake-only remediation path kept from Phase 1; with it disabled (and
always in live mode) the case parks at `AWAITING_REMEDIATION_APPROVAL`.

Run the scenarios through the real webhook endpoint:

```bash
uv run python scripts/simulate.py --scenario all --wait
uv run python scripts/simulate.py --scenario good --repeat 2
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
| `DEVIN_TRIAGE_TIMEOUT_SECONDS` | `1800` | Absolute deadline after which the session is terminated remotely |
| `DEVIN_POLL_INTERVAL_SECONDS` | `15` | Poll interval; live mode enforces `>= 10` (10–30 recommended) |
| `DEVIN_HTTP_TIMEOUT_SECONDS` | `30` | Per-request HTTP timeout |
| `DEVIN_HTTP_MAX_RETRIES` | `3` | Bounded retries with backoff and jitter for GET/list only |
| `DEVIN_REPOS_FORMAT` | `https://github.com/{repository}` | How the allowlisted repository is passed in `repos` |
| `GITHUB_BASE_REF` | `master` | Ref resolved to the exact base SHA pinned in each session |
| `GITHUB_API_TOKEN` | unset | Optional token for the GitHub commits API used to resolve the base SHA |
| `RECONCILE_MAX_ATTEMPTS` | `3` | Bounded list-by-tag lookups after an uncertain create |
| `MAX_ATTEMPTS_PER_KIND` | `3` | Per-case triage/remediation spend cap |
| `SIMULATION_AUTO_APPROVE_REMEDIATION` | `true` | Fake-only; must be `false` in live mode |
| `LOG_LEVEL` | `INFO` | Application log level |
| `COOKIE_SECURE` | `false` | Set `true` when dashboard traffic is behind TLS |

`.env.example` uses very short fake-mode timeout and poll values so the
simulation finishes in seconds. Live mode fails closed on startup when the API
key or org id is missing, the base URL is not HTTPS, the poll interval is under
10 seconds, or auto-approve is enabled.

### Live mode

```bash
DEVIN_CLIENT_MODE=live
DEVIN_API_KEY=...            # service-user key
DEVIN_ORG_ID=...
DEVIN_TRIAGE_MAX_ACU=5
DEVIN_TRIAGE_TIMEOUT_SECONDS=1800
DEVIN_POLL_INTERVAL_SECONDS=15
SIMULATION_AUTO_APPROVE_REMEDIATION=false
```

Each session is created with exactly one allowlisted repository at the exact
base SHA, `max_acu_limit`, the operation key as an exact tag plus
`repo:`/`issue:`/`kind:`/`case:`/`attempt:` correlation tags,
`structured_output_required=true`, and the versioned prompt in
`remediator/devin/prompts/triage_v1.md`. Issue content is fenced as untrusted
data inside the prompt. No automated test calls the real API.

The worker's Docker `stop_grace_period` must exceed
`WORKER_SHUTDOWN_TIMEOUT_SECONDS` so in-flight jobs can drain before SIGKILL.

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

Set `SIMULATION_AUTO_APPROVE_REMEDIATION=false` to park eligible cases at
`AWAITING_REMEDIATION_APPROVAL`. With that setting, `simulate.py --wait` stops
at the approval state and prints the operator approve curl command. The Slack
approval request is written to the outbox; a later phase will dispatch it and
Slack interactivity will call the same approval service used by the operator
endpoint.

## Project layout

```text
remediator/
  api/             FastAPI app, webhook, dashboard, auth, operator routes
  devin/           DevinClient protocol, live (httpx) + fake clients, status
                   mapping, Draft 7 triage schema, versioned prompt, tags
  github_refs.py   Base commit SHA resolution (GitHub API or fake)
  github/          Signature verification
  templates/       Jinja2 pages and HTMX partials
  static/          CSS and vendored HTMX
  worker/          Claims, lifecycle processing, DevinRunner, worker entrypoint
  config.py        Environment settings
  db.py            Async engine/session helpers
  lifecycle.py     States, transitions, and transition audit writes
  models.py        SQLAlchemy models
  rubric.py        Pure deterministic issue evaluation
alembic/            Async migration environment; 0001 schema, 0002 Phase 2 attempts
fixtures/github/    Simulation webhook payloads
scripts/            Endpoint-only simulator
tests/              Unit and Postgres integration tests
docs/architecture.md
docs/threat-model.md
```

## Scope

Phase 2 performs triage only and does not dispatch GitHub, Slack, or outbox
notifications, create remediation sessions, open PRs, or merge. See
[docs/architecture.md](docs/architecture.md) for component boundaries,
lifecycle semantics, status mapping, and
[docs/threat-model.md](docs/threat-model.md) for the spend and secret
boundaries.
