# Architecture

## Components

- **API:** FastAPI verifies signed GitHub issue deliveries, applies repository,
  event, action, and label filters, persists accepted payloads, and serves the
  authenticated operator UI.
- **Worker:** Claims pending deliveries and resumable case intents with
  PostgreSQL row locks, evaluates the zero-ACU deterministic eligibility
  filter, coordinates fake Devin sessions, and records every lifecycle change.
- **PostgreSQL:** The operational source of truth for webhook payloads, cases,
  attempts, append-only transitions, approval requests/events, Slack action
  dedupe records, and the transactional notification outbox.
- **Dashboard:** Jinja2 and HTMX pages for state counts, throughput, active
  work, case history, approval/delivery status, outbox attempts and errors,
  and operator retry/cancel/outbox-retry/token-expire actions. There is no
  dashboard approve/reject: the only decision surface is Slack.
- **Devin client (`remediator/devin`):** One `DevinClient` protocol with two
  implementations selected by `DEVIN_CLIENT_MODE`. `LiveDevinClient` calls the
  official Devin v3 API (`POST/GET/DELETE /v3/organizations/{org_id}/sessions…`)
  over httpx; `FakeDevinClient` emits API-shaped snapshots (same `status` /
  `status_detail` vocabulary) per deterministic scenario and never touches the
  network. Both share `status.classify`, the Draft 7 triage output schema, the
  versioned prompt template, and the tag helpers.
- **DevinRunner (`remediator/worker/devin_runner.py`):** The bounded session
  state machine: durable create intent → single create → reconcile-by-tag →
  poll until deadline → final GET → remote termination → schema validation.
- **Slack adapter (`remediator/slack`):** request signature verification
  (`v0:{ts}:{raw body}` HMAC-SHA256, replay window, constant-time compare),
  a Block Kit builder that escapes untrusted text and enforces Slack limits,
  and `SlackClient` implementations: `FakeSlackClient` persists messages in
  `slack_fake_messages`; `LiveSlackClient` calls `chat.postMessage` /
  `chat.update`.
- **GitHub client (`remediator/github/client.py`):** `FakeGitHubClient` and
  `LiveGitHubClient` (issues GET, labels POST, comments POST). Every call
  checks the repository allowlist before any network I/O.
- **Approval service (`remediator/approvals.py`):** creates approval requests
  after a valid `remediation_candidate` triage, processes verified Slack
  actions (token → expiry → approver → state → dedupe) in one transaction,
  and confirms delivery from the signed `issues/labeled` webhook.
- **Outbox dispatcher (`remediator/worker/outbox.py`):** claims outbox rows
  with leases, performs the Slack/GitHub call, applies bounded exponential
  backoff, and marks terminal failures visibly.

```mermaid
flowchart LR
  GH[GitHub issues] -->|signed webhook| API[FastAPI API]
  API --> DB[(PostgreSQL\noperational source of truth)]
  W[Worker / DevinRunner] --> DB
  W -->|DEVIN_CLIENT_MODE=live| L[Devin v3 API]
  W -->|DEVIN_CLIENT_MODE=fake| D[Fake Devin]
  W -->|base SHA| GHAPI[GitHub commits API]
  DB --> UI[Operator browser dashboard]
  DB --> O[Transactional outbox]
  W -->|dispatch| O
  O -->|chat.postMessage / chat.update| SL[Slack channel]
  SL -->|signed block_actions| API
  O -->|devin:remediate label + audit comment| GHAPI2[GitHub issues API]
  GH -->|signed issues/labeled webhook| API
  GH -. audit ledger .- UI
```

Slack has no path to Devin: the API records the decision, the worker talks
only to GitHub, and the case advances only on GitHub's own webhook.

GitHub remains the audit ledger for issue history and eventual comments or
pull requests. PostgreSQL is the operational source of truth for work state.

## Phase 2 triage flow

```text
GitHub issue opened
→ deterministic zero-ACU eligibility filter        (rubric.py, no Devin call)
→ eligible issue automatically queues Devin triage (TRIAGE_CREATE_INTENT)
→ durable create intent                            (attempts row + unique operation_key, committed BEFORE POST)
→ bounded Devin session                            (max_acu_limit, absolute timeout_at, exact op tag)
→ schema-validated triage result                   (structured_output_required + Draft 7 validation)
→ awaiting remediation approval                    (AWAITING_REMEDIATION_APPROVAL)
```

