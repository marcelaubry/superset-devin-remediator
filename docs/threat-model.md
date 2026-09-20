# Threat model (Phase 5: bounded triage, human-gated dispatch, isolated verification)

Assets: the Devin service-user API key, the Slack bot token and signing secret,
the GitHub token, the database credentials, ACU spend, the operator dashboard,
the integrity of triage results, the integrity of the human approval that
gates remediation, the integrity of the immutable probe (the only independent
evidence that a PR fixes the issue), and the allowlisted repository's branches
and settings.

## Trust boundaries

```text
GitHub ──signed webhook──▶ API ──▶ PostgreSQL ◀── Worker ──▶ Devin (triage + remediation)
Slack  ──signed action ──▶ API                     Worker ──▶ Slack (post/update)
                                                   Worker ──▶ GitHub (label/comment, read pulls/
                                                             files/compare/timeline/check-runs)
GitHub ──signed labeled webhook──▶ API  ⇒ REMEDIATION_APPROVED
probes/ (git-tracked, reviewed) ──▶ Worker snapshot (hashes only)
                                        │ HMAC-signed verifier.v2 request:
                                        │ repo, 40-hex SHA, probe id, script+manifest hash
                                        ▼
                                   Verifier container (no credentials, ro root, no caps)
                                        │ loads probe from its own ro registry mount,
                                        │ refuses on hash mismatch
                                        ▼
                                   anonymous clone of the allowlisted repo @ exact SHA
```

The verifier boundary (Phase 5):

| Side | Holds | Never sees |
| --- | --- | --- |
| API / worker | every provider credential, database URL, operator token, the verifier HMAC key | repository code; there is no local probe runner |
| `verifier` (front, UID 65533) | the HMAC key (via Docker secret file), the read-only probe registry | repository code, `.env`, the Docker socket, any provider credential, the database network, the internet |
| `verifier-runner` (UID 65534) | the read-only probe registry, a disk-backed workspace, a route to `egress-proxy` | **any secret at all, the HMAC key included**; `.env`; the Docker socket; the worker network; the internet directly |
| `egress-proxy` (UID 65532) | an exact hostname allowlist | everything else |

The HMAC key is the single shared value between worker and front. It
authenticates *who may ask* the verifier to run an already-registered probe
and grants nothing else. Because the key never reaches the runner, a probe or
test suite compromised by a malicious commit cannot read it from `os.environ`,
`/proc`, or `/run/secrets` and therefore cannot forge, replay or self-approve
a verdict for another SHA; it can only lie about the run it is already in.
The worker treats the verifier's answer as evidence, not authority: it checks
the request id, repository, SHA, script hash and command identity of the
response and refuses (infrastructure failure, never a pass) a verifier whose
signed `/capabilities` show `execution != runner`, a credential, UID 0,
writable root, missing tool, `direct_egress` true or unverified, or — with
`PROBE_VERIFIER_REQUIRE_ISOLATION` (forced in live mode) — missing
`no-new-privileges`, non-empty capability sets or absent cgroup PID/memory
limits. Docker Desktop hosts that cannot present those cgroup facts therefore
fail closed rather than being trusted with a weaker sandbox.

Network egress from the runner is required (dependencies are installed from
registries by declared `setup` steps) and is *not* credential-bearing. It is
enforced by topology: the runner's only network is `internal: true` (no
gateway), so `git fetch` and `npm ci` go through `egress-proxy`, which admits
`CONNECT` to exact allowlisted hosts on :443 only (`EGRESS_ALLOWED_HOSTS`).
The runner attempts a bounded direct connection to a public host on every
capability read and reports the outcome as `direct_egress`; readiness and the
worker fail when it succeeds. A compromised probe can still push the
repository contents it already has to any allowlisted host (e.g. another
GitHub repository or an npm package it controls), and package lifecycle
scripts execute as the runner UID with the same access a malicious commit
already has; approved manifests use `--ignore-scripts`, which reduces but
does not remove that surface.

Slack has no path to Devin. The API never performs outbound HTTP for Slack or
GitHub; the worker does, through the transactional outbox, and only to the
allowlisted repository / configured channel. Devin never talks to the
remediator: its session output is read back and treated as a claim to be
corroborated by GitHub and by the probe.

