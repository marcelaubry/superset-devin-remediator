# Superset Devin Remediator

Phase 1 is a production-shaped, simulation-first issue remediation slice. It
accepts signed GitHub issue webhooks, evaluates a deterministic rubric, persists
an append-only lifecycle in PostgreSQL, runs a fake Devin client, and exposes an
authenticated operator dashboard. Notification outbox rows are written but
never dispatched in Phase 1.

## How it works

- FastAPI verifies the raw GitHub webhook body before parsing and filters
  deliveries by repository, event/action, and required label.
- PostgreSQL stores webhook payloads, current cases, attempts, transitions, and
  notification intents.
- A zero-ACU deterministic eligibility filter rejects non-eligible issues or
  sends eligible issues to fake Devin triage.
- A worker claims deliveries with `FOR UPDATE SKIP LOCKED` and advances
  triage-feasible issues through remediation after Slack approval.
- The dashboard refreshes operational sections with HTMX and supports operator
  retry/cancel and remediation approve/reject actions.
- The fake Devin client deterministically produces success, failure, or blocked
  outcomes from the issue number.

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
| `failure` | `issue_fake_failure.json` | 4515 | `FAILED` | Fake Devin fails for issue numbers divisible by five |
| `blocked` | `issue_human_blocked.json` | 4529 | `HUMAN_BLOCKED` | Fake Devin requests human intervention for issue numbers divisible by seven |
| `wrong-repo` | `issue_wrong_repo.json` | 4601 | filtered out | Repository does not match `GITHUB_REPOSITORY` |
| `missing-label` | `issue_missing_label.json` | 4602 | filtered out | Required `devin-candidate` label is absent |

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
| `DEVIN_CLIENT` | `fake` | Phase 1 only accepts `fake` |
| `DEVIN_POLL_INTERVAL_SECONDS` | `0.01` | Fake poll delay; real clients would use about 15 seconds |
| `DEVIN_MAX_POLLS` | `5` | Poll budget before `TIMED_OUT` |
| `MAX_ATTEMPTS_PER_KIND` | `3` | Per-case triage/remediation spend cap |
| `SIMULATION_AUTO_APPROVE_REMEDIATION` | `true` | Auto-approve remediation after a feasible triage verdict |
| `LOG_LEVEL` | `INFO` | Application log level |
| `COOKIE_SECURE` | `false` | Set `true` when dashboard traffic is behind TLS |

A real Devin client must raise `DEVIN_POLL_INTERVAL_SECONDS` (approximately 15
seconds) and `DEVIN_MAX_POLLS` together; the fake defaults would time out any
real session.

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
at the approval state and prints the operator approve curl command. Phase 1
writes the Slack approval request to the outbox; the Phase 2 Slack outbox worker
will dispatch it and Slack interactivity will call the same approval service
used by the operator endpoint.

## Project layout

```text
remediator/
  api/             FastAPI app, webhook, dashboard, auth, operator routes
  devin/           Devin protocol and deterministic fake client
  github/          Signature verification
  templates/       Jinja2 pages and HTMX partials
  static/          CSS and vendored HTMX
  worker/          Claims, lifecycle processing, and worker entrypoint
  config.py        Environment settings
  db.py            Async engine/session helpers
  lifecycle.py     States, transitions, and transition audit writes
  models.py        SQLAlchemy models
  rubric.py        Pure deterministic issue evaluation
alembic/            Async migration environment and initial schema
fixtures/github/    Simulation webhook payloads
scripts/            Endpoint-only simulator
tests/              Unit and Postgres integration tests
docs/architecture.md
```

## Scope

Phase 1 deliberately uses only the fake Devin client and does not dispatch
GitHub, Slack, or outbox notifications. See
[docs/architecture.md](docs/architecture.md) for component boundaries,
lifecycle semantics, security boundaries, and Phase 2 work.
