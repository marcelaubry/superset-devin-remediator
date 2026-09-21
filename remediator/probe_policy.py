"""Acceptance-probe policy (`PROBE_POLICY`).

`required` (default) keeps the original production behaviour: an approved immutable probe
must exist, reproduce the defect at the pinned base SHA and pass unchanged at the PR head.
`if_available` runs exactly that verification whenever a probe is registered, and otherwise
lets a case whose Slack approval and remediation label are already confirmed proceed to the
bounded session. Nothing about the rest of the pipeline changes: PR validation and
exact-head CI stay mandatory, no probe evidence is written, and the case reports
`probe_status=not_configured` rather than any claim of behavioural verification.

`CANARY_PROBE_OVERRIDE` (the removed `LIVE_CANARY_ALLOW_MISSING_PROBE` escape hatch) lives
on only as historical evidence on cases that ran under it; nothing here writes it.
"""

from enum import StrEnum

# Historical transition/audit label. Never written for new cases; still rendered.
LEGACY_CANARY_PROBE_OVERRIDE = "CANARY_PROBE_OVERRIDE"
LEGACY_CANARY_OVERRIDE_WARNING = (
    "CANARY OVERRIDE: remediation proceeded without base/head acceptance-probe "
    "verification. PR and exact-head CI were validated, but behavioral correctness "
    "requires human review."
)

PROBE_NOT_CONFIGURED = "PROBE_NOT_CONFIGURED"

# Neutral status shown wherever a probe verdict would otherwise appear.
PROBE_NOT_CONFIGURED_NOTE = (
    "Acceptance probe: not configured. PR structure and exact-head CI will still be "
    "independently validated."
)

PROBE_NOT_CONFIGURED_REASON = "no acceptance probe registered; PROBE_POLICY=if_available"

PROBE_POLICY_ACTOR = "config:PROBE_POLICY"

# Prefix of the `cases.failure_reason` written when dispatch blocks on a missing probe.
MISSING_PROBE_BLOCK_PREFIX = "approved probe unavailable:"

# Substring the registry raises only when nothing is registered for the issue; any other
# registry error (malformed manifest, hash mismatch, unreadable script) keeps blocking.
_MISSING_PROBE_MARKER = "no approved probe registered"


class ProbeStatus(StrEnum):
    """What acceptance-probe evidence an attempt has."""

    VERIFIED = "verified"
    NOT_CONFIGURED = "not_configured"


def is_missing_probe_error(message: str) -> bool:
    return _MISSING_PROBE_MARKER in message


def is_missing_probe_block(failure_reason: str | None) -> bool:
    """True when a REMEDIATION_HUMAN_BLOCKED case is blocked *solely* on a missing probe."""
    reason = (failure_reason or "").strip()
    return reason.startswith(MISSING_PROBE_BLOCK_PREFIX) and is_missing_probe_error(reason)