## Phase 3 approval flow

```text
validated remediation_candidate                    (worker _finish_triage)
→ approval_requests row + slack outbox row         (same transaction as the triage result)
→ worker posts Block Kit message                   (fake or live; failure never touches the triage result)
→ token generated, sha256 stored, channel/ts stored
→ POST /webhooks/slack/actions                     (raw body → timestamp window → HMAC → parse)
   approve: decision recorded (actor, time, action id, triage hash, label op)
            + outbox apply_remediation_label + slack update
   reject:  REMEDIATION_REJECTED + outbox rejection_comment + slack update
→ worker apply_remediation_label                   (issue exists? allowlisted? triage hash current?
                                                    label idempotent → applied once → audit comment)
→ GitHub issues/labeled webhook (signed)           → REMEDIATION_APPROVED
```

Design points:

- **Only candidates notify.** `approval_requests` is created only when the
  triage output validated against the schema and `outcome ==
  remediation_candidate`; a unique constraint on `case_id` prevents a second
  notification for the same case.
- **Opaque tokens.** The button value is `secrets.token_urlsafe(32)`; only its
  SHA-256 and expiry are stored. Repository, issue number, and case id are
  resolved from the token row, never from the Slack payload.
- **Dedupe.** `slack_actions` has a unique `(approval_request_id, action_ts,
  slack_user_id)` key; a repeated click returns `duplicate` and the current
  state. A click after a decision returns `already_decided`.
- **Fast acknowledgement.** The Slack endpoint does one short transaction and
  returns; Slack HTTP (message updates) and GitHub HTTP run in the worker.
- **GitHub is the authority.** After the worker applies the label the
  approval request shows `LABEL_APPLIED`, but the case stays in
  `AWAITING_REMEDIATION_APPROVAL` (or `APPROVAL_DELIVERY_FAILED` after a retry)
  until the signed `issues/labeled` webhook for `GITHUB_REMEDIATION_LABEL`
  arrives; then `confirm_label_webhook` moves it to `REMEDIATION_APPROVED`.
  A `labeled` webhook for a case without a recorded `APPROVED` decision is
  treated as an ordinary ingest and does not advance the case.
- **Failure isolation.** Slack post failures leave the triage result and
  case untouched; after `OUTBOX_MAX_ATTEMPTS` the row is `FAILED` with
  `last_error`. GitHub label failures keep the approval decision and move the
  case to `APPROVAL_DELIVERY_FAILED`; `POST /operator/outbox/{id}/retry`
  re-queues the same row and never requires a second human decision.
- **Nothing calls Devin.** No Phase 3 code path creates a `REMEDIATION`
  attempt or reaches `REMEDIATION_CREATE_INTENT`; the integration tests assert
  the fake Devin create count is unchanged across approval.

### Create intent and spend-boundary idempotency

The Devin create endpoint has no idempotency key, so the runner makes the
boundary durable on our side:

1. Insert the `attempts` row with `operation_key = op:<case>:<kind>:<n>`
   (`UNIQUE`) and `create_state = PENDING`; a partial unique index also
   enforces one unfinished attempt per `(case, kind)`. Commit.
2. Validate the request (allowlisted repository, resolvable base SHA, prompt
   renders). Any failure → `create_state = NOT_SENT`, attempt `FAILED`, no HTTP.
3. `POST /sessions` exactly once with the operation key as the first tag plus
   correlation tags (`repo:`, `issue:`, `kind:`, `case:`, `attempt:`).
   - Definitive 4xx/5xx → `API_ERROR`, attempt `FAILED`, case `FAILED`.
   - Transport failure/timeout → `UNCERTAIN`, case `RECONCILING_CREATE`.
4. Reconcile by listing sessions and matching the exact operation tag
   client-side (bounded lookups with backoff). Exactly one match →
   `RECONCILED` and polling continues on that session. No match after the
   bounded lookups, or more than one → `UNRESOLVED`, case `HUMAN_BLOCKED`.
   A second POST is never issued automatically.

### Polling and status mapping (`remediator/devin/status.py`)

