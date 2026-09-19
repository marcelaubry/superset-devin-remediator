# Threat model (Phase 3: bounded triage + human-gated GitHub dispatch)

Assets: the Devin service-user API key, the Slack bot token and signing secret,
the GitHub token, ACU spend, the operator dashboard, the integrity of triage
results, and the integrity of the human approval that gates remediation.

## Trust boundaries

```text
GitHub ──signed webhook──▶ API ──▶ PostgreSQL ◀── Worker ──▶ Devin (triage only)
Slack  ──signed action ──▶ API                     Worker ──▶ Slack (post/update)
                                                   Worker ──▶ GitHub (label/comment)
GitHub ──signed labeled webhook──▶ API  ⇒ REMEDIATION_APPROVED
```

Slack has no path to Devin. The API never performs outbound HTTP for Slack or
GitHub; the worker does, through the transactional outbox, and only to the
allowlisted repository / configured channel.

| Threat | Vector | Mitigation |
| --- | --- | --- |
| API key disclosure | logs, `repr(Settings)`, DB rows, API error bodies | `DEVIN_API_KEY` is a `SecretStr`; `LiveDevinClient.__repr__` omits it; `SecretRedactingFilter` scrubs log records and `_api_error` scrubs HTTP error text; nothing in `attempts`/`cases` stores credentials. Tests scan captured logs and every table for the test key. |
| Live calls without credentials | misconfigured deployment | `Settings` fails closed in live mode when key or org id is missing, base URL is not HTTPS, poll interval `< 10 s`, triage timeout `< 300 s` (the `.env.example` fake value would kill every real session), webhook/operator secrets are `change-me` or shorter than 16 chars. Live Slack/GitHub modes fail closed on missing or placeholder tokens/signing secret, empty channel/approver lists, or non-HTTPS API URLs. |
| Orphaned paid session | worker exception or lease loss mid-poll, operator cancel during polling | Attempts with a sent create are never marked terminal directly: worker errors and cancels route through `TERMINATION_PENDING` → final GET → `DELETE`; `fail_case` refuses to fail a case that may still own a session; the poll loop re-reads case state each iteration so a cancel terminates on the next poll. |
| Second paid session behind a live/unknown one | operator retry of a blocked or unresolved case | Retry terminates blocked attempts with a known session id first (DELETE failure blocks the retry); an `UNRESOLVED` create refuses a new `POST` until the operator confirms `confirm_no_session=true`, recorded on the attempt. |
| Secrets in the Docker image | `.env` copied into build context | `.dockerignore` excludes `.env`, `.env.*` (except `.env.example`), `.git`, and caches. |
| Automated tests spending ACU | test suite hits `api.devin.ai` | `tests/conftest.py` forces `DEVIN_CLIENT_MODE=fake` and drops `DEVIN_API_KEY`; live client tests use `httpx.MockTransport`. |
| Duplicate paid sessions | webhook redelivery, worker crash mid-create, uncertain transport result | Webhook delivery id is unique; `attempts.operation_key` is `UNIQUE` and a partial unique index allows one unfinished attempt per (case, kind); the operation key is the exact session tag; an uncertain `POST` is never retried — the runner lists sessions by tag and either attaches the single match or goes `HUMAN_BLOCKED`. |
| Unbounded spend per session | long-running or looping agent | `max_acu_limit=DEVIN_TRIAGE_MAX_ACU` on every create; absolute `timeout_at` on the attempt; final GET then `DELETE` on deadline; `TERMINATION_PENDING` keeps retrying termination so a paid session is never abandoned unobserved. |
| Replacement-session storm | waiting/unknown/suspended statuses interpreted as failure | `status.classify` maps waiting and resumable suspension to `HUMAN_BLOCKED` while keeping the session association; unknown values keep polling until the deadline; failed/timed-out/blocked attempts are never retried automatically. |
| Prompt injection from issue content | attacker-authored issue title/body/labels | Issue data is untrusted: rendered inside a per-attempt nonce fence, the template tells Devin to ignore instructions within it, and rendering refuses issue data that contains the delimiter. The stable protocol lives in the versioned `prompts/triage_v1.md`; job variables are limited to repository, base SHA, issue content, eligibility reasons, and correlation ids. |
| Devin acting outside triage | prompt asks for edits/PRs/comments | Prompt is read-only by contract; exactly one allowlisted repository at an exact base SHA is passed; remediation attempts are refused in live mode. |
| Forged or malformed triage results | agent returns arbitrary JSON | `structured_output_required=true` plus server-side Draft 7 validation (`additionalProperties: false`, enum-bound `outcome`); missing/malformed/unknown output fails the attempt — session completion alone is never success. |
| Wrong repository / moving target | stale ref, misrouted webhook | Repository must equal `GITHUB_REPOSITORY` before any HTTP; base SHA is resolved to a full 40-char commit via the GitHub commits API in live mode and pinned on the attempt. |
| Operator UI data exposure | dashboard shows raw output | Dashboard requires bearer token or signed-in cookie; it shows validated structured output, statuses, deadlines and reasons — never prompts, headers, or keys. |
| Slack/GitHub credential disclosure | logs, `repr`, DB, Slack/GitHub bodies, fixtures, image | `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `GITHUB_TOKEN` are `SecretStr`s in `Settings.secret_values`, scrubbed by the log filter; no table stores them; fixtures and `.env.example` hold placeholders only; `.dockerignore` excludes `.env*`; `test_phase3_units` scans tracked files, fixtures, and the built image context for credential-shaped strings. |
| Forged Slack action | attacker posts to `/webhooks/slack/actions` | Raw body is read first; timestamp must be within `SLACK_MAX_TIMESTAMP_SKEW_SECONDS`; `v0:{ts}:{body}` HMAC-SHA256 compared with `hmac.compare_digest`; body parsed only after success. Tests cover missing/invalid signature, stale and future timestamps, tampered body, and wrong secret. |
| Replayed Slack action | captured valid request re-sent | Timestamp window bounds replay; inside the window the `(approval_request, action_ts, user)` unique key makes the replay a `duplicate` no-op that returns the current state. |
| Spoofed approver | any workspace member clicks | User id must be in `SLACK_APPROVER_USER_IDS`; others get 403 and an `unauthorized_action` timeline event; the case is unchanged. Slack's own signature binds the user id to the request. |
| Payload-supplied identity | attacker edits repository/issue/case in the payload | Only the opaque token is read from the payload; its SHA-256 resolves the approval row, which owns the case. Tokens are 256-bit random, hashed at rest, and expire after `SLACK_ACTION_TOKEN_TTL_SECONDS` or on operator expiry (410). |
| Approval bypass | dashboard or API approves directly | No operator approve/reject route exists; `REMEDIATION_APPROVED` is written only by `confirm_label_webhook` from a signature-verified GitHub `issues/labeled` delivery whose label matches `GITHUB_REMEDIATION_LABEL` and whose case has a recorded `APPROVED` decision. A labeled webhook without an approval never advances the case. |
| Slack approval starts remediation | Slack → Devin | Approval writes a decision and an outbox row; the only consumer applies a GitHub label and comment. No Phase 3 path creates a `REMEDIATION` attempt; integration tests assert the Devin create count is unchanged. |
| Duplicate GitHub side effects | outbox retry after partial success, repeated clicks | Approval enqueues at most one `apply_remediation_label` row (unique `dedupe_key`); the worker re-reads the issue and skips the label if already present; `github_comment_id` prevents a second audit comment. |
| Stale approval | triage re-run between notification and label | The approval stores `triage_result_hash`; the worker compares it with the current attempt's output and refuses to label on mismatch (permanent outbox failure, visible as `last_error`). |
| Cross-repository writes | misconfiguration or forged issue metadata | Both GitHub clients enforce `GITHUB_REPOSITORY` before any request; the repository comes from the case row, never from Slack. |
| Untrusted content in Slack/GitHub | issue title, triage fields, rejection reason | Block Kit text is escaped (`&`, `<`, `>`) and truncated to Slack limits; raw issue body is never sent; rejection reasons come from a fixed select and are stripped of markup before rendering into Slack/GitHub. |
| Delivery failure loses a decision | Slack/GitHub outage | Human decisions are stored before any HTTP. Slack failures never touch the triage result; GitHub failures move the case to `APPROVAL_DELIVERY_FAILED` with the approval intact, and `POST /operator/outbox/{id}/retry` re-queues without re-approval. Retries are bounded (`OUTBOX_MAX_ATTEMPTS`, exponential backoff) and terminal failures are visible with `last_error`. |
| Fake-adapter data exposure | fake Slack messages contain tokens | `slack_fake_messages` is served only via the authenticated `/api/slack/fake/messages`; unauthenticated routes expose no Slack user ids, tokens, or case details. |

## Residual risks

- Devin session URLs and structured output are stored in PostgreSQL and shown
  to authenticated operators; treat the database as sensitive.
- Reconciliation depends on the list endpoint returning recently created
  sessions with their tags; if it lags beyond the bounded lookups the case
  parks in `HUMAN_BLOCKED` rather than risking a duplicate create.
- The Slack signing secret and bot token are long-lived; rotate them via the
  Slack app settings and restart. A GitHub App with hourly installation
  tokens is preferred over the take-home PAT for production.
- An authorized approver's account compromise still yields a valid approval;
  the audit comment and approval timeline make this attributable but not
  preventable here.
- Approval tokens live in Slack message blocks; anyone who can read the
  channel can obtain a token, but cannot use it without being an allowlisted
  approver and without a Slack-signed request.
- Phase 3 does not run remediation sessions, create PRs, or merge; those
  surfaces are out of scope until Phase 4.
