"""Block Kit rendering for remediation approval messages.

Every untrusted string (issue title, Devin output) is escaped for mrkdwn and truncated to
Slack's documented limits: 150 chars for header text, 3000 for section text, 2000 for a
button value, 50 blocks per message. Secrets and full issue bodies are never rendered.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

HEADER_MAX = 150
SECTION_MAX = 3000
BUTTON_TEXT_MAX = 75
BUTTON_VALUE_MAX = 2000
CONTEXT_MAX = 3000
MAX_BLOCKS = 50
MAX_LIST_ITEMS = 8
ITEM_MAX = 300

ACTION_APPROVE = "approve_remediation"
ACTION_REJECT = "reject_remediation"
BLOCK_ID_ACTIONS = "remediation_decision"
BLOCK_ID_REASON = "rejection_reason"
ACTION_REASON = "rejection_reason"
# Optional, modal-free rejection reason: a select in the message whose current value Slack
# echoes back in `state.values` of the block_actions payload when a button is clicked.
REJECTION_REASONS: tuple[tuple[str, str], ...] = (
    ("not_a_bug", "Not a bug / works as intended"),
    ("needs_human", "Needs human design or product decision"),
    ("too_risky", "Change is too risky to automate"),
    ("duplicate", "Duplicate of another issue"),
    ("insufficient_evidence", "Triage evidence is insufficient"),
)


class ApprovalMessageStatus(StrEnum):
    AWAITING = "awaiting_approval"
    APPROVED_PENDING_GITHUB = "approved_github_dispatch_pending"
    LABEL_APPLIED = "github_label_applied"
    REJECTED = "rejected"
    DELIVERY_FAILED = "delivery_failed"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


STATUS_LINES: dict[ApprovalMessageStatus, str] = {
    ApprovalMessageStatus.AWAITING: ":hourglass_flowing_sand: *Awaiting approval*",
    ApprovalMessageStatus.APPROVED_PENDING_GITHUB: (
        ":white_check_mark: *Approved* — applying `devin:remediate` on GitHub…"
    ),
    ApprovalMessageStatus.LABEL_APPLIED: (
        ":rocket: *Approved* — `devin:remediate` applied; GitHub is the dispatch authority"
    ),
    ApprovalMessageStatus.REJECTED: ":no_entry: *Rejected* — no remediation will be started",
    ApprovalMessageStatus.DELIVERY_FAILED: (
        ":warning: *Approved, but GitHub delivery failed* — an operator must retry; "
        "no re-approval is needed"
    ),
    ApprovalMessageStatus.EXPIRED: ":clock3: *Expired* — the approval window closed",
    ApprovalMessageStatus.SUPERSEDED: (
        ":arrows_counterclockwise: *Superseded* — the issue was re-triaged; use the newer message"
    ),
}


def escape_mrkdwn(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _safe(text: object, limit: int) -> str:
    return truncate(escape_mrkdwn(str(text)), limit)


def _bullets(items: list[Any], *, empty: str) -> str:
    cleaned = [_safe(item, ITEM_MAX) for item in items[:MAX_LIST_ITEMS] if str(item).strip()]
    if not cleaned:
        return f"_{empty}_"
    extra = len(items) - len(cleaned)
    lines = [f"• {item}" for item in cleaned]
    if extra > 0:
        lines.append(f"_… {extra} more_")
    return "\n".join(lines)


def _section(text: str, *, block_id: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": truncate(text, SECTION_MAX)},
    }
    if block_id:
        block["block_id"] = block_id
    return block


def _link(url: str | None, label: str) -> str:
    if not url or not url.startswith(("https://", "http://")):
        return _safe(label, ITEM_MAX)
    return f"<{escape_mrkdwn(url)}|{_safe(label, ITEM_MAX)}>"


@dataclass(frozen=True)
class ApprovalMessageInput:
    repository: str
    issue_number: int
    issue_title: str
    issue_url: str
    devin_session_url: str | None
    dashboard_url: str
    triage: dict[str, Any]
    action_token: str | None
    status: ApprovalMessageStatus
    decision_note: str | None = None


def fallback_text(message: ApprovalMessageInput) -> str:
    return truncate(
        f"Remediation approval for {message.repository}#{message.issue_number}: "
        f"{message.issue_title} [{message.status}]",
        SECTION_MAX,
    )


def build_approval_blocks(message: ApprovalMessageInput) -> list[dict[str, Any]]:
    triage = message.triage
    probe: dict[str, Any] = triage["probe"] if isinstance(triage.get("probe"), dict) else {}
    risk: dict[str, Any] = triage["risk"] if isinstance(triage.get("risk"), dict) else {}
    header = truncate(
        f"Remediation approval: {message.repository}#{message.issue_number}", HEADER_MAX
    )
    status_text = STATUS_LINES[message.status]
    if message.decision_note:
        status_text += f"\n{_safe(message.decision_note, ITEM_MAX)}"
    issue_link = _link(message.issue_url, f"#{message.issue_number} {message.issue_title}")
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": False}},
        _section(status_text, block_id="status"),
        _section(f"*Issue:* {issue_link}\n*Repository:* `{_safe(message.repository, ITEM_MAX)}`"),
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity*\n{_safe(triage.get('severity', '?'), 50)}"},
                {"type": "mrkdwn", "text": f"*Priority*\n{_safe(triage.get('priority', '?'), 50)}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Confidence*\n{_confidence(triage.get('confidence'))}",
                },
                {"type": "mrkdwn", "text": f"*Risk*\n{_safe(risk.get('level', '?'), 50)}"},
            ],
        },
        _section(
            f"*Reproduction summary*\n{_safe(triage.get('summary', ''), 1500)}\n\n"
            f"*Evidence*\n{_bullets(list(triage.get('evidence') or []), empty='none recorded')}"
        ),
        _section(
            f"*Affected files*\n"
            f"{_bullets(list(triage.get('affected_files') or []), empty='none listed')}\n\n"
            f"*Estimated scope*\n{_safe(triage.get('scope', ''), 1000)}\n\n"
            f"*Risk notes*\n{_safe(risk.get('notes', ''), 600)}"
        ),
        _section(
            f"*Proposed immutable probe* (expected base exit code "
            f"`{_safe(probe.get('expected_base_exit_code', '?'), 5)}`)\n"
            f"```{_safe(probe.get('command', ''), 1500)}```\n"
            f"*Focused tests*\n"
            f"{_bullets(list(triage.get('focused_tests') or []), empty='none proposed')}"
        ),
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": truncate(
                        f"Devin triage session: {_link(message.devin_session_url, 'open session')}"
                        f" · Evidence/dashboard: {_link(message.dashboard_url, 'open dashboard')}",
                        CONTEXT_MAX,
                    ),
                }
            ],
        },
    ]
    actions: list[dict[str, Any]] = []
    if message.status is ApprovalMessageStatus.AWAITING and message.action_token:
        value = truncate(message.action_token, BUTTON_VALUE_MAX)
        actions.extend(
            [
                {
                    "type": "button",
                    "action_id": ACTION_APPROVE,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve remediation"},
                    "value": value,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Approve remediation?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "This applies `devin:remediate` on GitHub. Nothing starts a "
                                "Devin session from Slack."
                            ),
                        },
                        "confirm": {"type": "plain_text", "text": "Approve"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "action_id": ACTION_REJECT,
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": value,
                },
            ]
        )
    if message.issue_url.startswith("https://"):
        actions.append(
            {
                "type": "button",
                "action_id": "view_issue",
                "text": {"type": "plain_text", "text": "View issue"},
                "url": truncate(message.issue_url, BUTTON_VALUE_MAX),
            }
        )
    if message.dashboard_url.startswith(("https://", "http://")):
        actions.append(
            {
                "type": "button",
                "action_id": "view_dashboard",
                "text": {"type": "plain_text", "text": "View evidence/dashboard"},
                "url": truncate(message.dashboard_url, BUTTON_VALUE_MAX),
            }
        )
    if message.status is ApprovalMessageStatus.AWAITING and message.action_token:
        blocks.append(
            {
                "type": "actions",
                "block_id": BLOCK_ID_REASON,
                "elements": [
                    {
                        "type": "static_select",
                        "action_id": ACTION_REASON,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Rejection reason (optional)",
                        },
                        "options": [
                            {"text": {"type": "plain_text", "text": label}, "value": value}
                            for value, label in REJECTION_REASONS
                        ],
                    }
                ],
            }
        )
    if actions:
        blocks.append({"type": "actions", "block_id": BLOCK_ID_ACTIONS, "elements": actions})
    return blocks[:MAX_BLOCKS]


def _confidence(value: object) -> str:
    if isinstance(value, int | float):
        return f"{float(value):.0%}"
    return "?"
