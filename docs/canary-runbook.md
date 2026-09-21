# Controlled live canary — operator runbook

One human-authorized end-to-end run against a single fork:

| | |
| --- | --- |
| Repository | `marcelaubry/superset` (the only allowlisted repository) |
| Base branch | `master` — the pipeline resolves and pins the **current** tip at dispatch |
| Last human-verified base | `7b6dd7597c4af49f7f0571b7b33dd247f382218b` — readiness comparison only (`GITHUB_BASE_SHA_REFERENCE`), never configured for execution |
| Intake label | `devin:triage` (`GITHUB_REQUIRED_LABEL`) |
| Remediation label | `devin:remediate` (`GITHUB_REMEDIATION_LABEL`) |
| Concurrency | 1 triage, 1 remediation, 1 probe, 1 per repository |
| End state | `CI_PASSED` — the service never merges a PR or closes an issue |

Every step below that talks to a live provider needs the explicit go-ahead of the
person running the canary. Nothing in this repository creates an issue, applies a
label, posts to Slack or spends an ACU on its own: the canary starts when a human
labels the issue (step 7) and ends when the case reaches `CI_PASSED`.

## Envelope (`LIVE_CANARY=true`)

Set `LIVE_CANARY=true` in the deployment `.env` from step 3 onward. `Settings`
then refuses to start (and `make readiness` reports `canary.envelope`) unless:

- exactly one repository is allowlisted;
- `GITHUB_REQUIRED_LABEL` is set and differs from `GITHUB_REMEDIATION_LABEL`;
- `MAX_CONCURRENT_TRIAGE`, `MAX_CONCURRENT_REMEDIATION`, `MAX_CONCURRENT_PROBES`
  and `MAX_CONCURRENT_REMEDIATION_PER_REPOSITORY` are all `1`;
- `PROBE_RUNNER_MODE=remote`;
- `COOKIE_SECURE=true` whenever GitHub or Slack is live;
- GitHub and Slack are live whenever Devin is (fake PR/CI/approval evidence is
  never accepted next to a live session).

The remaining safeguards are unconditional: exact triage-hash approval, an
approved immutable probe that must fail at the pinned base before any
remediation session, PR validation against GitHub (author, branch prefix, base
SHA, `probes/` untouched, file budget), the same probe snapshot at the exact PR
head, required checks read for that head SHA only, and a hard stop on every
repository / base SHA / issue / approval / probe / PR / CI contradiction.

## Activation order

Work through the steps in order; do not skip ahead when a step is yellow.

1. **All-fake mode.** `cp .env.example .env`, `docker compose up --build -d`.
   Open the dashboard, sign in with `OPERATOR_TOKEN`.
2. **Complete simulations.** `uv run python scripts/simulate.py --scenario all --wait`
   (Phase 1–5, zero external calls). Every scenario must report its expected
   end state before any provider goes live. Phase 4 fixtures are fake probes for
   `apache/superset#42xx`, so this step needs `PROBE_RUNNER_MODE=fake`; with the
   remote verifier those scenarios stop at `PROBE_INFRASTRUCTURE_BLOCKED` by design.
3. **Live Slack.** Fill the `Controlled live canary` block of `.env.example`
   into the deployment `.env` (real values only there, `LIVE_CANARY=true`,
   `COOKIE_SECURE=true`, HTTPS `PUBLIC_BASE_URL` / `DASHBOARD_BASE_URL`), set
   `SLACK_CLIENT_MODE=live` with the bot token, signing secret, approval
   channel and approver ids. `make readiness` must show `slack.*` green;
   `make readiness-mutating CONFIRM_CHANNEL=<SLACK_CHANNEL_ID>` posts the one
   allowed test message — confirm it in the channel and that the interactivity
   request URL points at `PUBLIC_BASE_URL/webhooks/slack/actions`.