| Remote `status` | `status_detail` | Disposition |
| --- | --- | --- |
| `new`, `claimed`, `running`, `resuming` | working / other | keep polling |
| any live status | `finished` | validate structured output |
| any live status | `waiting_for_user`, `waiting_for_approval` | `HUMAN_BLOCKED`, session URL retained, no replacement |
| `exit` | `finished` | validate structured output |
| `exit`, `error` | anything else | attempt `FAILED` |
| `suspended` | usage/credit/quota/billing/limit | attempt `FAILED` (terminal) |
| `suspended` | other (inactivity, user request) | `HUMAN_BLOCKED` |
| unknown | unknown | attempt `RECONCILING`, keep polling until deadline |

Session completion alone is never success: the `structured_output` must exist
and validate against `TRIAGE_OUTPUT_SCHEMA`, and `outcome` must be one of
`remediation_candidate`, `needs_human`, `no_change_needed`,
`deterministic_automation`, `invalid_issue`. Only `remediation_candidate`
advances to `AWAITING_REMEDIATION_APPROVAL`; every other outcome ends at
`POLICY_REJECTED` with a `triage_not_feasible` GitHub outbox intent carrying
the summary and blocking questions.

### Timeout and termination

When `now >= attempt.timeout_at` the runner performs one final `GET`. If the
session finished in the polling gap it is processed normally (no false
timeout). Otherwise it calls `DELETE /sessions/{id}`. Success → attempt and
case `TIMED_OUT`. Failure → case `TERMINATION_PENDING`; the worker reclaims it
after lease expiry and retries the final-GET/DELETE cycle until termination is
confirmed. Local polling therefore never stops while a paid session may still
be running unobserved.

The same final-GET/DELETE path handles every other way a live session can
lose its local owner:

- **Worker error.** An unexpected exception while an attempt has a sent
  create (`create_sent_at` set) parks the attempt and case in
  `TERMINATION_PENDING` with a `worker error: …` reason instead of `FAILED`;
  `fail_case` refuses to make such a case terminal until the remote session is
  confirmed gone. Lease loss / concurrent transitions cancel only attempts
  whose create was never sent.
- **Operator cancel.** Cancelling a case in `TRIAGING`, `REMEDIATING`, or any
  state whose active/blocked attempt may own a session moves it to
  `TERMINATION_PENDING`. The poll loop re-reads the case state every
  iteration, so a cancel during polling terminates the session on the next
  poll rather than at the deadline. A `PENDING` create is terminated by exact
  tag lookup.
- **Retry.** Before a new create, earlier blocked attempts with a known
  session id are `DELETE`d (failure blocks the retry). An `UNRESOLVED` create
  never permits a second `POST` until the operator retries with
  `confirm_no_session=true`, which records the acknowledgement on the attempt.

The deadline `timeout_at` is anchored when the session is attached (after the
`POST` returns or the tag reconciliation succeeds), not before create latency.

### Restart recovery

All runner state lives in the `attempts` row (session id, deadline, poll
counters, create state). The worker claims cases in `TRIAGE_CREATE_INTENT`,
`TRIAGING`, `RECONCILING_CREATE`, and `TERMINATION_PENDING` whose lease has
expired and resumes from the persisted attempt: a `PENDING` create is
reconciled by tag rather than re-sent, a `CREATED` session is polled, a
pending termination is retried.

## Lifecycle

The eligibility filter runs without ACU cost. Eligible cases go directly to
triage; there is no approval or notification before triage. A
`remediation_candidate` verdict creates one approval request and one Slack
outbox row; an authorized human approves or rejects from Slack, and the case
reaches `REMEDIATION_APPROVED` only through GitHub's signed `labeled` webhook.
`REMEDIATION_APPROVED → REMEDIATION_CREATE_INTENT` exists in the transition
table for Phase 4 but nothing in this release performs it.

