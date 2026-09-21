"""Emergency canary-only probe override (`LIVE_CANARY_ALLOW_MISSING_PROBE`).

The override lets a human Slack approval start one bounded remediation session when no
immutable probe is registered for the issue. It skips *only* probe registration and the
BASE/HEAD probe executions: every other dispatch precondition, the independent GitHub PR
validation and exact-head CI still gate the case, and the resulting PR is disclosed
everywhere as behaviourally unverified.
"""

CANARY_PROBE_OVERRIDE = "CANARY_PROBE_OVERRIDE"

CANARY_OVERRIDE_WARNING = (
    "CANARY OVERRIDE: remediation proceeded without base/head acceptance-probe "
    "verification. PR and exact-head CI were validated, but behavioral correctness "
    "requires human review."
)

CANARY_OVERRIDE_REASON = "missing probe accepted for controlled canary"

CANARY_OVERRIDE_ACTOR = "config:LIVE_CANARY_ALLOW_MISSING_PROBE"

# Prefix of the `cases.failure_reason` written when dispatch blocks on a missing probe.
MISSING_PROBE_BLOCK_PREFIX = "approved probe unavailable:"

# Substring the registry raises only when nothing is registered for the issue; any other
# registry error (malformed manifest, hash mismatch, unreadable script) keeps blocking.
_MISSING_PROBE_MARKER = "no approved probe registered"


def is_missing_probe_error(message: str) -> bool:
    return _MISSING_PROBE_MARKER in message


def is_missing_probe_block(failure_reason: str | None) -> bool:
    """True when a REMEDIATION_HUMAN_BLOCKED case is blocked *solely* on a missing probe."""
    reason = (failure_reason or "").strip()
    return reason.startswith(MISSING_PROBE_BLOCK_PREFIX) and is_missing_probe_error(reason)
