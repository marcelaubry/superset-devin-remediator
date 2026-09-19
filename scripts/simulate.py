"""Drive the running stack through its real HTTP endpoints only.

Phase 1/2 scenarios post signed GitHub `issues.opened` webhooks and wait for the case to
settle. Phase 3 scenarios continue from a validated remediation candidate: read the fake
Slack channel via the operator API, submit correctly (or deliberately incorrectly) signed
Slack interaction payloads to POST /webhooks/slack/actions, let the worker process the
outbox, post the signed GitHub `labeled` webhook and wait for REMEDIATION_APPROVED.
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
    "approve": "issue_good_candidate.json",  # 4213: full happy path to REMEDIATION_APPROVED
    "reject": "issue_approval_reject.json",  # 4219
    "slack-negative": "issue_approval_negative.json",  # 4217: bad sig / stale / unauthorized / dup
    "expired-token": "issue_approval_expired.json",  # 4216
    "slack-delivery-failure": "issue_slack_failure.json",  # 4699: fake Slack fails, retry
    "github-label-failure": "issue_label_failure.json",  # 4688: fake GitHub fails, retry
}

TERMINAL_STATES = {
    "AWAITING_REMEDIATION_APPROVAL",
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
        if case["state"] == "REMEDIATION_APPROVED":
            print("  already REMEDIATION_APPROVED from a previous run")
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
        case = self.wait_state(repository, number, "REMEDIATION_APPROVED")
        self.check(case["approval"]["delivery_status"] == "CONFIRMED", "label confirmed by webhook")
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
            outsider.status_code == 403 and outsider.json().get("outcome") == "unauthorized",
            f"unauthorized approver rejected ({outsider.status_code})",
        )
        unknown = self.slack_click("not-a-real-token")
        self.check(unknown.status_code == 404, f"unknown token rejected ({unknown.status_code})")
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
            click.status_code == 410 and click.json().get("outcome") == "expired_token",
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
        if case["state"] == "REMEDIATION_APPROVED":
            print("  already REMEDIATION_APPROVED from a previous run")
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
        case = self.wait_state(repository, number, "REMEDIATION_APPROVED")
        self.summarize(case)

    def run_phase3(self, scenario: str) -> None:
        print(f"== phase3 {scenario}")
        try:
            getattr(self, "run_" + scenario.replace("-", "_"))()
        except (SimulationError, httpx.HTTPError) as exc:
            self.check(False, f"{scenario}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario", choices=[*SCENARIOS, *PHASE3_SCENARIOS, "all", "phase3"], default="all"
    )
    parser.add_argument("--delivery-id", default=None, help="(ignored; kept for compatibility)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--bad-signature", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    base_url = _environment("BASE_URL", "http://localhost:8000")
    if args.scenario == "all":
        ingest, phase3 = list(SCENARIOS), list(PHASE3_SCENARIOS)
    elif args.scenario == "phase3":
        ingest, phase3 = [], list(PHASE3_SCENARIOS)
    elif args.scenario in SCENARIOS:
        ingest, phase3 = [args.scenario], []
    else:
        ingest, phase3 = [], [args.scenario]
    with httpx.Client(base_url=base_url, timeout=10) as client:
        simulator = Simulator(client)
        for scenario in ingest:
            simulator.run_ingest(
                scenario, repeat=args.repeat, bad_signature=args.bad_signature, wait=args.wait
            )
        for scenario in phase3:
            simulator.run_phase3(scenario)
        if simulator.failures:
            print(f"{len(simulator.failures)} check(s) failed:", *simulator.failures, sep="\n  ")
            sys.exit(1)
        if phase3:
            print("all phase 3 checks passed")


if __name__ == "__main__":
    main()
