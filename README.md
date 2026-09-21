# Superset Devin Remediator

An event-driven service that receives GitHub issues from an allowlisted
repository, runs a bounded Devin triage session, asks a human for approval in
Slack, starts a bounded Devin remediation session, validates the pull request
and the CI run for the exact commit it verified, and then stops: the pull
request is left open for human review and is never merged automatically.

## How it works

1. A signed GitHub `issues/opened` webhook starts the workflow. The signature is
   verified, the repository must be allowlisted, and the delivery is recorded so
   a replay cannot start the work twice.
2. A deterministic context check runs first. It reads the issue text and asks
   whether there is a concrete problem, an expected outcome, and enough of a
   reproduction or code-path signal to start investigating. An issue that fails
   is rejected with the missing items spelled out, before any session exists, so
   an incomplete issue consumes zero ACUs.
3. A bounded Devin session performs repository-aware triage under an ACU budget,
   a timeout, and a per-repository concurrency lease. The session is created
   with an idempotency key, so a retried webhook reuses the existing session
   instead of paying for a second one.
4. A schema-valid triage result creates a Slack approval request. Invalid or
   unparseable output fails the case instead of guessing. The Slack message
   carries the triage summary, the evidence links, and approve/reject buttons
   bound to an opaque token.
5. Approval applies the `devin:remediate` label. The label is the actual trigger
   and is only trusted once GitHub confirms it through a second signed webhook,
   so a click in Slack alone cannot start paid work. A rejection is recorded,
   commented on the issue, and ends the case.
6. A bounded Devin remediation session runs and produces a pull request. Under
   the default `PROBE_POLICY=required`, an approved immutable acceptance probe
   must first reproduce the defect at the pinned base commit, so there is
   evidence the bug was real before any fix is attempted; without such a probe
   the case is blocked rather than remediated.
7. The service then validates the result rather than trusting the session: the
   pull request must be in the expected repository, authored by the configured
   service user, based on the recorded base SHA, linked to the originating
   issue, and limited to plausible files. The head SHA is corroborated against
   GitHub, the probe is re-run unchanged at that head, and CI is read for that
   exact head SHA — not for the branch, which can move.
8. The case ends at **Ready for human review**. Nothing is merged, closed, or
   force-pushed by the service. Failures stop in a state that says what failed
   and what the operator can retry, visible on the dashboard.

## Quick start

### A. Run the Docker simulation

Docker (with Compose v2) is the only substantial prerequisite. No GitHub, Slack
or Devin credentials are needed, and no ACUs are consumed: the stack runs with
fake GitHub, Slack, Devin and probe adapters that contact no external service.

```bash
git clone https://github.com/marcelaubry/superset-devin-remediator.git
cd superset-devin-remediator

cp .env.example .env
./scripts/demo.sh up
./scripts/demo.sh run
```

`./scripts/demo.sh up` is idempotent. It creates `.env` from `.env.example` if
it is missing, replaces the remaining `change-me` placeholders with locally
generated random values without touching anything you have already set, writes
the git-ignored `docker/secrets/verifier_hmac_key` only if it does not exist,
builds the images, applies the migrations, and waits for the API to be healthy.

`./scripts/demo.sh run` drives the workflow through the real HTTP surface —
signed GitHub webhooks, signed Slack interactions, operator routes — from inside
the API image, and asserts each outcome. It creates representative cases: an
issue rejected by the context check, a triage session parked for approval, a
triage session blocked on a question, a Slack rejection, an approval whose case
then blocks because no acceptance probe is registered (`PROBE_POLICY=required`),
a successful remediation that reaches CI and stops at ready-for-human-review,
and a remediation whose probe fails at head.

The dashboard is at <http://localhost:8000>; sign in with the operator token:

```bash
./scripts/demo.sh token
```

Stop the demo, keeping the local data:

```bash
./scripts/demo.sh down
```

Optionally delete the local demo database and verifier workspace volumes. This
is destructive:

```bash
./scripts/demo.sh reset
```

### B. Run live

A live run against a real repository needs a Devin service-user API key and
organization ID, a fine-grained GitHub token plus a webhook on the target
repository, a Slack bot token with an interactivity endpoint, a public HTTPS
callback URL for both webhooks, and a remote isolated verifier for probe
execution. The step-by-step procedure, including the readiness check, is in
[docs/canary-runbook.md](docs/canary-runbook.md).

`PROBE_POLICY` decides what happens when an issue has no registered acceptance
probe:

- `required` (default) — remediation is blocked until an approved probe
  reproduces the defect at the pinned base commit and passes unchanged at head.
- `if_available` — for demonstrations without probes. An already-approved,
  label-confirmed case goes straight to the bounded session; every other gate is
  unchanged, but the case has no behavioral verification — only the PR structure
  and exact-head CI are validated — and it is recorded and displayed as
  `probe_status=not_configured` rather than as a verified fix.

## Development with uv

```bash
uv sync --frozen
docker compose up -d db
make test
```