4. **Live GitHub and remote verifier.** `GITHUB_CLIENT_MODE=live` with the
   fine-grained token (Issues read/write + Metadata read on the fork only),
   `GITHUB_REPOSITORY=marcelaubry/superset`, `GITHUB_BASE_REF=master`,
   `GITHUB_REQUIRED_LABEL=devin:triage`, `PROBE_RUNNER_MODE=remote`,
   `VERIFIER_REPOSITORY_ALLOWLIST=marcelaubry/superset`,
   `PROBE_SMOKE_PROBE=marcelaubry/superset#0`. Register the webhook
   (`issues` events, secret = `GITHUB_WEBHOOK_SECRET`, URL
   `PUBLIC_BASE_URL/webhooks/github`) and create both labels on the fork.
   `docker compose up -d` restarts the stack with the new modes.
5. **Non-mutating readiness + real smoke.** `make readiness` then
   `make readiness-smoke`. Required green: `github.repository`,
   `github.default_branch`, `github.base_sha` (record the printed SHA; a `warn`
   means master moved past `GITHUB_BASE_SHA_REFERENCE` — re-run the smoke and
   review the probe manifest before continuing), `github.label[devin:triage]`,
   `github.label[devin:remediate]`, `verifier.key_separation`,
   `verifier.isolation`, `verifier.egress`, `verifier.node`, `verifier.smoke`,
   `dashboard.cookie_secure`, `allowlist.required_label`, `canary.envelope`.
   `PROBE_SMOKE_PROBE=marcelaubry/superset#0` clones the fork, runs `npm ci`
   in `superset-frontend` and one Jest test: the heaviest probe in the
   repository (up to 1800 s), so the verifier needs its full CPU/memory/disk
   envelope and network reach to npm through the egress proxy.
6. **Live Devin last.** `DEVIN_CLIENT_MODE=live`, `DEVIN_API_KEY`,
   `DEVIN_ORG_ID`, `DEVIN_TRIAGE_TIMEOUT_SECONDS=1800`,
   `DEVIN_REMEDIATION_TIMEOUT_SECONDS=5400`, `DEVIN_POLL_INTERVAL_SECONDS=15`.
   Restart, then `make readiness` again: `devin.identity`, `devin.list_sessions`,
   `devin.repos_format` (`marcelaubry/superset`), `devin.repository_access`
   and `canary.envelope` must pass. Still zero ACUs at this point.
7. **Exactly one issue.** A human creates one issue on the fork that satisfies
   the eligibility filter (reproduction, objective checks, acceptance criteria)
   and applies `devin:triage`. Note the issue number and the `X-GitHub-Delivery`
   ids of the `opened` and `labeled` deliveries.
8. **Exactly one triage session.** Dashboard: one case, one `TRIAGE` attempt,
   one operation key, one Devin session URL; `GET /v3/organizations/{org}/sessions`
   (or the Devin app) shows exactly one session tagged with that operation key.
   More than one session, or a session without the tag, is an emergency stop.
9. **Review triage.** Wait for `AWAITING_REMEDIATION_APPROVAL`. Read the triage
   output on the case page and in the Slack post; record the triage output hash.
   Do not approve if the plan touches `probes/`, exceeds the file budget, or the
   base SHA differs from what `github.base_sha` printed.
10. **Immutable issue-specific probe.** Only now write
    `probes/marcelaubry/superset/<issue>/probe.sh`, register it with
    `scripts/register_probe.py marcelaubry/superset <issue> --base-sha <pinned base>
    --base-exit 1 --head-exit 0 …`, commit, and redeploy so the read-only
    registry mount contains it. Record the probe identifier and hashes.
11. **Approve through Slack.** One approver clicks *Approve*. The approval is
    bound to the exact triage hash; the worker applies `devin:remediate` and
    waits for GitHub's own `labeled` delivery. Record actor and timestamp.
12. **Observe.** `REMEDIATION_APPROVED` → `PROBE_VALIDATING_BASE` (must exit
    `base`, no ACUs before that) → `REMEDIATION_CREATE_INTENT` → `REMEDIATING`
    → `PR_DISCOVERED` → `PR_VALIDATING` → `PROBE_VALIDATING_HEAD` →
    `PR_VALIDATED` → `CI_PENDING`. Every transition is on the case page with the
    Slack thread mirroring it.
