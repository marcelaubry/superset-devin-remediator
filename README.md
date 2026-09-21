# Superset Devin Remediator

A Dockerized service that turns GitHub issues on an allowlisted repository into
reviewed pull requests, with a human in the loop.

For each incoming issue it checks **contextual completeness** deterministically
(zero ACUs, no session is created for an under-specified issue), runs a
**bounded Devin triage** session, asks for **Slack approval**, runs a **bounded
Devin remediation** session only after a human approves, then **validates the
resulting PR and the CI run for the exact head SHA** it verified. It never
merges, closes or force-pushes anything: the final step is always human review.

## Workflow

```text
GitHub issue
  → context eligibility   (deterministic, zero ACUs)
  → Devin triage          (bounded session, structured result)
  → Slack approval        (signed interactive message, human decision)
  → Devin remediation     (bounded session, only after approval)
  → PR validation         (PR exists, belongs to the issue, head SHA corroborated)
  → CI                    (checks for that exact head SHA)
  → human review          (nothing merges automatically)
```

## Quick start with Docker

You need only **Docker** (with Compose v2) and **Git**. No host Python, uv,
Node, PostgreSQL, cloudflared, or Slack/GitHub/Devin credentials are required:
the default configuration uses fake Devin, GitHub, Slack and probe adapters and
contacts no external service.

```bash
git clone https://github.com/marcelaubry/superset-devin-remediator.git
cd superset-devin-remediator

cp .env.example .env      # fake mode defaults
./scripts/demo.sh up      # generate local secrets, build, migrate, start
./scripts/demo.sh run     # drive the representative scenarios
```

`./scripts/demo.sh up` is idempotent and safe to re-run. It:

- creates `.env` from `.env.example` if missing (`cp` above is optional);
- replaces the `change-me` placeholders for `GITHUB_WEBHOOK_SECRET`,
  `OPERATOR_TOKEN` and `SLACK_SIGNING_SECRET` with locally generated random
  values, and never overwrites a value you already set;
- creates the git-ignored `docker/secrets/verifier_hmac_key` if it does not
  already exist;
- runs `docker compose up --build -d` (Postgres, Alembic migrations, API,
  worker, credential-free verifier, isolated verifier runner, egress proxy) and
  waits for the API to become healthy.

Then open the dashboard and sign in with the operator token:

```bash
open http://localhost:8000        # or just browse to it
./scripts/demo.sh token           # prints OPERATOR_TOKEN
```

Stop the stack (local data kept):

```bash
./scripts/demo.sh down
```

If you prefer the raw commands, `scripts/demo.sh` is a short shell script; the
equivalent is:

```bash
cp -n .env.example .env
openssl rand -hex 32 > docker/secrets/verifier_hmac_key
docker compose up --build -d
```

## Simulate the workflow

One Docker-only command (it runs inside the API image, so nothing is installed
on the host):

```bash
./scripts/demo.sh run
```

It drives the real HTTP endpoints — signed GitHub webhooks, signed Slack
interactions, operator routes — and asserts the outcome of each case. Expected
result: the command prints `all phase 3/4/5 checks passed`, and
<http://localhost:8000> shows cases covering

| Case | Dashboard state |
| --- | --- |
| Under-specified issue rejected before any session | `POLICY_REJECTED` |
| Devin triage completed, waiting on a human | `AWAITING_REMEDIATION_APPROVAL` |
| Triage session blocked on a question | `HUMAN_BLOCKED` |
| Slack approval, label confirmed by webhook | `REMEDIATION_APPROVED` |
| Slack rejection recorded, no label, no session | `REMEDIATION_REJECTED` |
| Remediation with PR, head probe and CI verified | `CI_PASSED` (ready for human review) |
| Remediation whose head probe fails verification | `REMEDIATION_FAILED` |

The simulation uses the built-in `apache/superset` fixtures; you do **not** need
to change `GITHUB_REPOSITORY` between the fork and `apache/superset`.
`scripts/simulate.py` also has finer-grained scenarios — see
[docs/simulation.md](docs/simulation.md).

Optional reset — **this deletes the local demo database and verifier workspace
volumes**:

```bash
./scripts/demo.sh reset
```

## Live canary

Running against a real repository additionally requires: Devin service-user
credentials, a fine-grained GitHub token plus a webhook on the target
repository, a Slack app (bot token and signing secret), a public HTTPS callback
URL for the GitHub and Slack webhooks, and an isolated verifier host for probe
execution. Step-by-step instructions are in
[docs/canary-runbook.md](docs/canary-runbook.md).

