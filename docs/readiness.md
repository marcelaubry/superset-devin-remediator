# Live-readiness runbook

```bash
make readiness                 # read-only; exit 1 if any check fails
make readiness-mutating        # adds the single mutating check (one Slack test message)
uv run python -m remediator.readiness --json   # machine-readable
```

The command loads `.env` exactly as the services do, then runs every check
below **without** creating a Devin session, writing to GitHub, modifying a
database row or posting to Slack. Anything mutating is skipped unless
`--allow-mutations` is present, and today that is only `slack.test_message`.
All output passes through the settings redactor and secrets are printed as
`length=N sha256=abcd…` masks — never as values.

Statuses: `pass`, `warn` (acceptable in fake/simulated modes, wrong for
production), `fail`, `skip` (not applicable in this mode).

## Checks

| Check | Verifies |
| --- | --- |
| `config.modes` | Which adapters are live; warns when a live Devin is paired with any fake adapter |
| `config.secret.<NAME>` | Each required secret is present, not a `change-me` placeholder and ≥ 16 chars (`fail` in live mode, `warn` otherwise); conditional secrets are `skip` when their adapter is fake |
| `config.devin_org_id` | Set when Devin is live |
| `config.verifier_secret_length` | `PROBE_VERIFIER_SHARED_SECRET` is ≥ 32 chars when the remote runner is configured |
| `config.env_permissions` | `.env` is not group/world readable |
| `config.acu_reporting` | `DEVIN_ACU_REPORTING_ENABLED` also has `DEVIN_ORG_ID` |
| `database.connect` / `database.migrations` | Connection with the configured URL; `alembic_version` exists and equals the code's head revision |
| `devin.api_base_url` | HTTPS, public host, no embedded credentials |
| `devin.identity` | `GET /v3/self` — service user and organization match `DEVIN_ORG_ID` |
| `devin.list_sessions` | Read-only session list on the organization succeeds (needed for create reconciliation) |
| `devin.acu_reporting` | Reminder (`warn`) that consumption is fetched lazily and shows `Unavailable` when the plan/service user lacks the permission — no consumption call is made here |
| `github.api_base_url` | HTTPS, public host |
| `github.repository[owner/name]` | Repository exists and resolves to the configured name |
| `github.default_branch[...]` | Equals `GITHUB_BASE_REF` |
| `github.permissions[...]` | Token reports `pull` (read); `push` is shown for information (labels/comments need `issues:write`); fine-grained tokens that omit `permissions` are `warn` |
| `github.label[...]` | `GITHUB_REQUIRED_LABEL` / `GITHUB_REMEDIATION_LABEL` exist |
| `slack.api_base_url` | HTTPS, public host |
| `slack.identity` | `auth.test` returns the bot identity and team |
| `slack.channel` | Bot is a member of `SLACK_APPROVAL_CHANNEL` |
| `slack.approver[Uxxxx]` | Each `SLACK_APPROVER_USER_IDS` entry resolves via `users.info` to a non-deleted user |
| `slack.test_message` | **mutating** — posts one message to the approval channel |
| `verifier.url` | Internal http(s) URL without credentials |
| `verifier.health` | Signed `GET /capabilities` answers with `verifier.v2`; reports UID and in-flight/max concurrency |
| `verifier.isolation` | No credentials visible, non-root, read-only root, `no-new-privileges`, empty capability sets, cgroup PID and memory limits (fails closed when any cannot be shown) |
| `verifier.tools` / `verifier.node` | Every known tool is present with its version; Node is 24.x (Superset master pins `^24.16.0`) |
| `verifier.registry` | The read-only probe registry is mounted in the verifier |
| `verifier.allowlist` | Verifier repository allowlist covers the worker allowlist |
| `verifier.timeout` / `verifier.cache` | `PROBE_TIMEOUT_SECONDS` ≤ verifier max; whether the dependency download cache is enabled |
| `webhook.base_url` / `webhook.health` | `PUBLIC_BASE_URL` is HTTPS and its `/health` answers from the outside |
| `allowlist.repositories` / `allowlist.required_label` | Non-empty exact `owner/name` allowlist; a required label is configured for live GitHub |
| `limits.concurrency` / `limits.per_repository` / `limits.acu` | `MAX_CONCURRENT_*` ≥ 1; per-repository limit ≤ global (otherwise `warn`, global wins); ACU caps set and positive |

## Minimal provider permissions

**Devin service user** (API key scoped to one organization):

- `GET /v3/self`; on `/organizations/{org}/sessions`: list (reconciliation by
  tag), get, create with `max_acu_limit`, delete (termination).
- Optional: `GET /organizations/{org}/consumption/daily/sessions/{id}`
  (requires the org consumption permission). Not available on every plan; the
  remediator shows `Unavailable` rather than failing.
- Repository access for the allowlisted fork only, granted inside Devin's own
  GitHub integration.

**GitHub token / App** on the allowlisted repository only:

- Read: contents (compare/commits), pull requests, issues, checks, metadata.
- Write: issues (labels and comments for the approval gate). No `workflow`,
  `administration`, or `contents: write` — the remediator never pushes.
- Webhook: `issues` events, JSON payload, secret = `GITHUB_WEBHOOK_SECRET`.

**Slack app** in one workspace:

- Bot scopes: `chat:write`, `channels:read` (or `groups:read` for private
  channels), `users:read`; interactivity request URL pointing at
  `/webhooks/slack/actions`; signing secret = `SLACK_SIGNING_SECRET`.
- Invite the bot to `SLACK_APPROVAL_CHANNEL`. No `chat:write.public`, no
  user tokens.

## Typical failures

| Symptom | Cause / action |
| --- | --- |
| `config.secret.* fail` | `.env` still has a `change-me`; generate ≥ 32 random chars |
| `database.migrations fail` | run `make migrate` |
| `devin.identity fail 401` | key belongs to another org or was rotated |
| `verifier.isolation fail` | compose hardening missing or the host cannot expose cgroup limits — see known limitations; live mode will refuse probes |
| `verifier.registry fail` | `probes/` is not mounted read-only into the verifier; check `docker-compose.yml` volumes |
| `webhook.health fail` | tunnel down or `PUBLIC_BASE_URL` stale |
| `slack.channel fail` | invite the bot to the channel |