13. **Stop at `CI_PASSED`.** The case is terminal for the service. A human
    reviews the PR on GitHub. The service has no code path that merges a PR or
    closes an issue.

## Rollback and emergency stop

Any of these can be done at any point; do them top-down and stop when the
situation is under control.

1. **Close the intake.** Remove `devin:triage` from the issue (or delete the
   label on the fork). No new triage can start; `labeled` events for other
   labels are ignored.
2. **Devin back to fake.** `DEVIN_CLIENT_MODE=fake` in `.env` and
   `docker compose up -d`. No further create/poll/terminate calls go out;
   in-flight live attempts can no longer be observed by the service, so do
   step 3 first whenever a session may still be running.
3. **Cancel the active case.** `POST /operator/cases/{id}/cancel` with the
   operator token (dashboard *Cancel* does the same). A live session goes
   through `TERMINATION_PENDING` / `REMEDIATION_TERMINATION_PENDING` → remote
   `DELETE` → `CANCELLED` / `REMEDIATION_CANCELLED`.
4. **Confirm remote termination.** The Devin app or
   `GET /v3/organizations/{org}/sessions/{id}` shows the session terminated.
   If the service was already back in fake mode before the cancel, terminate
   the session manually from the Devin app and record that you did.
5. **Preserve evidence.** Do not delete the database: `cases`, `attempts`
   (operation keys), `state_transitions`, `probe_snapshots`,
   `probe_executions`, `pull_request_evidence`, `ci_snapshots`,
   `approval_events` — nor the Slack thread. Export the case page and the
   readiness output into the incident notes.
6. **Never blindly retry an uncertain create.** A case in `RECONCILING_CREATE`
   / `REMEDIATION_RECONCILING_CREATE` or an attempt with `create_state=UNCERTAIN`/`UNRESOLVED`
   means a session may exist. List sessions by the operation-key tag first;
   only when none exists is `POST /operator/cases/{id}/retry?confirm_no_session=true`
   acceptable, and only with `LIVE_CANARY` still enforcing a single slot.

## Recovering a triage stuck in `HUMAN_BLOCKED` after the session finished

An interactive Devin session reports `status_detail=waiting_for_user`
("Devin is awaiting instructions") once its task is done, including after it
has submitted `structured_output`. The worker now treats a snapshot that
carries structured output as *finished* regardless of that status, and it
re-reads (`GET` only) retained sessions of `HUMAN_BLOCKED` cases on every pass
(`HUMAN_BLOCKED_RECONCILE_INTERVAL_SECONDS`). A case blocked by the earlier
behaviour is recovered like this — no database edits, no new issue, no new
session:

1. Deploy this release and let the worker run, **or** open the case page and
   click **Reconcile existing session** (`POST
   /operator/cases/{id}/reconcile-session`). Both paths only `GET` the
   retained session(s); neither ever `POST`s a create.
2. If the case already has two triage attempts (a dashboard *Retry* created a
   duplicate), the newest attempt whose session holds schema-valid output
   becomes authoritative; the other blocked attempt is marked `CANCELLED`
   ("superseded by reconciled attempt …") and stays in the audit trail. No
   third attempt is created. If only the older session has valid output, that
   one wins.
3. The case advances `HUMAN_BLOCKED` → `TRIAGING` → `TRIAGED` →
   `AWAITING_REMEDIATION_APPROVAL`, exactly one `approval_requests` row is
   created (bound to the sha256 of the ingested output) and exactly one Slack
   approval card is queued. Repeating the action, or the worker passing over
   the case again, changes nothing.
4. If the retained session still has no output, the response says so and the
   case stays `HUMAN_BLOCKED` with the attempt annotated
   `reconciled without structured output …`. Only then does the page also
   offer **Retry (replacement triage)**, which terminates the retained
   session before any new create; the plain `retry` endpoint answers `409`
   until that reconciliation has happened.
5. If the session no longer exists remotely (`404`), the attempt is annotated
   as gone and a replacement retry becomes available under the same rule.

