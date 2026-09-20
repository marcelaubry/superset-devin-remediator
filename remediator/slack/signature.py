"""Slack request signing (https://api.slack.com/authentication/verifying-requests-from-slack).

Slack signs `v0:{timestamp}:{raw_body}` with HMAC-SHA256 using the app's signing secret and
sends the hex digest as `X-Slack-Signature: v0=<hex>` plus `X-Slack-Request-Timestamp`.
Verification must run on the raw body before any form/JSON decoding.
"""

import hashlib
import hmac
import time
from dataclasses import dataclass
from enum import StrEnum

SIGNATURE_VERSION = "v0"


class SlackVerificationFailure(StrEnum):
    MISSING_TIMESTAMP = "missing_timestamp"
    INVALID_TIMESTAMP = "invalid_timestamp"
    STALE_TIMESTAMP = "stale_timestamp"
    MISSING_SIGNATURE = "missing_signature"
    INVALID_SIGNATURE = "invalid_signature"


@dataclass(frozen=True)
class SlackVerification:
    ok: bool
    failure: SlackVerificationFailure | None = None


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    basestring = f"{SIGNATURE_VERSION}:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_slack_request(
    secret: str,
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    *,
    max_skew_seconds: int,
    now: float | None = None,
) -> SlackVerification:
    if not timestamp_header:
        return SlackVerification(False, SlackVerificationFailure.MISSING_TIMESTAMP)
    try:
        timestamp = int(timestamp_header)
    except ValueError:
        return SlackVerification(False, SlackVerificationFailure.INVALID_TIMESTAMP)
    current = time.time() if now is None else now
    if abs(current - timestamp) > max_skew_seconds:
        return SlackVerification(False, SlackVerificationFailure.STALE_TIMESTAMP)
    if not signature_header or not signature_header.startswith(f"{SIGNATURE_VERSION}="):
        return SlackVerification(False, SlackVerificationFailure.MISSING_SIGNATURE)
    expected = compute_signature(secret, timestamp_header, body)
    if not hmac.compare_digest(expected.encode(), signature_header.encode()):
        return SlackVerification(False, SlackVerificationFailure.INVALID_SIGNATURE)
    return SlackVerification(True)