```mermaid
stateDiagram-v2
  [*] --> RECEIVED
  RECEIVED --> ELIGIBILITY_EVALUATED
  ELIGIBILITY_EVALUATED --> POLICY_REJECTED
  ELIGIBILITY_EVALUATED --> TRIAGE_CREATE_INTENT
  TRIAGE_CREATE_INTENT --> TRIAGING
  TRIAGE_CREATE_INTENT --> RECONCILING_CREATE
  TRIAGE_CREATE_INTENT --> FAILED
  TRIAGE_CREATE_INTENT --> CANCELLED
  TRIAGE_CREATE_INTENT --> TERMINATION_PENDING
  TRIAGING --> TRIAGED
  TRIAGING --> FAILED
  TRIAGING --> HUMAN_BLOCKED
  TRIAGING --> TIMED_OUT
  TRIAGED --> POLICY_REJECTED
  TRIAGED --> AWAITING_REMEDIATION_APPROVAL
  TRIAGED --> FAILED
  TRIAGED --> CANCELLED
  TRIAGED --> TERMINATION_PENDING
  AWAITING_REMEDIATION_APPROVAL --> REMEDIATION_APPROVED: signed labeled webhook
  AWAITING_REMEDIATION_APPROVAL --> REMEDIATION_REJECTED: Slack reject
  AWAITING_REMEDIATION_APPROVAL --> APPROVAL_DELIVERY_FAILED: label outbox exhausted
  AWAITING_REMEDIATION_APPROVAL --> CANCELLED
  AWAITING_REMEDIATION_APPROVAL --> FAILED
  APPROVAL_DELIVERY_FAILED --> REMEDIATION_APPROVED: retry + signed labeled webhook
  APPROVAL_DELIVERY_FAILED --> CANCELLED
  APPROVAL_DELIVERY_FAILED --> FAILED
  APPROVAL_DELIVERY_FAILED --> TERMINATION_PENDING
  REMEDIATION_APPROVED --> REMEDIATION_CREATE_INTENT: Phase 4
  REMEDIATION_APPROVED --> CANCELLED
  REMEDIATION_APPROVED --> FAILED
  REMEDIATION_REJECTED --> [*]
  REMEDIATION_CREATE_INTENT --> REMEDIATING
  REMEDIATION_CREATE_INTENT --> RECONCILING_CREATE
  REMEDIATION_CREATE_INTENT --> FAILED
  REMEDIATION_CREATE_INTENT --> CANCELLED
  REMEDIATION_CREATE_INTENT --> TERMINATION_PENDING
  REMEDIATING --> OUTPUT_VALIDATING
  REMEDIATING --> FAILED
  REMEDIATING --> HUMAN_BLOCKED
  REMEDIATING --> TIMED_OUT
  OUTPUT_VALIDATING --> PR_VALIDATED
  OUTPUT_VALIDATING --> FAILED
  OUTPUT_VALIDATING --> CANCELLED
  OUTPUT_VALIDATING --> TERMINATION_PENDING
  PR_VALIDATED --> CI_PENDING
  PR_VALIDATED --> FAILED
  PR_VALIDATED --> CANCELLED
  PR_VALIDATED --> TERMINATION_PENDING
  CI_PENDING --> CI_PASSED
  CI_PENDING --> FAILED
  CI_PENDING --> CANCELLED
  CI_PENDING --> TERMINATION_PENDING
  HUMAN_BLOCKED --> RECEIVED
  HUMAN_BLOCKED --> CANCELLED
  HUMAN_BLOCKED --> FAILED
  HUMAN_BLOCKED --> TERMINATION_PENDING
  FAILED --> RECEIVED
  TIMED_OUT --> RECEIVED
  TERMINATION_PENDING --> CANCELLED
  TERMINATION_PENDING --> FAILED
  RECEIVED --> CANCELLED
  RECEIVED --> FAILED
  RECEIVED --> TERMINATION_PENDING
  TRIAGING --> CANCELLED
  TRIAGING --> TERMINATION_PENDING
  REMEDIATING --> CANCELLED
  REMEDIATING --> TERMINATION_PENDING
  AWAITING_REMEDIATION_APPROVAL --> TERMINATION_PENDING
  RECONCILING_CREATE --> TRIAGING
  RECONCILING_CREATE --> REMEDIATING
  RECONCILING_CREATE --> HUMAN_BLOCKED
  RECONCILING_CREATE --> FAILED
  RECONCILING_CREATE --> CANCELLED
  RECONCILING_CREATE --> TERMINATION_PENDING
  TERMINATION_PENDING --> TIMED_OUT
  TERMINATION_PENDING --> TRIAGED
  TERMINATION_PENDING --> HUMAN_BLOCKED
```

