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
- A worker claims deliveries with `FOR UPDATE SKIP LOCKED` and advances eligible
  issues through deterministic triage and remediation simulation.
- The dashboard refreshes operational sections with HTMX and supports operator
  retry/cancel actions.
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
| `needs-scoping` | `issue_needs_scoping.json` | 4321 | `POLICY_REJECTED` | Missing reproducibility and acceptance details |
| `deterministic` | `issue_deterministic.json` | 4422 | `POLICY_REJECTED` | Dependency/version-bump work is deterministic automation |
| `human-led` | `issue_human_led.json` | 4501 | `POLICY_REJECTED` | Architecture and breaking-change reasoning requires human ownership |
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
| `DEVIN_CLIENT` | `fake` | Phase 1 only accepts `fake` |
| `SIMULATION_AUTO_APPROVE` | `true` | Auto-advance approval stages in simulation |
| `LOG_LEVEL` | `INFO` | Application log level |

## Development and test database

```bash
make fmt lint typecheck
docker compose up -d db
cp .env.example .env
uv run alembic upgrade head
make test
```

The Postgres init script creates both `remediator` and `remediator_test`.
Integration tests use `TEST_DATABASE_URL`, defaulting to
`postgresql+asyncpg://remediator:remediator@localhost:5432/remediator_test`.
They require the local test database and fail clearly if it is unavailable.

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
