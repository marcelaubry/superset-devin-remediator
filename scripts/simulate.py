import argparse
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import httpx

SCENARIOS = {
    "good": "issue_good_candidate.json",
    "needs-scoping": "issue_needs_scoping.json",
    "deterministic": "issue_deterministic.json",
    "human-led": "issue_human_led.json",
    "failure": "issue_fake_failure.json",
    "blocked": "issue_human_blocked.json",
    "wrong-repo": "issue_wrong_repo.json",
    "missing-label": "issue_missing_label.json",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--delivery-id", default=None)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--bad-signature", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "change-me")
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    selected = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    with httpx.Client(base_url=base_url, timeout=10) as client:
        for scenario in selected:
            payload = json.loads(
                (Path(__file__).parents[1] / "fixtures/github" / SCENARIOS[scenario]).read_text()
            )
            body = json.dumps(payload, separators=(",", ":")).encode()
            signature = (
                "sha256=" + hashlib.sha256(body).hexdigest()
                if args.bad_signature
                else "sha256=" + __import__("hmac").new(secret.encode(), body, "sha256").hexdigest()
            )
            delivery_id = args.delivery_id or str(uuid.uuid4())
            for index in range(args.repeat):
                response = client.post(
                    "/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Delivery": delivery_id,
                        "X-GitHub-Event": "issues",
                        "X-Hub-Signature-256": signature,
                    },
                )
                print(scenario, index + 1, response.status_code, response.json())
            if (
                args.wait
                and response.json().get("accepted")
                and payload.get("issue", {}).get("number")
            ):
                repository = payload["repository"]["full_name"]
                issue_number = payload["issue"]["number"]
                for _ in range(40):
                    result = client.get(
                        f"/api/cases/{repository}/{issue_number}",
                        headers={
                            "Authorization": f"Bearer {os.getenv('OPERATOR_TOKEN', 'change-me')}"
                        },
                    )
                    if result.status_code == 200 and result.json().get("state") in {
                        "CI_PASSED",
                        "FAILED",
                        "HUMAN_BLOCKED",
                        "POLICY_REJECTED",
                        "CANCELLED",
                    }:
                        print("timeline", result.json())
                        break
                    time.sleep(0.25)


if __name__ == "__main__":
    main()