`TRIAGE_CREATE_INTENT` and `REMEDIATION_CREATE_INTENT` make external session
creation resumable. Triage infeasibility transitions to `POLICY_REJECTED`,
records a GitHub notification intent, and creates no remediation attempt.
Retries from `FAILED` or `TIMED_OUT` normally return to eligibility, while a
case whose latest attempt is a failed remediation after successful triage
returns directly to `REMEDIATION_CREATE_INTENT`.

## Data model

| Table | Key columns | Purpose |
| --- | --- | --- |
| `webhook_events` | delivery id, payload, status, case id | Verbatim accepted webhook deliveries and processing lease |
| `cases` | issue identity, state, recommendation, Devin/PR fields | Current operational case record |
| `attempts` | case, kind, `operation_key` (unique), `create_state`, Devin session id/url/tags/status/detail, ACU telemetry, `base_sha`, `prompt_version`, poll timestamps, `timeout_at`, validated `structured_output`, `reconciliation_reason` | Durable create intent and bounded session audit; partial unique index = one unfinished attempt per (case, kind) |
| `state_transitions` | case, from/to state, reason, actor | Append-only lifecycle audit |
| `notification_outbox` | case, channel, kind, payload, status, `attempts_count`, `next_attempt_at`, `last_error`, lease, `dedupe_key`, `terminal_failure` | Transactional outbox dispatched by the worker with bounded backoff |
| `approval_requests` | case (unique), triage attempt, `triage_result_hash`, `action_token_hash` + expiry, Slack channel/ts, notification status, decision/actor/time/action id/reason, label operation, delivery status, GitHub comment id | One approval per case; authoritative record of the human decision |
| `slack_actions` | approval request, `action_ts`, Slack user, action id, outcome | Unique dedupe key and append-only audit of every verified click |
| `approval_events` | approval request, kind, actor, detail | Append-only approval timeline shown in the dashboard |
| `slack_fake_messages` | channel, ts, text, blocks | Fake adapter store, readable only via the authenticated operator API |

## Webhook request path

1. Read the raw request body.
2. Verify `X-Hub-Signature-256` with HMAC-SHA256 before parsing JSON.
3. Require the delivery and event headers, parse JSON, and apply repository,
   event, action, and required-label filters.
4. Persist accepted payloads as `PENDING` using the unique delivery id.
5. Return `202`; duplicate deliveries are acknowledged as deduplicated.

Filtered requests are acknowledged but never persisted, and the API performs no
worker processing inline.

`issues/labeled` deliveries are additionally routed to
`approvals.confirm_label_webhook` when the label equals
`GITHUB_REMEDIATION_LABEL`; every other labeled delivery is a normal ingest.

### Slack request path (`POST /webhooks/slack/actions`)

1. Read the raw body.
2. Require `X-Slack-Request-Timestamp`; reject non-integer or
   `|now - ts| > SLACK_MAX_TIMESTAMP_SKEW_SECONDS` (401).
3. Compute `v0={hex(hmac_sha256(secret, "v0:" + ts + ":" + body))}` and
   compare with `X-Slack-Signature` via `hmac.compare_digest` (401).
4. Only then `application/x-www-form-urlencoded` → `payload` JSON →
   `block_actions`. Non-decision actions (the reason select) are acknowledged
   with `200 {"ok": true, "outcome": "ignored"}`.
5. Look up the token hash (404 unknown, 410 expired), the approver allowlist
   (403, audited), the case state (409), and the dedupe key.
6. Record the decision and outbox rows in one transaction and return the
   current state.

## Worker claiming and failure isolation

The worker first claims one `PENDING` webhook event, or reclaims a
`PROCESSING` event whose lease expired. It then claims cases in
`RECEIVED`, `TRIAGE_CREATE_INTENT`, `REMEDIATION_CREATE_INTENT`, or
`TERMINATION_PENDING` when their case lease is free or expired and no related
event is pending (or processing with an unexpired lease). Each claim uses
`SELECT ... FOR UPDATE SKIP LOCKED`, records a worker id and lease, and
commits before processing. This provides queue-like concurrency without
introducing Redis or another queue service.

