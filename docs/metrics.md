# Metrics

`GET /metrics` (Prometheus text format) requires the operator bearer token or
the operator session cookie; unauthenticated scrapes are `401`. Every
case/session/probe series carries a `mode` label that is set once at process
start from the configured adapters:

| `mode` | Condition |
| --- | --- |
| `live` | Devin, GitHub **and** Slack clients are live |
| `simulated` | anything else, including the real-Slack / fake-GitHub / fake-Devin workflow and every test |

A process that has not finished configuration reports `simulated`, so fake
provider data can never be presented as live.

## Scrape targets

Prometheus client series live in the process that increments them, and the
API and worker are separate containers, so there are two targets:

| Target | Default | Serves |
| --- | --- | --- |
| API `GET /metrics` | `:8000` | request-edge counters (`webhooks_rejected_total`, `operator_requests_rejected_total`, ...) and the database-derived gauges (`case_queue_depth`, `active_jobs`, `capacity_*`, `waiting_for_capacity`, `human_blocked`, `outbox_backlog`), refreshed on every scrape |
| worker `GET /metrics` | `:8001` (`WORKER_METRICS_PORT`, `0` disables) | the worker's own counters and histograms: `probe_outcomes_total`, `devin_session_outcomes_total`, `provider_requests_total`, `provider_request_seconds`, `capacity_denied_total`, `retries_total`, `reconciliations_total`, `time_to_milestone_seconds`, ... |

Both require `Authorization: Bearer <OPERATOR_TOKEN>`; the worker endpoint
does not accept the operator session cookie. With several worker replicas,
scrape each one — counters are per process and are not aggregated.

## Label policy

Labels are closed enums only: `mode`, `state`, `kind`, `phase`, `outcome`,
`target`, `milestone`, `actor`, `provider`, `method`, `status_class`,
`result`, `reason`, `channel`, `status`. Issue numbers, case ids, session
ids, PR URLs, user ids, repository names, branches and paths are never
labels (`tests/integration/test_metrics.py` asserts this against the rendered
exposition).

## Series

### Request edge (API process)

| Series | Labels | Meaning |
| --- | --- | --- |
| `webhook_requests_total` | `result` | GitHub deliveries: `accepted`, `deduplicated`, `filtered`, `invalid_signature`, `bad_request` |
| `slack_action_requests_total` | `result` | Slack interactions by outcome (`approved`, `rejected`, `unauthorized`, `stale_token`, ...) |
| `operator_requests_rejected_total` | `reason` | `rate_limited`, `csrf`, `body_too_large`, `unauthenticated` |

### Case lifecycle

| Series | Labels | Meaning |
| --- | --- | --- |
| `cases_received_total` | `mode` | Issues accepted into `cases` |
| `eligibility_outcomes_total` | `mode`, `outcome` | `eligible` / `rejected` |
| `case_transitions_total` | `mode`, `to_state`, `actor` | Every state transition; `actor` is `worker`, `operator`, `github`, `slack`, `system` |
| `case_milestones_total` | `mode`, `milestone` | First time a case reaches a milestone (below) |
| `time_to_milestone_seconds` | `mode`, `milestone` | Histogram from `cases.created_at`: `triaged`, `first_pr`, `validated_pr` |

Milestones are deliberately distinct and never collapsed into "success":

| Milestone (`case_milestones_total`) | Recorded at state |
| --- | --- |
| `triage_session_completed` | `TRIAGED` — triage output accepted (a finished session alone records nothing) |
| `remediation_session_completed` | `OUTPUT_VALIDATING` — Devin reports the remediation session finished; no judgement of output yet |
| `structured_output_accepted_pr_discovered` | `PR_DISCOVERED` — output validated *and* a PR candidate corroborated by GitHub |
| `pr_validated` | `PR_VALIDATED` — scope, ancestry and the identical head probe all passed |
| `ci_passed` | `CI_PASSED` — at least one completed successful check run (all required ones when configured) |

Probe verdicts are a separate series (`probe_outcomes_total`) so "head probe
passed" is countable independently of "PR validated".

### Sessions, probes, CI

| Series | Labels | Meaning |
| --- | --- | --- |
| `devin_session_outcomes_total` | `mode`, `phase`, `outcome` | Devin session terminal results per `triage` / `remediation`: `finished`, `failed`, `human_blocked`, `timed_out`, `orphaned` (termination-pending and capacity waits are not outcomes) |
| `probe_outcomes_total` | `mode`, `target`, `outcome` | `base`/`head` × `matched`, `mismatched`, `infrastructure`, `busy` |
| `ci_outcomes_total` | `mode`, `outcome` | `success`, `failure`, `timed_out` |
| `retries_total` | `mode`, `kind` | `operator_retry` (operator re-queues a case), `provider_backoff` (Devin client retried a request) |
| `reconciliations_total` | `mode`, `kind`, `result` | e.g. `capacity_lease` × `expired` / `finished` |
| `capacity_denied_total` | `mode`, `kind` | Times a job parked for lack of a `triage`, `remediation`, `probe` or `resource` lease (a per-repository denial counts under `remediation`) |
| `worker_transient_db_errors_total` | – | Transient database/connection errors absorbed by the worker loop |

### Providers

| Series | Labels | Meaning |
| --- | --- | --- |
| `provider_requests_total` | `provider`, `method`, `status_class` | `devin`/`github`/`slack` × `2xx`, `3xx`, `4xx`, `429`, `5xx`, `error` |
| `provider_request_seconds` | `provider` | Latency histogram (50 ms – 30 s buckets) |
| `outbox_deliveries_total` | `channel`, `result` | Slack/GitHub outbox attempts |

### Gauges (refreshed from PostgreSQL on every scrape)

| Series | Labels | Meaning |
| --- | --- | --- |
| `case_queue_depth` | `mode`, `state` | Open cases per lifecycle state |
| `active_jobs` | `mode`, `kind` | Held capacity leases per kind |
| `capacity_limit` | `mode`, `kind` | Configured `MAX_CONCURRENT_*` |
| `capacity_utilisation_ratio` | `mode`, `kind` | `active_jobs / capacity_limit` |
| `cases_waiting_for_capacity` | `mode` | Cases with `waiting_for` set (parked on a full limit) |
| `cases_human_blocked` | `mode` | Cases in a `*HUMAN_BLOCKED` state |
| `outbox_backlog` | `mode`, `status` | Undelivered outbox rows per status |

Because gauges are computed from the database they agree across API and
worker processes; counters and histograms are per process and must be
scraped from each.

## Useful queries

```promql
# capacity saturation
capacity_utilisation_ratio{kind="remediation"}
# paid sessions that ended without an accepted PR (never "success")
sum(devin_session_outcomes_total{phase="remediation"}) - sum(case_milestones_total{milestone="pr_validated"})
# provider trouble
sum by (provider) (rate(provider_requests_total{status_class=~"429|5xx|error"}[5m]))
# humans needed
cases_human_blocked
```