The probe runner executes repository code (the probe script plus whatever the
checked-out commit's test suite does), so the process that hosts it must hold
no credentials. It lives only in the verifier container; remaining gaps are in
[known-limitations.md](known-limitations.md).

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
| Spoofed approver | any workspace member clicks | User id must be in `SLACK_APPROVER_USER_IDS`; others are acknowledged with `200 {"ok": false, "outcome": "unauthorized"}` (Slack renders non-2xx as a generic error), receive an ephemeral explanation via `response_url`, and leave an `unauthorized_action` timeline event; the case is unchanged. Slack's own signature binds the user id to the request. |
| Payload-supplied identity | attacker edits repository/issue/case in the payload | Only the opaque token is read from the payload; its SHA-256 resolves the approval row, which owns the case. Tokens are 256-bit random, hashed at rest, and expire after `SLACK_ACTION_TOKEN_TTL_SECONDS` or on operator expiry. On approval, rejection, expiry or supersession the stored hash is rotated/nulled so a leaked token can never again resolve to a decidable request. |
| Remediation without a matching approval | forged/replayed `devin:remediate` label, approval of an older triage, label on another issue | The pipeline re-reads the approval row (`decision=APPROVED`) and requires `approval.triage_result_hash == sha256(latest validated triage output)`; a newer triage attempt supersedes the approval; the label is re-read from GitHub on the exact issue; the webhook delivery id is unique. Any failure is `REMEDIATION_FAILED`/`REMEDIATION_HUMAN_BLOCKED` before a create intent exists (0 ACU). |
| Paying for a defect that is not reproducible | probe missing, altered, or already passing at base | The probe manifest is validated (repo, issue, 40-hex base SHA, script hash recomputed) and snapshotted; the snapshot runs at the pinned base *before* `POST /sessions`. Only the declared failing code permits spend; passing base is `no_change_needed`; missing runtime is `PROBE_INFRASTRUCTURE_BLOCKED`. |
| Probe supplied by an attacker | probe command in issue text, Slack, or Devin structured output | Probes are loaded only from the git-tracked registry under `PROBE_ROOT`, keyed by repository and issue number; no other source is consulted. Devin's `probe_identifier`/`probe_hash` are compared against the snapshot, never used to locate a script. The PR validator rejects any change under `probes/`. |
| Devin-reported success trusted as evidence | `outcome=pr_created`, PR URL in chat, Devin-run probe output | Candidates come from `pull_requests[]` only; GitHub must corroborate repo, state, base, head ref/SHA, ancestry, author and closing reference; the remediator runs the identical snapshot at head itself. Structured output or PR URL alone never advances past `PR_DISCOVERED`. |
| PR linked to the wrong issue / repo / branch | `Closes #10` vs `#1`, fork PR, PR against a release branch, merged PR | Timeline cross-reference from the exact issue, then exact `owner/repo#N` token parse (word-bounded); repository must be allowlisted; `base.ref == GITHUB_BASE_REF`; `merged=false`; head ref must start with the configured prefix; `compare` must show the head ahead of the pinned base. |
| Scope expansion / privileged file changes | PR edits workflows, CODEOWNERS, repo settings, the probe | Changed files are listed from GitHub (not from Devin's `changed_files`); `.github/`, `probes/`, `CODEOWNERS`, `SECURITY.md`, `.pre-commit-config.yaml`, `setup.cfg`, `pyproject.toml` fail closed; undeclared files or more than `REMEDIATION_MAX_CHANGED_FILES` are human-blocked. |
| Absent CI mistaken for green | repo with no checks, checks not yet queued | `evaluate_checks` returns `absent` for zero check runs and `pending` for incomplete ones; `CI_PASSED` requires at least one completed successful check (all named in `GITHUB_REQUIRED_CHECKS` when set); the poll is bounded by `CI_TIMEOUT_SECONDS` and expires to `CI_FAILED (timed_out)`. |
| Automatic fix / merge loop | red probe at head, red CI | The pipeline never re-prompts Devin, never merges, never closes the issue; `REMEDIATION_FAILED`/`CI_FAILED` require an authenticated operator to start a new attempt. |
| Probe subprocess reads worker credentials | probe or test suite at the checked-out commit reads `os.environ`, `/proc/<pid>/environ`, mounted secret files, the Docker socket, or reaches the database / Devin API over the network | **A scrubbed child environment is not a boundary** inside the credential-bearing worker (same UID, `/proc`, mounted files, network). The worker therefore has no local execution path at all: `build_probe_runner` offers only `fake` and `remote`, and `Settings` rejects `local`. Repository code runs only in the `verifier` container (`docker/verifier/Dockerfile`): no `env_file`, never imports `remediator.config`/pydantic-settings so no `.env` can be read, dedicated UID 65534, read-only root, bounded `/tmp` tmpfs, `cap_drop: ALL`, `no-new-privileges`, PID and memory limits, its own network with no route to PostgreSQL. The boundary is checked at runtime, not declared: `GET /health` reports the verifier's credential exposure, UID, root writability and toolset; `RemoteProbeRunner` refuses (infrastructure failure, never a verdict) if any credential-shaped variable or secret path is visible, the UID is 0, the root is writable or the protocol version differs, and the verifier's `LocalProbeRunner` re-checks its own environment before every run. Tests cover secrets loaded from `.env` into `Settings` (the worker still cannot run a probe locally), a verifier reporting credentials, root or a writable root, and that importing the verifier never imports `Settings`. |
| Probe escapes limits | infinite loop, output flood, child closes stdout/stderr and keeps running, `setsid`/double-fork escapes the process group, disk/PID/memory exhaustion | The deadline (`min(manifest.timeout, PROBE_TIMEOUT_SECONDS)`, capped again by `VERIFIER_MAX_TIMEOUT_SECONDS`) covers `proc.wait()` as well as both pipes, so closing descriptors does not extend it; on expiry `killpg` plus a `/proc` sweep for every process still carrying the per-run `REMEDIATOR_PROBE_RUN=<uuid>` marker (also run after every normal exit, before the workspace is removed); rlimits (`RLIMIT_NPROC`, `RLIMIT_FSIZE`, optional `RLIMIT_AS`) are inherited by all descendants; the container adds `pids_limit`, `mem_limit`/`memswap_limit` and a size-bounded tmpfs so a probe cannot exhaust the host; per-stream `PROBE_MAX_OUTPUT_BYTES` capture with `output_truncated` recorded. Probes are serialized per verifier (`409` while busy → transient worker retry) and after each run every remaining process of the probe UID is SIGKILLed, so a descendant that clears its environment *and* escapes the process group still dies with its own probe; `init: true` reaps orphans so they cannot pile up as zombies against `pids_limit`. Residual: the escapee lives for the duration of its own probe only; the container, not the worker, is the blast radius. |
| Wrong commit verified | branch moved after validation, base drifted | Base and head are always addressed by full SHA (`git fetch --depth 1 origin <sha>` + detached checkout); the head SHA verified by the probe is the one whose check runs are polled and the one displayed as "Ready for human review". |
| Fake evidence in live mode | `PROBE_RUNNER_MODE=fake` or `GITHUB_CLIENT_MODE=fake` with real Devin | `Settings` fails closed: live Devin requires live GitHub, `PROBE_RUNNER_MODE=remote` with a `PROBE_VERIFIER_URL`, existing `PROBE_ROOT`, remediation timeout ≥ 600 s and CI poll interval ≥ 30 s; the verifier is then re-verified via `/health` before every probe. |
| Deadlock between worker loops corrupts case truth | two loops at `WORKER_CONCURRENCY=2` touching one case's outbox rows and case row in opposite orders; the loser retries through a poisoned session | Single lock order everywhere (case → approval request → outbox row, `FOR NO KEY UPDATE` so FK inserts are not blocked): the dispatcher locks the case first, the processor only inserts outbox rows. `DeadlockDetected`/serialization/connection errors are classified transient: the session is rolled back, the lease released, and the job retried (bounded by `EVENT_MAX_ATTEMPTS`); the case is never marked FAILED from a poisoned session. |
| Two retries share an operation key / Devin tag | concurrent operator retries allocate the same ordinal | Ordinals are allocated under the case row lock (`allocate_attempt_ordinal`), backed by the partial unique index `(case_id, kind, ordinal)` and the unique `operation_key`; the key embeds the *full* triage hash and *full* base SHA and is used verbatim as the Devin tag (no truncation anywhere). |
| Second paid remediation | concurrent operator retries, retry while a session is live | Retries are only allowed from `REMEDIATION_FAILED`/`TIMED_OUT`/`HUMAN_BLOCKED`; the case row is locked `FOR UPDATE`; the partial unique index allows one unfinished `REMEDIATION` attempt; a blocked attempt with a known session is terminated first; the operation key embeds the triage hash, base SHA and attempt ordinal so it can never collide with an earlier tag. |
| Approval bypass | dashboard or API approves directly | No operator approve/reject route exists; `REMEDIATION_APPROVED` is written only by `confirm_label_webhook` from a signature-verified GitHub `issues/labeled` delivery whose label matches `GITHUB_REMEDIATION_LABEL` and whose case has a recorded `APPROVED` decision on the current request and attempt with our own delivery at `LABEL_APPLIED`. A labeled webhook without an approval, or one that arrives before the worker applied the label, is recorded as `label_webhook_unexpected` and never advances the case; the only replay is the worker re-running the same signed, already-persisted delivery once its own `label_applied_at` commit lands (liveness, not a new trust path). |
| Slack approval starts remediation | Slack → Devin | Approval writes a decision and an outbox row; the only consumer applies a GitHub label and comment. No Phase 3 path creates a `REMEDIATION` attempt; integration tests assert the Devin create count is unchanged. |
| Duplicate GitHub side effects | outbox retry after partial success, repeated clicks | Approval enqueues at most one `apply_remediation_label` row (unique `dedupe_key`); `label_requested_at` / `comment_requested_at` are committed before each POST; on retry the worker re-reads the issue labels and searches comments for the request-specific marker `<!-- remediator:approval:<id> -->` before writing, so a crash between GitHub accepting the write and our commit cannot duplicate it. The Slack approval message likewise commits `SENDING` + token hash first and reconciles by message metadata before reposting. |
| Stale approval | triage re-run between notification and label | The approval stores `triage_result_hash`; the worker requires the request to be the case's newest round bound to the newest triage attempt (`is_current_request`) and the hash to match; otherwise it refuses to label (permanent outbox failure, visible as `last_error`). Re-triage supersedes earlier pending rounds in the same transaction (`SUPERSEDED`, token hash nulled, Slack buttons removed) and Slack clicks on an old token return `stale_token`. |
| Cross-repository writes | misconfiguration or forged issue metadata | Both GitHub clients enforce `GITHUB_REPOSITORY` before any request; the repository comes from the case row, never from Slack. |
| Untrusted content in Slack/GitHub | issue title, triage fields, rejection reason | Block Kit text is escaped (`&`, `<`, `>`) and truncated to Slack limits; raw issue body is never sent; rejection reasons come from a fixed select and are stripped of markup before rendering into Slack/GitHub. |
| Delivery failure loses a decision | Slack/GitHub outage | Human decisions are stored before any HTTP. Slack failures never touch the triage result; GitHub failures move the case to `APPROVAL_DELIVERY_FAILED` with the approval intact, and `POST /operator/outbox/{id}/retry` re-queues without re-approval. Retries are bounded (`OUTBOX_MAX_ATTEMPTS`, exponential backoff) and terminal failures are visible with `last_error`. |
| Fake-adapter data exposure | fake Slack messages contain tokens | `slack_fake_messages` is served only via the authenticated `/api/slack/fake/messages`; unauthenticated routes expose no Slack user ids, tokens, or case details. |
| Forged verifier request or verdict | attacker on the internal network posts to `/probe`, or replays/mutates a captured request | Every `/probe` and `/capabilities` request is signed `HMAC-SHA256(key, timestamp.body)` with a ±300 s window; unsigned/mis-signed/stale requests are `401` before the body is parsed. `ProbeRequest` forbids unknown fields, requires an exact allowlisted `owner/name`, a 40-hex SHA and 64-hex hashes. A reused request id with different semantics is `409`; an exact repeat is answered from memory (`replayed`). The worker verifies the response echoes its request id, repository, SHA, script hash and command identity. |
| Verifier asked to run attacker-chosen code | request carries a script body, a URL, or a path | The protocol has no field for script content or clone URL: the verifier resolves `<probe root>/<owner>/<repo>/<issue>/` from its own read-only mount, rejects symlinks and traversal, recomputes both hashes and refuses on mismatch; the clone URL is `VERIFIER_CLONE_URL_FORMAT` applied to the allowlisted repository, and `git remote get-url` plus `rev-parse HEAD` are re-checked after checkout. |
| Unbounded concurrency / spend | webhook burst, two workers, retried claims | Capacity is a PostgreSQL lease table taken under an advisory transaction lock before any create intent: `MAX_CONCURRENT_TRIAGE`/`REMEDIATION`/`PROBES`, a per-repository remediation limit, resource-key mutexes, and the partial unique index for one unfinished attempt per (case, kind). Denied cases stay in their current state with `cases.waiting_for` set ("waiting for capacity" in the dashboard), no attempt row and no HTTP call. Leases are heartbeaten and expire after `CAPACITY_LEASE_GRACE_SECONDS`; a cancel keeps the lease until remote termination is confirmed. |
| Oversized or malformed request bodies | multi-megabyte webhook, chunked flood | Raw ASGI `BodySizeLimitMiddleware` rejects declared or streamed bodies above `MAX_REQUEST_BODY_BYTES` with `413` before FastAPI parses anything; the verifier applies its own 64 KiB limit. |
| CSRF against the operator session | hostile page posts to `/operator/...` with the cookie | Cookie-authenticated mutations must present a same-origin `Sec-Fetch-Site` or an `Origin`/`Referer` matching the request host or `OPERATOR_CSRF_TRUSTED_ORIGINS`; bearer-token requests are exempt. Session cookies are `HttpOnly`, `SameSite=Strict`, `Secure` behind TLS. |
| Operator-action abuse | scripted retry/cancel storm with a valid token | Token-bucket limiter (`OPERATOR_RATE_LIMIT_PER_MINUTE`) on mutation routes returns `429` + `Retry-After`; rejections are counted in `operator_requests_rejected_total{reason}`. |
| SSRF via configuration | provider base URL pointing at loopback, private ranges or cloud metadata | `non_public_service_host` rejects loopback/private/link-local/metadata hosts for Devin/GitHub/Slack bases in live mode; `unsafe_service_url` rejects non-http(s), embedded credentials, query/fragment for every service base; repository names are validated `owner/name` and never used to build URLs from request input. |
| Secret leakage through errors | nested exceptions, HTTP error bodies, `str(exc)` in logs or the dashboard | `Settings.secret_values` includes every configured secret plus the database password and `user:password`; `SettingsRedactingFilter` scrubs every log record (message, args and `%r` of chained causes) and provider clients scrub HTTP error text; tests raise a nested `HTTPStatusError` carrying the secret and assert only `[REDACTED]` reaches the log. |
| Unsafe links in Slack/dashboard | `javascript:`/`data:` URLs from Devin output or PR metadata | `safe_href` allows only absolute http(s) URLs without whitespace/control characters; everything else renders as text. |
| Metric label cardinality / data exposure | issue numbers, session ids, PR URLs as labels | Labels are fixed enums (`mode`, `kind`, `state`, `outcome`, `provider`, `reason`); no identifier is ever a label and `/metrics` requires the operator token. |

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
- The remediation session has write access to the allowlisted fork through
  Devin's own GitHub integration; the remediator cannot prevent Devin from
  pushing arbitrary branches, only from having them accepted. Branch
  protection on the default branch and a review requirement remain necessary.
- Probe scripts run whatever the checked-out commit's tooling does. A
  malicious PR head can therefore execute code inside the verifier. This is
  acceptable only in the credential-free verifier container; it is the reason
  the in-worker local runner fails closed. Egress from the verifier is open
  (see known limitations): the blast radius is the verifier's own workspace
  and outbound bandwidth, never a credential.
- The verifier HMAC key lives in a git-ignored file on the host and in the
  worker's `.env`; rotate both together and restart both services.
- Verifier idempotency is process-local. A restart re-executes an unknown
  request id (deterministically) rather than answering from disk, so the
  cost of a lost answer is one extra probe run, never a fabricated verdict.
- `compare` ancestry and timeline cross-references depend on GitHub's
  eventual consistency; transient errors are retried with the case lease, and
  a persistent disagreement is a verification failure rather than a pass.
- Phase 4 does not merge, close the issue, or auto-fix. `CI_PASSED` is an input
  to human review, not a release decision.