In live mode the guarantees are unchanged: a human must approve in Slack before
any paid remediation session starts, the PR and the CI run for the exact
verified head SHA are still validated, and nothing is merged automatically. The
explicit option that allows remediation without a reproduction probe
(`LIVE_CANARY_ALLOW_MISSING_PROBE`) is **off by default**; when enabled, the
case is remediated without independent behavioral verification and the
dashboard/PR evidence says so.

## Superset fork and results

Target repository: <https://github.com/marcelaubry/superset> (fork of
[apache/superset](https://github.com/apache/superset)).

| Issue | Outcome | PR |
| --- | --- | --- |
| [#2 bump js-yaml override to clear GHSA-2883-xcg3-v3hh](https://github.com/marcelaubry/superset/issues/2) | Not remediated (open, no PR) | — |
| [#3 Alembic migration graph has two heads](https://github.com/marcelaubry/superset/issues/3) | Not remediated (open, no PR) | — |
| [#4 short digit-only strings formatted as epoch offsets](https://github.com/marcelaubry/superset/issues/4) | Not remediated (open, no PR) | — |
| [#5 V2 of #4, edited](https://github.com/marcelaubry/superset/issues/5) | Not remediated (open, no PR) | — |
| [#6 CheckboxControl has no accessible name](https://github.com/marcelaubry/superset/issues/6) | Remediated, awaiting human review | [#7 (open, draft)](https://github.com/marcelaubry/superset/pull/7) |
| [#8 connect_args from adjust_engine_params are dropped](https://github.com/marcelaubry/superset/issues/8) | Remediated, awaiting human review | [#9 (open, draft)](https://github.com/marcelaubry/superset/pull/9) |
| [#10 get_columns_description runs the probe statement twice](https://github.com/marcelaubry/superset/issues/10) | Remediated, awaiting human review | [#11 (open, draft)](https://github.com/marcelaubry/superset/pull/11) |

All three PRs are open drafts; none has been merged, by design. Issues without
a PR were either filtered before any session was created or not approved for
remediation — the context filter is deterministic and costs zero ACUs, which is
the point of running it first.

## Safety boundaries

- Only allowlisted repositories are accepted; webhook payloads must carry a
  valid HMAC signature, and rendered URLs are allowlist-validated.
- Every Devin session is bounded by ACU budget, timeout and per-repository
  concurrency leases; the deterministic context filter spends zero ACUs.
- Session creation is idempotent (operation keys), so replayed webhooks and
  repeated clicks never create a second paid session.
- Remediation starts only after a signed Slack approval from a human, recorded
  with the deciding user and an append-only event trail.
- The PR is validated against the issue and its head SHA is corroborated with
  GitHub; CI is read for that exact head SHA, and probes run at base then head.
- Nothing is merged, closed or force-pushed automatically — the terminal state
  is "ready for human review".

## Architecture

FastAPI application (webhooks, operator dashboard, Slack interactions) backed by
PostgreSQL, with a separate worker process that drives the case lifecycle, the
Devin API client, and a transactional outbox for GitHub and Slack side effects.
GitHub and Slack adapters have live and fake implementations. Reproduction
probes run in a **credential-free verifier** service that holds no tokens and
executes in an isolated runner behind an exact-host egress allowlist. An
authenticated operator dashboard shows every case, its evidence and the
available operator actions.

Details: [docs/architecture.md](docs/architecture.md) ·
[docs/threat-model.md](docs/threat-model.md).

## Project layout

```text
remediator/   FastAPI app, worker, Devin/GitHub/Slack adapters, probes, verifier
alembic/      Database migrations
docker/       Dockerfiles, Compose secrets, DB init
docs/         Architecture, runbooks and reference documentation
fixtures/     Deterministic fake issues, sessions and CI payloads
probes/       Immutable reproduction probe registry
scripts/      demo.sh (Docker-only demo), simulate.py, operational helpers
tests/        Unit and integration tests (Postgres + fake adapters)
```

## Further documentation

- [Architecture](docs/architecture.md)
- [Canary runbook](docs/canary-runbook.md)
- [Simulation](docs/simulation.md)
- [Probes](docs/probes.md)
- [Threat model](docs/threat-model.md)
- [Concurrency](docs/concurrency.md)
- [Metrics](docs/metrics.md)
- [Known limitations](docs/known-limitations.md)
