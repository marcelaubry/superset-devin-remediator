"""Mapping of Devin v3 session ``status``/``status_detail`` to worker dispositions.

Reference: https://docs.devin.ai/api-reference (SessionResponse ``status`` and
``status_detail`` enums). Any value outside the documented sets is UNKNOWN so
the worker keeps observing the session instead of guessing.
"""

from dataclasses import dataclass
from enum import StrEnum

LIVE_STATUSES = frozenset({"new", "claimed", "running", "resuming"})
TERMINAL_STATUSES = frozenset({"exit", "error"})
SUSPENDED_STATUS = "suspended"
KNOWN_STATUSES = LIVE_STATUSES | TERMINAL_STATUSES | {SUSPENDED_STATUS}

FINISHED_DETAIL = "finished"
WORKING_DETAIL = "working"
WAITING_DETAILS = frozenset({"waiting_for_user", "waiting_for_approval"})
# Suspension reasons that mean the session cannot legitimately continue on its own
# and that spending more would be pointless.
BILLING_DETAILS = frozenset(
    {
        "usage_limit_exceeded",
        "out_of_credits",
        "out_of_quota",
        "no_quota_allocation",
        "payment_declined",
        "org_usage_limit_exceeded",
        "user_usage_limit_exceeded",
        "total_session_limit_exceeded",
    }
)
# Suspension reasons where a human may resume the session; the worker must not
# spawn a replacement.
RESUMABLE_SUSPENSION_DETAILS = frozenset({"inactivity", "user_request"})
ERROR_DETAIL = "error"
KNOWN_DETAILS = (
    {FINISHED_DETAIL, WORKING_DETAIL, ERROR_DETAIL}
    | WAITING_DETAILS
    | BILLING_DETAILS
    | RESUMABLE_SUSPENSION_DETAILS
)


class Disposition(StrEnum):
    LIVE = "LIVE"
    FINISHED = "FINISHED"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class StatusMapping:
    disposition: Disposition
    reason: str
    # True when the session is gone/stopped remotely so no DELETE is needed.
    remote_terminal: bool = False


def classify(status: str | None, status_detail: str | None) -> StatusMapping:
    normalized_status = (status or "").strip().lower()
    detail = (status_detail or "").strip().lower() or None

    if normalized_status in TERMINAL_STATUSES:
        if normalized_status == "exit":
            return StatusMapping(
                Disposition.FINISHED, f"session exited ({detail or 'no detail'})", True
            )
        return StatusMapping(Disposition.FAILED, f"session errored ({detail or 'no detail'})", True)

    if normalized_status == SUSPENDED_STATUS:
        if detail in BILLING_DETAILS:
            return StatusMapping(Disposition.FAILED, f"session suspended: {detail}", True)
        if detail in RESUMABLE_SUSPENSION_DETAILS:
            return StatusMapping(
                Disposition.WAITING_FOR_HUMAN, f"session suspended: {detail}", True
            )
        if detail == ERROR_DETAIL:
            return StatusMapping(Disposition.FAILED, "session suspended: error", True)
        if detail in WAITING_DETAILS:
            return StatusMapping(
                Disposition.WAITING_FOR_HUMAN, f"session suspended: {detail}", True
            )
        return StatusMapping(
            Disposition.UNKNOWN, f"session suspended with unknown detail {detail!r}", True
        )

    if normalized_status in LIVE_STATUSES:
        if detail is None or detail == WORKING_DETAIL:
            return StatusMapping(Disposition.LIVE, f"session {normalized_status}")
        if detail == FINISHED_DETAIL:
            return StatusMapping(Disposition.FINISHED, "session finished")
        if detail in WAITING_DETAILS:
            return StatusMapping(Disposition.WAITING_FOR_HUMAN, f"session {detail}")
        if detail in BILLING_DETAILS:
            return StatusMapping(Disposition.FAILED, f"session halted: {detail}")
        if detail == ERROR_DETAIL:
            return StatusMapping(Disposition.FAILED, "session reported error")
        if detail in RESUMABLE_SUSPENSION_DETAILS:
            return StatusMapping(Disposition.WAITING_FOR_HUMAN, f"session {detail}")
        return StatusMapping(
            Disposition.UNKNOWN,
            f"session {normalized_status} with unknown detail {detail!r}",
        )

    return StatusMapping(
        Disposition.UNKNOWN,
        f"unknown session status {status!r} (detail {detail!r})",
    )
