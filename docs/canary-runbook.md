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