Record the attempt IDs, the superseding decision and the approval hash in the
canary checklist below.

## Acceptance-probe policy (`PROBE_POLICY`)

`PROBE_POLICY=required` (default) is the original production behaviour: an
approved immutable probe must exist, reproduce the defect at the pinned base SHA
and pass unchanged at the PR head; a missing registration blocks the case with
`approved probe unavailable: no approved probe registered at /app/probes/<owner>/<repo>/<n>/probe.yaml`.

`PROBE_POLICY=if_available` runs exactly that verification whenever a probe is
registered. With no probe registered, the validated Slack approval and the
confirmed `devin:remediate` label proceed straight to the bounded session; only
the probe-registration requirement and the BASE/HEAD executions have nothing to
run. Everything else still gates the case — allowlisted repository, the approval
bound to the exact triage hash, the dynamically pinned base SHA, capacity and
spend, durable create intent with operation-key idempotency and uncertain-create
reconciliation, structured-output validation, PR discovery from `pull_requests[]`,
independent GitHub PR validation and CI for the exact head SHA. No probe
snapshot, probe execution or probe-passed metric is ever written, and nothing
merges or closes anything. The case page, dashboard, Slack update, the
`probe_outcomes_total{target="none",outcome="not_configured"}` counter and the
append-only `probe_policy_decisions` row all report:

> Acceptance probe: not configured. PR structure and exact-head CI will still be independently validated.

`LIVE_CANARY_ALLOW_MISSING_PROBE` has been removed: `make readiness` fails
`probe.policy_migration` while it is still set in the environment. Historical
`CANARY_PROBE_OVERRIDE` rows are retained and still rendered on the cases that
ran under it; nothing writes new ones.

Resuming a case that is already approved and blocked solely on the missing probe —
no database edit, no new approval, no new triage session, no new issue:

1. Set `PROBE_POLICY=if_available` in the deployment `.env`, run `make readiness`
   (expect the `probe.policy` warning) and `docker compose up -d`.
2. The worker picks the case up on its own: only a case blocked *solely* on a
   missing registration is re-evaluated, and `_dispatch()` re-validates every
   precondition above before anything is created. Any other blocker keeps the
   case blocked.
3. Exactly one `probe_policy_decisions` row, one remediation attempt and one
   Devin session result; the case then follows the normal path to `CI_PASSED` and
   stops there for human PR review.
4. Set `PROBE_POLICY=required` again once probes are registered for the repository.

Record the decision row (actor, timestamp, triage hash, pinned base SHA) in the
checklist below, and mark the probe lines `n/a — probe not configured`.

## Canary checklist

Copy into the canary notes. Identifiers only — never secrets, tokens, or raw
issue/webhook payloads.

```text
Controlled canary — marcelaubry/superset
Date / operator:
Readiness run (make readiness, make readiness-smoke) all green at:
Current master SHA (github.base_sha):
  equals GITHUB_BASE_SHA_REFERENCE 7b6dd7597c4af49f7f0571b7b33dd247f382218b? [ ] yes [ ] no (re-verified smoke at new SHA: )
Issue number:
Webhook delivery ids: opened=            labeled(devin:triage)=            labeled(devin:remediate)=
Case id:
Triage attempt id:                       operation key:
Triage Devin session id / URL:
Triage output hash (triage_result_hash):
Slack approval: actor=                   ts=
Probe policy: [ ] required [ ] if_available — probe_status: [ ] verified [ ] not_configured (decision actor/ts: )
Probe identifier (marcelaubry/superset#<issue>):   manifest hash:          script hash:
Base probe result: exit=      expected base=      verdict=
Remediation attempt id:                  operation key:
Remediation Devin session id / URL:
PR number:                               exact head SHA:
Head probe result: exit=      expected head=      verdict=
Required CI checks (name → conclusion, head SHA):
Final lifecycle state:                   (expected CI_PASSED)
ACUs consumed (triage / remediation, when the plan reports them):
Confirmed: PR not merged by the service [ ]   issue not closed by the service [ ]
Emergency stop used? [ ] no [ ] yes — steps taken:
```
