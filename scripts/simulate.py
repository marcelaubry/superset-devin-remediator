"""Drive the running stack through its real HTTP endpoints only.

Phase 1/2 scenarios post signed GitHub `issues.opened` webhooks and wait for the case to
settle. Phase 3 scenarios continue from a validated remediation candidate: read the fake
Slack channel via the operator API, submit correctly (or deliberately incorrectly) signed
Slack interaction payloads to POST /webhooks/slack/actions, let the worker process the
outbox, post the signed GitHub `labeled` webhook and wait for the Phase 4 precondition gate.
Nothing here bypasses production validation or writes to the database.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "fixtures/github"

SCENARIOS = {
    "good": "issue_good_candidate.json",
    "needs-scoping": "issue_needs_scoping.json",
    "deterministic": "issue_deterministic.json",
    "human-led": "issue_human_led.json",
    "failure": "issue_fake_failure.json",
    "blocked": "issue_human_blocked.json",
    "triage-infeasible": "issue_triage_infeasible.json",
    "wrong-repo": "issue_wrong_repo.json",
    "missing-label": "issue_missing_label.json",
    # Phase 2 fake Devin scenarios (issue numbers pinned in remediator.devin.fake.FIXTURE_SCENARIOS)
    "malformed-output": "issue_malformed_output.json",
    "missing-output": "issue_missing_output.json",
    "uncertain-create": "issue_uncertain_create.json",
    "quota-failure": "issue_quota_failure.json",
    "timeout": "issue_timeout.json",
    "unknown-status": "issue_unknown_status.json",
    "create-rejected": "issue_create_rejected.json",
}

# Phase 3: each scenario starts from a distinct fixture issue so runs do not interfere.
PHASE3_SCENARIOS = {
    "approve": "issue_good_candidate.json",  # 4213: approval + label; no probe -> zero-ACU gate
    "reject": "issue_approval_reject.json",  # 4219
    "slack-negative": "issue_approval_negative.json",  # 4217: bad sig / stale / unauthorized / dup
    "expired-token": "issue_approval_expired.json",  # 4216
    "slack-delivery-failure": "issue_slack_failure.json",  # 4699: fake Slack fails, retry
    "github-label-failure": "issue_label_failure.json",  # 4688: fake GitHub fails, retry
}

# Phase 4: fixture issue numbers are pinned in remediator.fixtures.REMEDIATION_FIXTURES and
# each has an immutable probe under probes/apache/superset/<n>/. Payloads are derived from
# the good-candidate fixture so triage always yields `remediation_candidate`.
PHASE4_SCENARIOS: dict[str, int] = {
    "remediate": 4702,  # BASE fails -> session -> PR -> HEAD passes -> CI_PASSED
    "remediate-base-passes": 4744,  # probe already passes at base: zero ACUs, human review
    "remediate-probe-infra": 4748,  # runner unavailable: PROBE_INFRASTRUCTURE_BLOCKED, zero ACUs
    "remediate-head-fails": 4747,  # PR exists but probe still fails at head: no fix chain
    "remediate-forbidden-files": 4738,  # PR touches probes/workflows: fails closed
    "remediate-ci-failed": 4751,  # required checks fail for the verified head SHA
    "remediate-uncertain-create": 4706,  # POST unknown -> tag reconciliation, single session
    "remediate-unlabeled-intake": 4222,  # unlabeled opened issue is evaluated; `bug` label inert
}

REMEDIATION_RESTING_STATES = {
    "REMEDIATION_HUMAN_BLOCKED",
    "PROBE_INFRASTRUCTURE_BLOCKED",
    "REMEDIATION_FAILED",
    "REMEDIATION_TIMED_OUT",
    "REMEDIATION_CANCELLED",
    "CI_PASSED",
    "CI_FAILED",
}

TERMINAL_STATES = {
    "AWAITING_REMEDIATION_APPROVAL",
    *REMEDIATION_RESTING_STATES,
    "REMEDIATION_APPROVED",
    "REMEDIATION_REJECTED",
    "APPROVAL_DELIVERY_FAILED",
    "CI_PASSED",
    "FAILED",
    "HUMAN_BLOCKED",
    "POLICY_REJECTED",
    "CANCELLED",
    "TIMED_OUT",
}

ACTION_APPROVE = "approve_remediation"
ACTION_REJECT = "reject_remediation"

# Phase 3 fixtures register no immutable probe, so once the signed label webhook lands the
# Phase 4 precondition gate fails closed (zero ACUs) instead of parking in REMEDIATION_APPROVED.
PHASE3_POST_LABEL_STATES = ("REMEDIATION_APPROVED", "REMEDIATION_HUMAN_BLOCKED")


def _environment(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is not None:
        return value
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, separator, candidate = line.partition("=")
            if separator and key.strip() == name:
                return candidate.strip()
    return default


class SimulationError(RuntimeError):
    pass


class Simulator:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.github_secret = _environment("GITHUB_WEBHOOK_SECRET", "change-me").encode()
        self.slack_secret = _environment("SLACK_SIGNING_SECRET", "").encode()
        self.operator = {
            "Authorization": f"Bearer {_environment('OPERATOR_TOKEN', 'change-me')}",
            "Accept": "application/json",
        }
        approvers = _environment("SLACK_APPROVER_USER_IDS", "U0000000001")
        self.approver = approvers.split(",")[0].strip()
        self.failures: list[str] = []

    # ------------------------------------------------------------------ helpers
    def check(self, condition: bool, what: str) -> None:
        marker = "ok  " if condition else "FAIL"
        print(f"  [{marker}] {what}")
        if not condition:
            self.failures.append(what)

    def load(self, fixture: str) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads((FIXTURES / fixture).read_text())
        return payload

    def github_post(
        self, payload: dict[str, Any], *, bad_signature: bool = False
    ) -> httpx.Response:
        body = json.dumps(payload, separators=(",", ":")).encode()
        digest = (
            hashlib.sha256(body).hexdigest()
            if bad_signature
            else hmac.new(self.github_secret, body, hashlib.sha256).hexdigest()
        )
        return self.client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": f"sha256={digest}",
            },
        )

    def case(self, repository: str, number: int) -> dict[str, Any] | None:
        response = self.client.get(f"/api/cases/{repository}/{number}", headers=self.operator)
        return response.json() if response.status_code == 200 else None

    def wait_for(
        self, repository: str, number: int, predicate: Any, what: str, timeout: float = 120
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self.case(repository, number)
            if last is not None and predicate(last):
                return last
            time.sleep(0.25)
        raise SimulationError(
            f"timed out waiting for {what} on {repository}#{number}; "
            f"last state={last and last.get('state')} approval={last and last.get('approval')}"
        )

    def wait_state(self, repository: str, number: int, *states: str) -> dict[str, Any]:
        return self.wait_for(
            repository, number, lambda c: c["state"] in states, " or ".join(states)
        )

    def wait_approval(self, repository: str, number: int, **fields: str) -> dict[str, Any]:
        def ready(case: dict[str, Any]) -> bool:
            approval = case.get("approval") or {}
            return all(approval.get(key) == value for key, value in fields.items())

        return self.wait_for(repository, number, ready, f"approval {fields}")

    def open_candidate(self, fixture: str) -> tuple[str, int, dict[str, Any]]:
        payload = self.load(fixture)
        repository = payload["repository"]["full_name"]
        number = payload["issue"]["number"]
        existing = self.case(repository, number)
        if existing is None:
            response = self.github_post(payload)
            print(f"  opened {repository}#{number}: {response.status_code} {response.json()}")
        else:
            print(f"  {repository}#{number} already exists in state {existing['state']}")
            approval = existing.get("approval") or {}
            if existing["state"] != "AWAITING_REMEDIATION_APPROVAL" or approval.get(
                "decision"
            ) not in {None, "PENDING"}:
                raise SimulationError(
                    f"{repository}#{number} was already decided in an earlier run "
                    f"(state {existing['state']}, decision {approval.get('decision')}); "
                    "Phase 3 scenarios exercise the real one-decision-per-case lifecycle, so "
                    "start from a clean database (`docker compose down -v && docker compose up -d`)"
                )
        case = self.wait_state(
            repository,
            number,
            "AWAITING_REMEDIATION_APPROVAL",
            "REMEDIATION_APPROVED",
            "REMEDIATION_REJECTED",
            "APPROVAL_DELIVERY_FAILED",
        )
        return repository, number, case

    def slack_token(self, repository: str, number: int) -> str:
        """Read the approve button value from the fake Slack channel (operator-only endpoint)."""
        response = self.client.get(
            "/api/slack/fake/messages", headers=self.operator, params={"limit": 200}
        )
        response.raise_for_status()
        marker = f"{repository}#{number}"
        for message in response.json():
            if marker not in message["text"]:
                continue
            for block in message["blocks"]:
                if block.get("type") != "actions":
                    continue
                for element in block.get("elements", []):
                    if element.get("action_id") == ACTION_APPROVE and element.get("value"):
                        return str(element["value"])
        raise SimulationError(f"no pending Slack approval message found for {marker}")

    def slack_message(self, repository: str, number: int) -> dict[str, Any]:
        response = self.client.get(
            "/api/slack/fake/messages", headers=self.operator, params={"limit": 200}
        )
        response.raise_for_status()
        marker = f"{repository}#{number}"
        messages: list[dict[str, Any]] = response.json()
        for message in messages:
            if marker in message["text"]:
                return message
        raise SimulationError(f"no Slack message for {marker}")

    def slack_click(
        self,
        token: str,
        *,
        action_id: str = ACTION_APPROVE,
        user: str | None = None,
        action_ts: str | None = None,
        bad_signature: bool = False,
        timestamp: str | None = None,
        reason: str | None = None,
    ) -> httpx.Response:
        if not self.slack_secret:
            raise SimulationError("SLACK_SIGNING_SECRET is not configured; cannot sign actions")
        action: dict[str, Any] = {
            "type": "button",
            "block_id": "remediation_decision",
            "action_id": action_id,
            "value": token,
            "action_ts": action_ts or f"{time.time():.6f}",
        }
        payload: dict[str, Any] = {
            "type": "block_actions",
            "user": {"id": user or self.approver, "username": "simulator"},
            "channel": {"id": "C_SPOOFED_BY_CLIENT"},
            "container": {"message_ts": "0.0"},
            "actions": [action],
        }
        if reason:
            payload["state"] = {
                "values": {
                    "rejection_reason": {
                        "rejection_reason": {
                            "type": "static_select",
                            "selected_option": {"value": reason},
                        }
                    }
                }
            }
        body = urlencode({"payload": json.dumps(payload)}).encode()
        ts = timestamp or str(int(time.time()))
        secret = b"not-the-signing-secret" if bad_signature else self.slack_secret
        digest = hmac.new(secret, f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/webhooks/slack/actions",
            content=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": f"v0={digest}",
            },
        )

    def label_webhook(self, fixture: str) -> httpx.Response:
        payload = self.load(fixture)
        payload["action"] = "labeled"
        payload["label"] = {"name": "devin:remediate"}
        payload["issue"]["labels"].append({"name": "devin:remediate"})
        return self.github_post(payload)

    def retry_failed_outbox(self, case: dict[str, Any], kind: str) -> None:
        rows = [r for r in case["outbox"] if r["kind"] == kind and r["status"] == "FAILED"]
        if not rows:
            raise SimulationError(f"no FAILED outbox row of kind {kind}")
        row = rows[0]
        print(f"  outbox {kind}: attempts={row['attempts_count']} last_error={row['last_error']!r}")
        unauth = self.client.post(f"/operator/outbox/{row['id']}/retry")
        self.check(unauth.status_code in {401, 303}, "operator retry requires authentication")
        response = self.client.post(f"/operator/outbox/{row['id']}/retry", headers=self.operator)
        self.check(response.status_code == 200, f"operator retry accepted ({response.status_code})")

    def summarize(self, case: dict[str, Any]) -> None:
        approval = case.get("approval") or {}
        print(
            "  timeline",
            json.dumps(
                {
                    "state": case["state"],
                    "notification": approval.get("notification_status"),
                    "decision": approval.get("decision"),
                    "decided_by": approval.get("decided_by_slack_user_id"),
                    "delivery": approval.get("delivery_status"),
                    "events": [e["kind"] for e in approval.get("events", [])],
                    "outbox": [
                        (r["kind"], r["status"], r["attempts_count"]) for r in case["outbox"]
                    ],
                }
            ),
        )

    # ---------------------------------------------------------------- phase 1/2
    def run_ingest(self, scenario: str, *, repeat: int, bad_signature: bool, wait: bool) -> None:
        payload = self.load(SCENARIOS[scenario])
        response: httpx.Response | None = None
        for index in range(repeat):
            response = self.github_post(payload, bad_signature=bad_signature)
            print(scenario, index + 1, response.status_code, response.json())
        if not (wait and response is not None and response.json().get("accepted")):
            return
        if not payload.get("issue", {}).get("number"):
            return
        repository = payload["repository"]["full_name"]
        number = payload["issue"]["number"]
        result = self.wait_state(repository, number, *TERMINAL_STATES)
        summary = {key: value for key, value in result.items() if key not in {"attempts", "outbox"}}
        summary["attempts"] = [
            {
                "kind": attempt["kind"],
                "status": attempt["status"],
                "create_state": attempt["create_state"],
                "devin_status": attempt["devin_status"],
                "devin_status_detail": attempt["devin_status_detail"],
                "outcome": (attempt.get("structured_output") or {}).get("outcome"),
                "reason": attempt.get("reconciliation_reason") or attempt.get("error"),
            }
            for attempt in result.get("attempts", [])
        ]
        print("timeline", json.dumps(summary))
        if result["state"] == "AWAITING_REMEDIATION_APPROVAL":
            print("  awaiting a Slack decision; run `--scenario approve` for the Phase 3 flow")

    # ------------------------------------------------------------------ phase 3
    def run_approve(self) -> None:
        fixture = PHASE3_SCENARIOS["approve"]
        repository, number, case = self.open_candidate(fixture)
        if case["state"] in PHASE3_POST_LABEL_STATES:
            print(f"  already {case['state']} from a previous run")
            self.summarize(case)
            return
        case = self.wait_approval(repository, number, notification_status="SENT")
        self.check(
            case["state"] == "AWAITING_REMEDIATION_APPROVAL", "triage parked awaiting approval"
        )
        message = self.slack_message(repository, number)
        self.check("Awaiting approval" in json.dumps(message["blocks"]), "Slack message posted")
        token = self.slack_token(repository, number)
        response = self.slack_click(token)
        print(f"  approve click: {response.status_code} {response.json()}")
        self.check(response.json().get("outcome") == "approved", "signed approval accepted")
        case = self.wait_approval(repository, number, delivery_status="LABEL_APPLIED")
        self.check(
            case["state"] == "AWAITING_REMEDIATION_APPROVAL",
            "label applied by worker but state waits for the signed GitHub webhook",
        )
        self.check(
            all(a["kind"] == "TRIAGE" for a in case["attempts"]),
            "no remediation attempt/session was created by approval",
        )
        duplicate = self.slack_click(token, action_ts="1.000000")
        duplicate_again = self.slack_click(token, action_ts="1.000000")
        self.check(
            duplicate.json().get("outcome") in {"already_decided", "duplicate"}
            and duplicate_again.json().get("outcome") == "duplicate",
            f"repeat clicks are no-ops ({duplicate.json().get('outcome')}, "
            f"{duplicate_again.json().get('outcome')})",
        )
        webhook = self.label_webhook(fixture)
        print(f"  labeled webhook: {webhook.status_code} {webhook.json()}")
        case = self.wait_state(repository, number, *PHASE3_POST_LABEL_STATES)
        self.check(case["approval"]["delivery_status"] == "CONFIRMED", "label confirmed by webhook")
        self.check_no_paid_remediation(case)
        self.wait_for(
            repository,
            number,
            lambda c: all(r["status"] == "SENT" for r in c["outbox"]),
            "all outbox rows sent",
        )
        message = self.slack_message(repository, number)
        blocks = json.dumps(message["blocks"])
        self.check(
            ACTION_APPROVE not in blocks and "`devin:remediate` applied" in blocks,
            "Slack message updated and decision buttons removed",
        )
        self.summarize(case)

    def run_reject(self) -> None:
        fixture = PHASE3_SCENARIOS["reject"]
        repository, number, case = self.open_candidate(fixture)
        if case["state"] == "REMEDIATION_REJECTED":
            print("  already REMEDIATION_REJECTED from a previous run")
            self.summarize(case)
            return
        self.wait_approval(repository, number, notification_status="SENT")
        token = self.slack_token(repository, number)
        response = self.slack_click(token, action_id=ACTION_REJECT, reason="too_risky")
        print(f"  reject click: {response.status_code} {response.json()}")
        self.check(response.json().get("outcome") == "rejected", "signed rejection accepted")
        case = self.wait_state(repository, number, "REMEDIATION_REJECTED")
        self.wait_for(
            repository,
            number,
            lambda c: all(r["status"] == "SENT" for r in c["outbox"]),
            "rejection comment + Slack update sent",
        )
        case = self.case(repository, number) or case
        kinds = {r["kind"] for r in case["outbox"]}
        self.check("rejection_comment" in kinds, "GitHub rejection comment queued and sent")
        self.check("apply_remediation_label" not in kinds, "no label operation was queued")
        self.check(case["approval"]["decision_reason"] == "too_risky", "reason recorded")
        blocks = json.dumps(self.slack_message(repository, number)["blocks"])
        self.check(ACTION_APPROVE not in blocks and "Rejected" in blocks, "Slack shows rejected")
        self.summarize(case)

    def run_slack_negative(self) -> None:
        repository, number, case = self.open_candidate(PHASE3_SCENARIOS["slack-negative"])
        self.wait_approval(repository, number, notification_status="SENT")
        token = self.slack_token(repository, number)
        bad = self.slack_click(token, bad_signature=True)
        self.check(bad.status_code == 401, f"invalid signature rejected ({bad.status_code})")
        stale = self.slack_click(token, timestamp=str(int(time.time()) - 3600))
        self.check(stale.status_code == 401, f"stale timestamp rejected ({stale.status_code})")
        missing = self.client.post(
            "/webhooks/slack/actions",
            content=b"payload=%7B%7D",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.check(missing.status_code == 401, f"missing headers rejected ({missing.status_code})")
        outsider = self.slack_click(token, user="U_NOT_AN_APPROVER")
        self.check(
            outsider.status_code == 200
            and outsider.json().get("ok") is False
            and outsider.json().get("outcome") == "unauthorized",
            f"unauthorized approver rejected ({outsider.status_code})",
        )
        unknown = self.slack_click("not-a-real-token")
        self.check(
            unknown.status_code == 200
            and unknown.json().get("ok") is False
            and unknown.json().get("outcome") == "unknown_token",
            f"unknown token rejected ({unknown.status_code})",
        )
        case = self.case(repository, number) or case
        self.check(
            case["state"] == "AWAITING_REMEDIATION_APPROVAL"
            and case["approval"]["decision"] == "PENDING",
            "case still awaiting approval after every rejected attempt",
        )
        first = self.slack_click(token, action_ts="42.000001")
        second = self.slack_click(token, action_ts="42.000001")
        third = self.slack_click(token, action_id=ACTION_REJECT, action_ts="43.000001")
        self.check(first.json().get("outcome") == "approved", "first click approves")
        self.check(second.json().get("outcome") == "duplicate", "identical click is a duplicate")
        self.check(
            third.json().get("outcome") == "already_decided" and third.status_code == 200,
            "later reject click reports the existing decision",
        )
        case = self.wait_approval(repository, number, delivery_status="LABEL_APPLIED")
        labels = [r for r in case["outbox"] if r["kind"] == "apply_remediation_label"]
        self.check(len(labels) == 1, "exactly one label operation queued despite repeated clicks")
        self.summarize(case)

    def run_expired_token(self) -> None:
        repository, number, case = self.open_candidate(PHASE3_SCENARIOS["expired-token"])
        case = self.wait_approval(repository, number, notification_status="SENT")
        token = self.slack_token(repository, number)
        approval_id = case["approval"]["id"]
        response = self.client.post(
            f"/operator/approvals/{approval_id}/expire", headers=self.operator
        )
        self.check(
            response.status_code == 200, f"operator expired the token ({response.status_code})"
        )
        click = self.slack_click(token)
        self.check(
            click.status_code == 200
            and click.json().get("ok") is False
            and click.json().get("outcome") == "expired_token",
            f"expired token rejected ({click.status_code})",
        )
        case = self.wait_approval(repository, number, decision="EXPIRED")
        self.check(case["state"] == "AWAITING_REMEDIATION_APPROVAL", "case untouched by expiry")
        self.wait_for(
            repository,
            number,
            lambda c: all(r["status"] == "SENT" for r in c["outbox"]),
            "Slack expiry update sent",
        )
        blocks = json.dumps(self.slack_message(repository, number)["blocks"])
        self.check(ACTION_APPROVE not in blocks and "Expired" in blocks, "Slack shows expired")
        self.summarize(case)

    def run_slack_delivery_failure(self) -> None:
        repository, number, case = self.open_candidate(PHASE3_SCENARIOS["slack-delivery-failure"])
        if (case.get("approval") or {}).get("notification_status") == "SENT":
            print("  notification already delivered in a previous run")
            self.summarize(case)
            return
        case = self.wait_approval(repository, number, notification_status="FAILED")
        self.check(case["state"] == "AWAITING_REMEDIATION_APPROVAL", "triage result survived")
        self.retry_failed_outbox(case, "remediation_approval_requested")
        case = self.wait_approval(repository, number, notification_status="SENT")
        self.check(
            bool(self.slack_token(repository, number)), "Slack message delivered after retry"
        )
        self.summarize(case)

    def run_github_label_failure(self) -> None:
        fixture = PHASE3_SCENARIOS["github-label-failure"]
        repository, number, case = self.open_candidate(fixture)
        if case["state"] in PHASE3_POST_LABEL_STATES:
            print(f"  already {case['state']} from a previous run")
            self.summarize(case)
            return
        self.wait_approval(repository, number, notification_status="SENT")
        token = self.slack_token(repository, number)
        response = self.slack_click(token)
        self.check(response.json().get("outcome") == "approved", "signed approval accepted")
        case = self.wait_state(repository, number, "APPROVAL_DELIVERY_FAILED")
        self.check(
            case["approval"]["decision"] == "APPROVED"
            and case["approval"]["delivery_status"] == "FAILED",
            "approval retained while GitHub delivery is marked failed",
        )
        again = self.slack_click(token, action_ts="7.000001")
        self.check(
            again.json().get("outcome") == "already_decided", "no re-approval is needed or accepted"
        )
        self.retry_failed_outbox(case, "apply_remediation_label")
        case = self.wait_approval(repository, number, delivery_status="LABEL_APPLIED")
        self.check(
            case["state"] == "APPROVAL_DELIVERY_FAILED",
            "label applied on retry; state still waits for the signed GitHub webhook",
        )
        webhook = self.label_webhook(fixture)
        print(f"  labeled webhook: {webhook.status_code} {webhook.json()}")
        case = self.wait_state(repository, number, *PHASE3_POST_LABEL_STATES)
        self.check_no_paid_remediation(case)
        self.summarize(case)

    def check_no_paid_remediation(self, case: dict[str, Any]) -> None:
        remediation = [a for a in case["attempts"] if a["kind"] == "REMEDIATION"]
        self.check(
            all(a["create_state"] != "CREATED" and a["devin_status"] is None for a in remediation),
            f"no probe registered: {case['state']} without a paid remediation session",
        )

    def run_phase3(self, scenario: str) -> None:
        print(f"== phase3 {scenario}")
        try:
            getattr(self, "run_" + scenario.replace("-", "_"))()
        except (SimulationError, httpx.HTTPError) as exc:
            self.check(False, f"{scenario}: {exc}")

    # ------------------------------------------------------------------ phase 4
    def phase4_payload(self, number: int, labels: list[str] | None = None) -> dict[str, Any]:
        payload = self.load(PHASE3_SCENARIOS["approve"])
        payload["issue"]["number"] = number
        payload["issue"]["html_url"] = f"https://github.com/apache/superset/issues/{number}"
        payload["issue"]["title"] = f"Remediation fixture #{number}"
        if labels is not None:
            payload["issue"]["labels"] = [{"name": name} for name in labels]
        return payload

    def label_event(self, payload: dict[str, Any], label: str) -> httpx.Response:
        event = json.loads(json.dumps(payload))
        event["action"] = "labeled"
        event["label"] = {"name": label}
        event["issue"]["labels"].append({"name": label})
        return self.github_post(event)

    def remediation_attempts(self, case: dict[str, Any]) -> list[dict[str, Any]]:
        return list((case.get("remediation") or {}).get("attempts") or [])

    def probe_runs(self, case: dict[str, Any]) -> list[tuple[str, str, Any]]:
        runs: list[tuple[str, str, Any]] = []
        for attempt in self.remediation_attempts(case):
            for run in attempt.get("probe_executions") or []:
                runs.append((run["target"], run["verdict"], run.get("exit_code")))
        return runs

    def approve_fixture(self, number: int) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Phase 3 in full for a Phase 4 fixture issue: open, Slack-approve, label webhook."""
        payload = self.phase4_payload(number)
        repository = payload["repository"]["full_name"]
        existing = self.case(repository, number)
        if existing is not None:
            print(f"  {repository}#{number} already exists in state {existing['state']}")
            return repository, payload, existing
        response = self.github_post(payload)
        print(f"  opened {repository}#{number}: {response.status_code} {response.json()}")
        self.wait_approval(repository, number, notification_status="SENT")
        token = self.slack_token(repository, number)
        click = self.slack_click(token)
        self.check(click.json().get("outcome") == "approved", "signed Slack approval accepted")
        case = self.wait_approval(repository, number, delivery_status="LABEL_APPLIED")
        self.check(
            not self.remediation_attempts(case),
            "approval alone creates no remediation attempt or session",
        )
        webhook = self.label_event(payload, "devin:remediate")
        self.check(
            webhook.json().get("accepted") is True, "signed devin:remediate webhook accepted"
        )
        return repository, payload, case

    def settle(self, repository: str, number: int) -> dict[str, Any]:
        return self.wait_for(
            repository,
            number,
            lambda c: c["state"] in REMEDIATION_RESTING_STATES,
            "remediation to settle",
            timeout=240,
        )

    def summarize_remediation(self, case: dict[str, Any]) -> None:
        remediation = case.get("remediation") or {}
        print(
            "  remediation",
            json.dumps(
                {
                    "state": case["state"],
                    "ci_status": case.get("ci_status"),
                    "failure_reason": case.get("failure_reason"),
                    "ready_for_human_review": remediation.get("ready_for_human_review"),
                    "actions": remediation.get("actions"),
                    "attempts": [
                        {
                            "status": a["status"],
                            "session": a.get("devin_session_id"),
                            "acus": a.get("devin_acus_consumed"),
                            "pr": a.get("pr_url"),
                            "head": (a.get("head_sha") or "")[:12],
                            "stage": a.get("failure_stage"),
                            "class": a.get("failure_class"),
                        }
                        for a in remediation.get("attempts") or []
                    ],
                    "probes": self.probe_runs(case),
                    "outbox": [
                        (r["kind"], r["status"], r["attempts_count"]) for r in case["outbox"]
                    ],
                }
            ),
        )

    def run_remediate(self) -> None:
        number = PHASE4_SCENARIOS["remediate"]
        repository, payload, _ = self.approve_fixture(number)
        case = self.settle(repository, number)
        self.check(case["state"] == "CI_PASSED", f"reached CI_PASSED ({case['state']})")
        attempts = self.remediation_attempts(case)
        self.check(len(attempts) == 1, f"exactly one paid remediation attempt ({len(attempts)})")
        runs = self.probe_runs(case)
        self.check(
            [(t, v) for t, v, _ in runs] == [("BASE", "MATCHED"), ("HEAD", "MATCHED")],
            f"BASE reproduced before the session, HEAD verified after the PR ({runs})",
        )
        self.check(
            bool(attempts and attempts[0].get("pr_url") and attempts[0].get("head_sha")),
            "PR URL and GitHub-corroborated head SHA recorded",
        )
        self.check(
            bool(attempts and attempts[0].get("ci_snapshots")),
            "CI snapshot recorded for the verified head SHA",
        )
        self.check(
            (case.get("remediation") or {}).get("ready_for_human_review") is True,
            "ready for human review (no auto-merge, no auto-close)",
        )
        duplicate = self.label_event(payload, "devin:remediate")
        print(f"  duplicate label webhook: {duplicate.status_code} {duplicate.json()}")
        time.sleep(2)
        again = self.case(repository, number) or case
        self.check(
            len(self.remediation_attempts(again)) == 1 and again["state"] == "CI_PASSED",
            "duplicate devin:remediate webhook creates no second attempt",
        )
        self.wait_for(
            repository,
            number,
            lambda c: all(r["status"] == "SENT" for r in c["outbox"]),
            "all Slack remediation updates sent",
        )
        blocks = json.dumps(self.slack_message(repository, number)["blocks"])
        self.check("human review" in blocks.lower(), "Slack shows ready-for-human-review")
        self.summarize_remediation(again)

    def run_remediate_base_passes(self) -> None:
        number = PHASE4_SCENARIOS["remediate-base-passes"]
        repository, _, _ = self.approve_fixture(number)
        case = self.settle(repository, number)
        self.check(
            case["state"] == "REMEDIATION_HUMAN_BLOCKED"
            and "no_change_needed" in (case.get("failure_reason") or ""),
            f"BASE already passes -> no_change_needed for a human ({case['state']})",
        )
        self.check(not self.remediation_attempts(case), "zero attempts, zero Devin sessions")
        snapshots = (case.get("remediation") or {}).get("probe_snapshots") or []
        self.check(len(snapshots) == 1, "immutable probe snapshot persisted at dispatch")
        self.summarize_remediation(case)

    def run_remediate_probe_infra(self) -> None:
        number = PHASE4_SCENARIOS["remediate-probe-infra"]
        repository, _, _ = self.approve_fixture(number)
        case = self.settle(repository, number)
        self.check(
            case["state"] == "PROBE_INFRASTRUCTURE_BLOCKED",
            f"runner unavailable is never success ({case['state']})",
        )
        self.check(not self.remediation_attempts(case), "zero attempts, zero Devin sessions")
        unauth = self.client.post(f"/operator/cases/{case['id']}/retry-probe")
        self.check(unauth.status_code in {401, 303}, "probe retry requires authentication")
        retry = self.client.post(f"/operator/cases/{case['id']}/retry-probe", headers=self.operator)
        self.check(
            retry.status_code == 200, f"operator BASE probe retry accepted ({retry.status_code})"
        )
        case = self.settle(repository, number)
        self.check(
            case["state"] == "PROBE_INFRASTRUCTURE_BLOCKED" and not self.remediation_attempts(case),
            "retry re-ran BASE only; still blocked at zero ACUs while the runtime is missing",
        )
        self.summarize_remediation(case)

    def run_remediate_head_fails(self) -> None:
        number = PHASE4_SCENARIOS["remediate-head-fails"]
        repository, _, _ = self.approve_fixture(number)
        case = self.settle(repository, number)
        attempts = self.remediation_attempts(case)
        self.check(
            case["state"] == "REMEDIATION_FAILED"
            and bool(attempts)
            and attempts[-1].get("failure_stage") == "probe_head",
            f"HEAD probe failure is a verification failure ({case['state']})",
        )
        self.check(len(attempts) == 1, "no automatic repair chain: still one attempt")
        runs = self.probe_runs(case)
        self.check(
            [(t, v) for t, v, _ in runs] == [("BASE", "MATCHED"), ("HEAD", "MISMATCHED")],
            f"identical probe ran at BASE then HEAD ({runs})",
        )
        retry = self.client.post(f"/operator/cases/{case['id']}/retry-probe", headers=self.operator)
        self.check(
            retry.status_code in {200, 409}
            and (self.case(repository, number) or case)["state"] == "REMEDIATION_FAILED",
            "a genuine probe verdict cannot be retried as an infrastructure failure",
        )
        self.summarize_remediation(case)

    def run_remediate_forbidden_files(self) -> None:
        number = PHASE4_SCENARIOS["remediate-forbidden-files"]
        repository, _, _ = self.approve_fixture(number)
        case = self.settle(repository, number)
        attempts = self.remediation_attempts(case)
        self.check(
            case["state"] == "REMEDIATION_FAILED"
            and bool(attempts)
            and attempts[-1].get("failure_stage") == "pr",
            f"PR touching probes/workflows fails closed ({case['state']})",
        )
        self.check(
            all(t == "BASE" for t, _, _ in self.probe_runs(case)),
            "HEAD probe never ran for a rejected PR",
        )
        self.summarize_remediation(case)

    def run_remediate_ci_failed(self) -> None:
        number = PHASE4_SCENARIOS["remediate-ci-failed"]
        repository, _, _ = self.approve_fixture(number)
        case = self.settle(repository, number)
        self.check(
            case["state"] == "CI_FAILED" and case.get("ci_status") == "failure",
            f"failed required check for the verified head ({case['state']})",
        )
        self.check(len(self.remediation_attempts(case)) == 1, "no automatic regression-fix chain")
        retry = self.client.post(f"/operator/cases/{case['id']}/retry-ci", headers=self.operator)
        self.check(retry.status_code == 200, f"operator CI re-sync accepted ({retry.status_code})")
        case = self.settle(repository, number)
        self.check(
            case["state"] == "CI_FAILED" and len(self.remediation_attempts(case)) == 1,
            "CI re-sync re-reads checks on the same attempt; still failed, no new session",
        )
        self.summarize_remediation(case)

    def run_remediate_uncertain_create(self) -> None:
        number = PHASE4_SCENARIOS["remediate-uncertain-create"]
        repository, _, _ = self.approve_fixture(number)
        case = self.settle(repository, number)
        attempts = self.remediation_attempts(case)
        self.check(
            len(attempts) == 1 and bool(attempts[0].get("devin_session_id")),
            "uncertain POST reconciled by exact tag: one session attached, no second POST",
        )
        self.check(case["state"] == "CI_PASSED", f"reconciled session completed ({case['state']})")
        self.summarize_remediation(case)

    def run_remediate_unlabeled_intake(self) -> None:
        number = PHASE4_SCENARIOS["remediate-unlabeled-intake"]
        payload = self.phase4_payload(number, labels=["bug"])
        repository = payload["repository"]["full_name"]
        if self.case(repository, number) is None:
            response = self.github_post(payload)
            print(
                f"  opened unlabeled {repository}#{number}: "
                f"{response.status_code} {response.json()}"
            )
            self.check(
                response.json().get("accepted") is True,
                "unlabeled issues/opened accepted with GITHUB_REQUIRED_LABEL unset (default)",
            )
        case = self.wait_state(repository, number, "AWAITING_REMEDIATION_APPROVAL")
        self.check(
            case["state"] == "AWAITING_REMEDIATION_APPROVAL",
            "unlabeled issue evaluated by eligibility filter and triaged to approval",
        )
        inert = self.label_event(payload, "enhancement")
        print(f"  unrelated label webhook: {inert.status_code} {inert.json()}")
        premature = self.label_event(payload, "devin:remediate")
        print(f"  premature devin:remediate webhook: {premature.status_code} {premature.json()}")
        time.sleep(2)
        case = self.case(repository, number) or case
        self.check(
            case["state"] == "AWAITING_REMEDIATION_APPROVAL"
            and not self.remediation_attempts(case),
            "neither an unrelated label nor devin:remediate without a Slack approval remediates",
        )
        self.summarize(case)

    def run_phase4(self, scenario: str) -> None:
        print(f"== phase4 {scenario}")
        try:
            getattr(self, "run_" + scenario.replace("-", "_"))()
        except (SimulationError, httpx.HTTPError) as exc:
            self.check(False, f"{scenario}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=[*SCENARIOS, *PHASE3_SCENARIOS, *PHASE4_SCENARIOS, "all", "phase3", "phase4"],
        default="all",
    )
    parser.add_argument("--delivery-id", default=None, help="(ignored; kept for compatibility)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--bad-signature", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    base_url = _environment("BASE_URL", "http://localhost:8000")
    ingest: list[str] = []
    phase3: list[str] = []
    phase4: list[str] = []
    if args.scenario == "all":
        ingest, phase3, phase4 = list(SCENARIOS), list(PHASE3_SCENARIOS), list(PHASE4_SCENARIOS)
    elif args.scenario == "phase3":
        phase3 = list(PHASE3_SCENARIOS)
    elif args.scenario == "phase4":
        phase4 = list(PHASE4_SCENARIOS)
    elif args.scenario in SCENARIOS:
        ingest = [args.scenario]
    elif args.scenario in PHASE3_SCENARIOS:
        phase3 = [args.scenario]
    else:
        phase4 = [args.scenario]
    with httpx.Client(base_url=base_url, timeout=10) as client:
        simulator = Simulator(client)
        for scenario in ingest:
            simulator.run_ingest(
                scenario, repeat=args.repeat, bad_signature=args.bad_signature, wait=args.wait
            )
        for scenario in phase3:
            simulator.run_phase3(scenario)
        for scenario in phase4:
            simulator.run_phase4(scenario)
        if simulator.failures:
            print(f"{len(simulator.failures)} check(s) failed:", *simulator.failures, sep="\n  ")
            sys.exit(1)
        if phase3 or phase4:
            print("all phase 3/4 checks passed")


if __name__ == "__main__":
    main()