Formatting, linting and typing:

```bash
make fmt lint typecheck
```

None of this is needed for the Docker demo above.

## Architecture decisions

**Event-driven webhooks over repository polling.** The service reacts to signed
GitHub deliveries instead of scanning the repository on a timer, so work starts
immediately and costs nothing while the repository is quiet. The trade-off is
that the webhook path must be publicly reachable and every delivery has to be
signature-verified and de-duplicated, which is where a fair amount of the
handling logic lives.

**PostgreSQL for durability and idempotency.** Every case, attempt, approval,
transition and outgoing message is a row, and operation keys make session
creation and side effects exactly-once across restarts and retries. A database
is heavier than an in-memory queue, but the thing being protected is paid,
externally visible work: a crash mid-remediation must not create a second Devin
session or post a second Slack approval.

**Separate API and worker processes.** The API answers webhooks and dashboard
requests in milliseconds and commits the intent; a worker claims cases and runs
the slow lifecycle — sessions, probes, PR validation, CI polling — with its own
concurrency leases. This keeps a stalled Devin session from blocking webhook
delivery, at the cost of an asynchronous model where the dashboard shows work in
progress rather than a synchronous result.

**Human Slack approval before remediation.** Triage is cheap and automatic;
remediation is not, so it is gated on a person clicking approve, and the
approval is only honored once GitHub confirms the resulting label through a
signed webhook. This deliberately adds latency and a human dependency, and it is
what keeps a misjudged triage from spending ACUs and opening pull requests
unsupervised.

**Credential-free isolated verifier and independent verification.** Reproduction
probes clone and execute untrusted repository code, so they run in a separate
service that holds no GitHub, Slack or Devin credentials, behind an exact-host
egress allowlist, with the same probe executed at base and at head. The service
then re-derives the outcome from GitHub — PR metadata, head SHA, CI for that SHA
— rather than believing the session's own report. It is more moving parts than
running probes in the worker, and it means a compromised probe gains nothing
worth stealing.

## Superset fork and results

Target repository: <https://github.com/marcelaubry/superset>, a fork of
[apache/superset](https://github.com/apache/superset).

| Issue | Outcome | PR |
| --- | --- | --- |
| [#2 bump js-yaml override (GHSA-2883-xcg3-v3hh)](https://github.com/marcelaubry/superset/issues/2) | Not remediated — no remediation PR | — |
| [#3 Alembic migration graph has two heads](https://github.com/marcelaubry/superset/issues/3) | Not remediated — no remediation PR | — |
| [#4 short digit-only strings formatted as epoch offsets](https://github.com/marcelaubry/superset/issues/4) | Not remediated — no remediation PR | — |
| [#5 second version of #4](https://github.com/marcelaubry/superset/issues/5) | Not remediated — no remediation PR | — |
| [#6 CheckboxControl has no accessible name](https://github.com/marcelaubry/superset/issues/6) | Remediated, ready for human review | [#7 — open, draft](https://github.com/marcelaubry/superset/pull/7) |
| [#8 connect_args from adjust_engine_params are dropped](https://github.com/marcelaubry/superset/issues/8) | Remediated, ready for human review | [#9 — open, draft](https://github.com/marcelaubry/superset/pull/9) |
| [#10 get_columns_description runs the probe statement twice](https://github.com/marcelaubry/superset/issues/10) | Remediated, ready for human review | [#11 — open, draft](https://github.com/marcelaubry/superset/pull/11) |

Three issues were remediated and their pull requests are open drafts awaiting
human review; none has been merged, which is the intended end state. The
remaining issues have no remediation pull request: they were filtered or not
carried through to remediation, and the ones stopped by the deterministic
context check cost nothing, which is the point of running that check before any
session is created.

## Project structure

```text
remediator/api/         FastAPI app: webhooks, Slack actions, operator dashboard
remediator/worker/      Lifecycle processing, remediation pipeline, outbox dispatch
remediator/devin/       Devin API client, result schemas, versioned prompts
remediator/github/      Signature verification, issues/PR/CI client (live + fake)
remediator/slack/       Request signing, Block Kit approval messages (live + fake)
remediator/verifier/    Credential-free probe verifier and its isolated runner
remediator/templates/   Jinja2 dashboard pages and HTMX partials
probes/                 Immutable reproduction probe registry
scripts/                demo.sh (Docker-only demo), simulate.py, helpers
tests/                  Unit and integration tests against Postgres + fakes
docs/                   Architecture, runbooks, reference documentation
Dockerfile              Application image (API, worker, simulator)
docker-compose.yml      Full stack: db, migrate, api, worker, verifier, proxy
.env.example            Documented configuration, fake-mode by default
```

## Further documentation

- [Canary runbook](docs/canary-runbook.md)
- [Architecture](docs/architecture.md)
- [Simulation](docs/simulation.md)
- [Probes](docs/probes.md)
- [Threat model](docs/threat-model.md)
- [Known limitations](docs/known-limitations.md)
