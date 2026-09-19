# Threat model (Phase 2: live, bounded Devin triage)

Assets: the Devin service-user API key, ACU spend, the operator dashboard, and
the integrity of triage results that later gate remediation approval.

| Threat | Vector | Mitigation |
| --- | --- | --- |
| API key disclosure | logs, `repr(Settings)`, DB rows, API error bodies | `DEVIN_API_KEY` is a `SecretStr`; `LiveDevinClient.__repr__` omits it; `SecretRedactingFilter` scrubs log records and `_api_error` scrubs HTTP error text; nothing in `attempts`/`cases` stores credentials. Tests scan captured logs and every table for the test key. |
| Live calls without credentials | misconfigured deployment | `Settings` fails closed in live mode when key or org id is missing, base URL is not HTTPS, poll interval `< 10 s`, triage timeout `< 300 s` (the `.env.example` fake value would kill every real session), webhook/operator secrets are `change-me` or shorter than 16 chars, or auto-approve is enabled. |
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

## Residual risks

- Devin session URLs and structured output are stored in PostgreSQL and shown
  to authenticated operators; treat the database as sensitive.
- Reconciliation depends on the list endpoint returning recently created
  sessions with their tags; if it lags beyond the bounded lookups the case
  parks in `HUMAN_BLOCKED` rather than risking a duplicate create.
- Phase 2 does not dispatch outbox rows, create PRs, or run remediation
  sessions; those surfaces are out of scope until Phase 3.