State writes are guarded by `UPDATE ... WHERE state = expected_state` and the
current worker owner; a concurrent operator or worker therefore cannot
overwrite a newer state or a stolen lease. A heartbeat renews each case and
event lease at roughly one third of the lease duration.
Devin
CREATE_INTENT attempts have durable idempotency keys and are reconciled after
a crash, with a bounded second lookup before declaring reconciliation failure.
If a create returns after ownership was lost, the session is terminated and
the attempt is recorded as orphaned. A lease is released after success,
failure, or a concurrent-change abandonment. If a process stops, in-flight
jobs are drained for up to the configured shutdown timeout; anything longer is
reclaimed after lease expiry. Docker's worker stop grace period exceeds that
timeout.

Webhook and case processing are isolated in their own transactions. Exceptions
mark the event or case failed and are logged; the loop continues to process
other work. Invalid concurrent transitions are logged at INFO and do not fail
the case. On SIGTERM or SIGINT, loops stop after their current job, in-flight
tasks are drained up to the configured timeout, then cancelled if necessary;
the Devin client closes and the database engine is disposed.

## Security boundaries

- Signature verification occurs before JSON parsing.
- HMAC signatures and operator tokens use constant-time comparisons.
- Operator pages require a bearer token or signed-in cookie; API-like requests
  receive `401`, while browser pages redirect to `/login`.
- Secrets are environment values and are not committed to the repository.
  `DEVIN_API_KEY` is a `SecretStr`: it is excluded from `repr`, redacted from
  API error messages and log records, and never written to the database.
- Live mode fails closed: missing key/org, non-HTTPS base URL, or poll
  interval below 10 s refuse to start. `SLACK_CLIENT_MODE=live` and
  `GITHUB_CLIENT_MODE=live` likewise refuse missing/placeholder tokens and
  non-HTTPS API URLs. `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, and
  `GITHUB_TOKEN` are `SecretStr`s covered by the same log redaction filter.
- Issue title/body/labels are untrusted data inside the Devin prompt, fenced
  by a per-attempt nonce delimiter; the prompt instructs Devin to ignore any
  instructions inside the fence. See [threat-model.md](threat-model.md).
- Slack payloads are never trusted for identity: repository, issue, and case
  come from the token row; the approver id must be in
  `SLACK_APPROVER_USER_IDS`. Slack user ids appear only behind operator auth.
- Slack messages and GitHub comments contain escaped, length-capped triage
  fields; never the raw issue body, tokens, or secrets. The optional rejection
  reason is a fixed vocabulary and is stripped of markup before rendering.
- Fake-adapter state (`slack_fake_messages`) is exposed only through the
  authenticated `/api/slack/fake/messages` route.
- Accepted webhook payloads are stored verbatim; deployments should consider
  retention and possible personal information in issue bodies.

## API assumptions verified against current documentation

- Slack request signing: `v0:{timestamp}:{raw body}` HMAC-SHA256, hex digest
  prefixed with `v0=`, in `X-Slack-Signature`; Slack recommends a 5-minute
  replay window. Matches the spec.
- Slack interactivity: `block_actions` payloads arrive as
  `application/x-www-form-urlencoded` with a single `payload` field. A
  `static_select` in the message is echoed under `state.values[block_id]
  [action_id].selected_option.value` on the subsequent button click, which is
  how the optional rejection reason is collected without a modal. Selecting a
  reason also triggers its own `block_actions` request, which the endpoint
  acknowledges and ignores.
- GitHub `POST /repos/{owner}/{repo}/issues/{n}/labels` creates missing
  labels and is a no-op for labels already present; the client still checks
  the issue's current labels first so `applied` is reported truthfully.
- GitHub sends `issues` `labeled` events with `payload.label.name`; the
  webhook signature is `X-Hub-Signature-256`.

## Deferred to Phase 4

- **Remediation sessions:** consume `REMEDIATION_APPROVED` cases; live mode
  still refuses `REMEDIATION` attempts.
- **GitHub App writes:** branches, pull requests, and statuses (comments and
  labels are implemented here).
- **Stage deadline reconciliation:** apply longer-lived stage deadlines and operational escalation to `TIMED_OUT`.
- **CI webhook ingestion:** advance `CI_PENDING` from GitHub CI events.
- **Retention and cleanup:** expire old payloads, attempts, and notification records.
