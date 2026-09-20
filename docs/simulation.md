# Simulation guide

Everything here runs against the fake Devin, GitHub, Slack and probe adapters
through the production HTTP endpoints. No simulation or automated test calls
`api.devin.ai`, `api.github.com`, `slack.com`, or executes an external
repository; `tests/conftest.py` forces fake mode and drops credentials.

## Start the stack

```bash
cp .env.example .env            # fake-mode values: short timeouts, placeholder secrets
docker compose up --build -d    # db (Postgres), api (:8000), worker
uv run alembic upgrade head     # only needed when running the worker outside Docker
```

The simulator (`scripts/simulate.py`) reads the signing secrets and operator
token from `.env`, signs every webhook exactly as GitHub/Slack would, and never
prints them.

```bash
uv run python scripts/simulate.py --scenario all --wait      # Phase 1-4, a few minutes
uv run python scripts/simulate.py --scenario phase3
uv run python scripts/simulate.py --scenario phase4
uv run python scripts/simulate.py --scenario remediate       # any single Phase 4 scenario
uv run python scripts/simulate.py --scenario good --bad-signature
```

Phase 4 scenario names: `remediate`, `remediate-base-passes`,
`remediate-probe-infra`, `remediate-head-fails`, `remediate-forbidden-files`,
`remediate-ci-failed`, `remediate-uncertain-create`,
`remediate-unlabeled-intake`.

Phase 3/4 scenarios decide their fixture issue exactly once, as production
would; to re-run them reset the database with
`docker compose down -v && docker compose up -d`.

## Phase 4 walkthrough (`remediate`, issue 4702)

1. `issues/opened` webhook for the good-candidate body → eligibility → fake
   triage → `AWAITING_REMEDIATION_APPROVAL`, Slack message in
   `slack_fake_messages`.
2. Simulator reads the opaque token from the fake Slack message and posts a
   signed `block_actions` approve → decision recorded, label outbox row.
3. Worker applies `devin:remediate` (fake GitHub) → simulator posts the signed
   `issues/labeled` webhook → `REMEDIATION_APPROVED`.
4. Pipeline: preconditions → `probe_snapshots` row from
   `probes/apache/superset/4702/` → `PROBE_VALIDATING_BASE` → fake runner
   returns exit 1 → `REMEDIATION_CREATE_INTENT` → fake session
   (`fake-remediation-4702-…`, 0.25 fake ACU per poll) → `REMEDIATING` →
   `OUTPUT_VALIDATING` → `PR_DISCOVERED` (from `pull_requests[]`) →
   `PR_VALIDATING` (fake GitHub pull #13702 = 9000 + issue, files, compare,
   timeline) →
   `PROBE_VALIDATING_HEAD` (exit 0) → `PR_VALIDATED` → `CI_PENDING` → fake
   check runs complete → `CI_PASSED`.
5. The simulator asserts: exactly one paid remediation attempt, `BASE` and
   `HEAD` probe executions both `MATCHED`, PR URL and head SHA recorded on the
   attempt, CI snapshots present, `ready_for_human_review` true, a duplicate
   `devin:remediate` webhook changes nothing (still one attempt), and the Slack
   message reads "Ready for human review".

Other Phase 4 scenarios reuse steps 1–3 and diverge at the step the fixture
targets (see the table in the README).

## Fixture map (`remediator/fixtures.py`)

Fake Devin, fake GitHub and the fake probe runner all key their behaviour on
the issue number so a scenario is reproducible end to end:

| Issue | Fixture | Exercised by |
| ---: | --- | --- |
| 4702 | success | simulate `remediate`, integration tests |
| 4703 | malformed structured output | `test_phase4_remediation` |
| 4706 | uncertain create + tag reconciliation | simulate, tests |
| 4708 | session never finishes → final GET → DELETE; failed termination variant | tests (`REMEDIATION_TERMINATION_PENDING`, `REMEDIATION_TIMED_OUT`) |
| 4709 | `no_change_needed` returned by Devin | tests |
| 4712 | `needs_human` with blocking questions | tests |
| 4714 | `outcome=failed` | tests |
| 4717 | PR claimed in output but absent from `pull_requests[]`/GitHub | tests |
| 4721 / 4723 | contradictory PR URL / head SHA | tests |
| 4724 / 4726 / 4727 | wrong repository / base branch / branch prefix | tests |
| 4729 | PR already merged | tests |
| 4733 | unexpected author | tests |
| 4736 | body says `Closes apache/superset#47360` (substring) — `#1` vs `#10` linkage | tests |
| 4738 | forbidden file (`.github/workflows`) | simulate, tests |
| 4741 | scope expansion (> max changed files) | tests |
| 4742 | head diverged from pinned base | tests |
| 4744 | probe passes at base | simulate, tests |
| 4747 | probe fails at head | simulate, tests |
| 4748 | probe infrastructure unavailable | simulate, tests |
| 4751 / 4754 / 4756 | CI failed / pending until timeout / absent | simulate (4751), tests |
| 4222 | unlabeled `issues/opened` intake | simulate, `test_webhook` |

Additional integration tests cover duplicate `devin:remediate` webhook
delivery, missing approval and stale triage hash, missing/altered probe
manifest or script, manual retry and concurrent retries producing one
attempt, and a worker restart (re-claim) in every remediation state.

## Running the checks the PR ran

```bash
uv run ruff format --check . && uv run ruff check .
uv run mypy remediator scripts
uv run pytest -q                                   # needs the compose Postgres
DATABASE_URL=... uv run alembic downgrade base && uv run alembic upgrade head
docker compose down -v && docker compose build && docker compose up -d
uv run python scripts/simulate.py --scenario all --wait
```
