"""Transactional outbox dispatcher.

Every network side effect of the approval flow (Slack posts/updates, GitHub labels and
comments) is a row in `notification_outbox` written in the same transaction as the state
it announces. This module claims rows with leases, performs the call through the fake or
live adapter, and applies bounded exponential backoff. Terminal failures stay visible on
the row (`FAILED` + `last_error`) and, for the GitHub label operation, on the case
(`APPROVAL_DELIVERY_FAILED`). It never creates a Devin session.
"""

import logging
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..api.metrics import outbox_deliveries_total
from ..approvals import (
    enqueue_slack_status_update,
    generate_action_token,
    hash_action_token,
    is_current_request,
    record_event,
    triage_result_hash,
)
from ..config import Settings
from ..github.client import GitHubApiError, GitHubIssuesClient, IssueNotFound, comment_marker
from ..lifecycle import CaseState, InvalidTransition, transition
from ..models import (
    OUTBOX_KIND_GITHUB_APPLY_LABEL,
    OUTBOX_KIND_GITHUB_NOT_FEASIBLE,
    OUTBOX_KIND_GITHUB_REJECTION_COMMENT,
    OUTBOX_KIND_SLACK_APPROVAL_REQUEST,
    OUTBOX_KIND_SLACK_EPHEMERAL_RESPONSE,
    OUTBOX_KIND_SLACK_STATUS_UPDATE,
    OUTBOX_RECORD_ONLY_KINDS,
    ApprovalDecision,
    ApprovalRequest,
    Attempt,
    Case,
    DeliveryStatus,
    NotificationOutbox,
    NotificationStatus,
    OutboxChannel,
    OutboxStatus,
)
from ..slack.blocks import (
    ApprovalMessageInput,
    ApprovalMessageStatus,
    build_approval_blocks,
    fallback_text,
)
from ..slack.client import SlackApiError, SlackClient, SlackMessageRef, approval_metadata

logger = logging.getLogger(__name__)


class OutboxSkip(Exception):
    """The row is obsolete (e.g. the request was decided before Slack was notified)."""


class OutboxPermanentFailure(Exception):
    """Do not retry: the operation can never succeed (issue deleted, stale approval...)."""


def backoff_delay(settings: Settings, attempts: int) -> timedelta:
    base = settings.outbox_base_backoff_seconds * (2 ** max(attempts - 1, 0))
    jitter = random.uniform(0, settings.outbox_base_backoff_seconds)  # noqa: S311
    return timedelta(seconds=min(base + jitter, settings.outbox_max_backoff_seconds))


def message_status(request: ApprovalRequest) -> ApprovalMessageStatus:
    if request.decision == ApprovalDecision.REJECTED:
        return ApprovalMessageStatus.REJECTED
    if request.decision == ApprovalDecision.EXPIRED:
        return ApprovalMessageStatus.EXPIRED
    if request.decision == ApprovalDecision.SUPERSEDED:
        return ApprovalMessageStatus.SUPERSEDED
    if request.decision == ApprovalDecision.APPROVED:
        if request.delivery_status == DeliveryStatus.CONFIRMED:
            return ApprovalMessageStatus.LABEL_APPLIED
        if request.delivery_status == DeliveryStatus.FAILED:
            return ApprovalMessageStatus.DELIVERY_FAILED
        return ApprovalMessageStatus.APPROVED_PENDING_GITHUB
    return ApprovalMessageStatus.AWAITING


def _plain(text: str, limit: int = 200) -> str:
    """Untrusted free text rendered into Slack/GitHub: single line, no markup characters."""
    cleaned = " ".join(text.split())
    cleaned = cleaned.replace("<", "").replace(">", "").replace("`", "").replace("@", "")
    return cleaned[:limit]


def _decision_note(request: ApprovalRequest) -> str | None:
    if request.decision in {ApprovalDecision.APPROVED, ApprovalDecision.REJECTED}:
        who = request.decided_by_slack_user_id or "unknown"
        when = request.decided_at.isoformat(timespec="seconds") if request.decided_at else ""
        note = f"{request.decision.value.title()} by <@{who}> at {when}"
        if request.decision_reason:
            note += f" — {_plain(request.decision_reason)}"
        return note
    return None


class OutboxDispatcher:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        slack: SlackClient,
        github: GitHubIssuesClient,
        worker_id: str,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.slack = slack
        self.github = github
        self.worker_id = worker_id

    # -- claiming ------------------------------------------------------------------

    async def claim(self) -> NotificationOutbox | None:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(NotificationOutbox)
                    .where(
                        NotificationOutbox.status == OutboxStatus.PENDING,
                        NotificationOutbox.next_attempt_at <= now,
                        (
                            NotificationOutbox.lease_expires_at.is_(None)
                            | (NotificationOutbox.lease_expires_at < now)
                        ),
                    )
                    .order_by(NotificationOutbox.next_attempt_at, NotificationOutbox.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if row is None:
                    return None
                row.attempts_count += 1
                row.last_attempt_at = now
                row.claimed_by = self.worker_id
                row.lease_expires_at = now + timedelta(seconds=self.settings.outbox_lease_seconds)
                logger.info(
                    "claimed outbox %s %s/%s attempt %s",
                    row.id,
                    row.channel.value,
                    row.kind,
                    row.attempts_count,
                )
                return row

    # -- dispatch ------------------------------------------------------------------

    async def dispatch(self, row_id: Any) -> None:
        async with self.session_factory() as session:
            row = await session.get(NotificationOutbox, row_id, with_for_update=True)
            if row is None or row.claimed_by != self.worker_id:
                return
            channel = row.channel.value
            failure: tuple[str, bool, float | None] | None = None
            try:
                await self._handle(session, row)
            except OutboxSkip as exc:
                row.last_error = f"skipped: {exc}"
                outbox_deliveries_total.labels(channel=channel, result="skipped").inc()
            except OutboxPermanentFailure as exc:
                failure = (str(exc), True, None)
            except (SlackApiError, GitHubApiError) as exc:
                failure = (str(exc), not exc.retryable, exc.retry_after_seconds)
            except InvalidTransition as exc:
                failure = (f"case changed concurrently: {exc}", True, None)
            except Exception as exc:
                logger.exception("outbox %s raised", row_id)
                failure = (f"{type(exc).__name__}: {exc}", False, None)
            else:
                row.last_error = None
                outbox_deliveries_total.labels(channel=channel, result="sent").inc()
            if failure is None:
                row.status = OutboxStatus.SENT
                row.sent_at = datetime.now(UTC)
                row.claimed_by = None
                row.lease_expires_at = None
                await session.commit()
                return
            # Discard whatever the handler changed since its last explicit commit; only
            # retry bookkeeping is persisted.
            await session.rollback()
            await self._fail(session, row_id, *failure)
            await session.commit()

    async def _fail(
        self,
        session: AsyncSession,
        row_id: Any,
        error: str,
        permanent: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        row = await session.get(NotificationOutbox, row_id, with_for_update=True)
        assert row is not None
        terminal = permanent or row.attempts_count >= self.settings.outbox_max_attempts
        row.last_error = error[:2000]
        row.claimed_by = None
        row.lease_expires_at = None
        if terminal:
            row.status = OutboxStatus.FAILED
            outbox_deliveries_total.labels(channel=row.channel.value, result="failed").inc()
            logger.error(
                "outbox %s %s/%s failed permanently after %s attempt(s): %s",
                row.id,
                row.channel.value,
                row.kind,
                row.attempts_count,
                error,
            )
            await self._on_terminal_failure(session, row, error)
        else:
            delay = backoff_delay(self.settings, row.attempts_count)
            if retry_after_seconds is not None:
                # Honour the provider's Retry-After, capped like ordinary backoff.
                hinted = min(retry_after_seconds, self.settings.outbox_max_backoff_seconds)
                delay = max(delay, timedelta(seconds=hinted))
            row.next_attempt_at = datetime.now(UTC) + delay
            outbox_deliveries_total.labels(channel=row.channel.value, result="retry").inc()
            logger.warning(
                "outbox %s %s/%s attempt %s failed, retrying at %s: %s",
                row.id,
                row.channel.value,
                row.kind,
                row.attempts_count,
                row.next_attempt_at.isoformat(timespec="seconds"),
                error,
            )

    async def _on_terminal_failure(
        self, session: AsyncSession, row: NotificationOutbox, error: str
    ) -> None:
        if row.approval_request_id is None:
            return
        request = await session.get(ApprovalRequest, row.approval_request_id, with_for_update=True)
        if request is None:
            return
        if row.kind == OUTBOX_KIND_SLACK_APPROVAL_REQUEST:
            request.notification_status = NotificationStatus.FAILED
            record_event(
                session, request, "slack_notification_failed", "worker", f"outbox {row.id}: {error}"
            )
        elif row.kind == OUTBOX_KIND_GITHUB_APPLY_LABEL:
            if request.label_applied_at is not None:
                # The label itself landed; only the audit comment is missing. Keep the
                # delivery status truthful so the signed webhook can still confirm it.
                record_event(
                    session,
                    request,
                    "approval_comment_failed",
                    "worker",
                    f"outbox {row.id}: {error}",
                )
                return
            request.delivery_status = DeliveryStatus.FAILED
            record_event(
                session, request, "label_delivery_failed", "worker", f"outbox {row.id}: {error}"
            )
            case = await session.get(Case, request.case_id, with_for_update=True)
            awaiting = CaseState.AWAITING_REMEDIATION_APPROVAL
            if case is not None and CaseState(case.state) == awaiting:
                await transition(
                    session,
                    case,
                    CaseState.APPROVAL_DELIVERY_FAILED,
                    f"GitHub label delivery failed: {error[:200]}",
                    "worker",
                )
            enqueue_slack_status_update(session, request)
        elif row.kind == OUTBOX_KIND_GITHUB_REJECTION_COMMENT:
            record_event(
                session, request, "rejection_comment_failed", "worker", f"outbox {row.id}: {error}"
            )
        elif row.kind == OUTBOX_KIND_SLACK_STATUS_UPDATE:
            record_event(
                session, request, "slack_update_failed", "worker", f"outbox {row.id}: {error}"
            )
        elif row.kind == OUTBOX_KIND_SLACK_EPHEMERAL_RESPONSE:
            record_event(
                session, request, "slack_response_failed", "worker", f"outbox {row.id}: {error}"
            )

    # -- handlers ------------------------------------------------------------------

    async def _handle(self, session: AsyncSession, row: NotificationOutbox) -> None:
        if row.channel == OutboxChannel.SLACK and row.kind == OUTBOX_KIND_SLACK_APPROVAL_REQUEST:
            await self._slack_approval_request(session, row)
        elif row.channel == OutboxChannel.SLACK and row.kind == OUTBOX_KIND_SLACK_STATUS_UPDATE:
            await self._slack_status_update(session, row)
        elif row.channel == OutboxChannel.GITHUB and row.kind == OUTBOX_KIND_GITHUB_APPLY_LABEL:
            await self._github_apply_label(session, row)
        elif (
            row.channel == OutboxChannel.GITHUB and row.kind == OUTBOX_KIND_GITHUB_REJECTION_COMMENT
        ):
            await self._github_rejection_comment(session, row)
        elif (
            row.channel == OutboxChannel.SLACK and row.kind == OUTBOX_KIND_SLACK_EPHEMERAL_RESPONSE
        ):
            await self._slack_ephemeral_response(session, row)
        elif row.channel == OutboxChannel.GITHUB and row.kind == OUTBOX_KIND_GITHUB_NOT_FEASIBLE:
            await self._github_not_feasible(session, row)
        elif row.kind in OUTBOX_RECORD_ONLY_KINDS:
            # Phase 1/2 intents are audit records; nothing is delivered for them.
            logger.debug("outbox %s %s/%s is record-only", row.id, row.channel.value, row.kind)
        else:
            raise OutboxPermanentFailure(f"unknown outbox kind {row.channel.value}/{row.kind}")

    async def _load_request(
        self, session: AsyncSession, row: NotificationOutbox
    ) -> tuple[ApprovalRequest, Case, Attempt]:
        if row.approval_request_id is None:
            raise OutboxPermanentFailure("outbox row has no approval request")
        request = await session.get(ApprovalRequest, row.approval_request_id, with_for_update=True)
        if request is None:
            raise OutboxPermanentFailure("approval request no longer exists")
        case = await session.get(Case, request.case_id, with_for_update=True)
        attempt = await session.get(Attempt, request.attempt_id)
        if case is None or attempt is None:
            raise OutboxPermanentFailure("approval request lost its case or attempt")
        return request, case, attempt

    def _message_input(
        self,
        request: ApprovalRequest,
        case: Case,
        attempt: Attempt,
        *,
        token: str | None,
    ) -> ApprovalMessageInput:
        return ApprovalMessageInput(
            repository=case.repository,
            issue_number=case.issue_number,
            issue_title=case.issue_title,
            issue_url=case.issue_url,
            devin_session_url=attempt.devin_session_url or case.devin_session_url,
            dashboard_url=f"{self.settings.dashboard_base_url.rstrip('/')}/cases/{case.id}",
            triage=attempt.structured_output or {},
            action_token=token,
            status=message_status(request),
            decision_note=_decision_note(request),
        )

    async def _slack_approval_request(self, session: AsyncSession, row: NotificationOutbox) -> None:
        request, case, attempt = await self._load_request(session, row)
        if request.notification_status == NotificationStatus.SENT:
            raise OutboxSkip("approval request already notified")
        if request.decision != ApprovalDecision.PENDING:
            raise OutboxSkip(f"approval request already {request.decision.value}")
        if CaseState(case.state) != CaseState.AWAITING_REMEDIATION_APPROVAL:
            raise OutboxSkip(f"case is {case.state}, not awaiting approval")
        channel = self.settings.slack_channel_id
        if request.notification_status == NotificationStatus.SENDING:
            # A previous attempt may have posted before its commit was lost; find that
            # message by its metadata instead of posting a second one.
            ref = await self.slack.find_message(
                channel, str(request.id), oldest=request.created_at - timedelta(minutes=5)
            )
            if ref is not None:
                self._mark_notified(session, request, ref, reconciled=True)
                return
        token = generate_action_token()
        request.action_token_hash = hash_action_token(token)
        request.notification_status = NotificationStatus.SENDING
        # Commit the token and the SENDING marker before the network call so a crash after
        # Slack accepted the post leaves a reconcilable record rather than a fresh token.
        await session.commit()
        message = self._message_input(request, case, attempt, token=token)
        ref = await self.slack.post_message(
            channel,
            fallback_text(message),
            build_approval_blocks(message),
            metadata=approval_metadata(str(request.id)),
        )
        self._mark_notified(session, request, ref, reconciled=False)

    def _mark_notified(
        self,
        session: AsyncSession,
        request: ApprovalRequest,
        ref: SlackMessageRef,
        *,
        reconciled: bool,
    ) -> None:
        request.notification_status = NotificationStatus.SENT
        request.slack_channel = ref.channel
        request.slack_message_ts = ref.ts
        record_event(
            session,
            request,
            "slack_notified",
            "worker",
            f"approval request {'reconciled with' if reconciled else 'posted to'} Slack "
            f"channel {ref.channel} (ts {ref.ts})",
        )

    async def _slack_ephemeral_response(
        self, session: AsyncSession, row: NotificationOutbox
    ) -> None:
        request, _case, _attempt = await self._load_request(session, row)
        response_url = str(row.payload.get("response_url") or "")
        text = str(row.payload.get("text") or "")
        if not response_url or not text:
            raise OutboxSkip("no response_url to answer")
        await self.slack.post_response(response_url, text)
        record_event(
            session,
            request,
            "slack_responded",
            "worker",
            f"ephemeral `{row.payload.get('outcome', '')}` explanation sent to the clicking user",
        )

    async def _slack_status_update(self, session: AsyncSession, row: NotificationOutbox) -> None:
        request, case, attempt = await self._load_request(session, row)
        if request.slack_channel is None or request.slack_message_ts is None:
            raise OutboxSkip("no Slack message to update")
        message = self._message_input(request, case, attempt, token=None)
        await self.slack.update_message(
            SlackMessageRef(request.slack_channel, request.slack_message_ts),
            fallback_text(message),
            build_approval_blocks(message),
        )
        record_event(
            session,
            request,
            "slack_updated",
            "worker",
            f"Slack message updated to `{message.status.value}`",
        )

    async def _github_apply_label(self, session: AsyncSession, row: NotificationOutbox) -> None:
        request, case, attempt = await self._load_request(session, row)
        if request.decision != ApprovalDecision.APPROVED:
            raise OutboxPermanentFailure(f"approval request is {request.decision.value}")
        expected_hash = str(row.payload.get("triage_result_hash", ""))
        current_hash = triage_result_hash(attempt.structured_output or {})
        approved_hash = request.triage_result_hash
        if expected_hash != approved_hash or current_hash != approved_hash:
            raise OutboxPermanentFailure(
                "approved triage result is no longer current "
                f"(approved {request.triage_result_hash[:12]}, current {current_hash[:12]})"
            )
        expected_attempt = str(row.payload.get("attempt_id") or request.attempt_id)
        if expected_attempt != str(request.attempt_id) or not await is_current_request(
            session, request
        ):
            raise OutboxPermanentFailure(
                "approved triage attempt is no longer the case's current attempt"
            )
        state = CaseState(case.state)
        if state == CaseState.REMEDIATION_APPROVED and request.label_applied_at is None:
            raise OutboxPermanentFailure("case was approved without our label delivery")
        if state not in {
            CaseState.AWAITING_REMEDIATION_APPROVAL,
            CaseState.APPROVAL_DELIVERY_FAILED,
            CaseState.REMEDIATION_APPROVED,
        }:
            raise OutboxPermanentFailure(f"case is {state.value}; refusing to label")
        if not self.settings.repository_allowed(case.repository):
            raise OutboxPermanentFailure(f"repository {case.repository} is not allowlisted")
        label = str(row.payload.get("label") or self.settings.github_remediation_label)
        try:
            issue = await self.github.get_issue(case.repository, case.issue_number)
        except IssueNotFound as exc:
            raise OutboxPermanentFailure(str(exc)) from exc
        if issue.state != "open":
            raise OutboxPermanentFailure(f"issue is {issue.state}; refusing to label")
        if request.label_applied_at is None:
            if request.label_requested_at is not None and label in issue.labels:
                # A previous attempt's POST succeeded but its commit was lost.
                applied_now = False
            else:
                request.label_requested_at = datetime.now(UTC)
                await session.commit()
                result = await self.github.add_label(case.repository, case.issue_number, label)
                applied_now = result.applied
            request.label_applied_at = datetime.now(UTC)
            request.delivery_status = DeliveryStatus.LABEL_APPLIED
            record_event(
                session,
                request,
                "label_applied",
                "worker",
                f"`{label}` {'added to' if applied_now else 'already present on'} "
                f"{case.repository}#{case.issue_number}; awaiting signed GitHub webhook",
            )
            # Persist the label bookkeeping before the comment so a comment failure can
            # never cause a second label write on retry.
            await session.commit()
        if request.github_comment_id is None:
            marker = comment_marker("approval", str(request.id))
            request.github_comment_id = await self._ensure_comment(
                session,
                request,
                case,
                marker,
                self._approval_comment(request, case, attempt) + f"\n\n{marker}",
            )
            record_event(
                session,
                request,
                "approval_commented",
                "worker",
                f"approval audit comment {request.github_comment_id} posted",
            )
        if request.delivery_status == DeliveryStatus.FAILED:
            request.delivery_status = DeliveryStatus.LABEL_APPLIED
        enqueue_slack_status_update(session, request)

    async def _ensure_comment(
        self,
        session: AsyncSession,
        request: ApprovalRequest,
        case: Case,
        marker: str,
        body: str,
    ) -> int:
        """Create the comment exactly once across crash/retry windows.

        The intent is committed before the POST; on a later attempt with the intent set, the
        issue's comments are searched for the request-specific marker before posting again.
        """
        if request.comment_requested_at is not None:
            existing = await self.github.find_comment(case.repository, case.issue_number, marker)
            if existing is not None:
                return existing
        else:
            request.comment_requested_at = datetime.now(UTC)
            await session.commit()
        return await self.github.create_comment(case.repository, case.issue_number, body)

    def _approval_comment(self, request: ApprovalRequest, case: Case, attempt: Attempt) -> str:
        when = request.decided_at.isoformat(timespec="seconds") if request.decided_at else "?"
        dashboard = f"{self.settings.dashboard_base_url.rstrip('/')}/cases/{case.id}"
        session_url = attempt.devin_session_url or case.devin_session_url or "n/a"
        return (
            "### Remediation approved\n\n"
            f"- **Approved by:** Slack user `{request.decided_by_slack_user_id}`\n"
            f"- **Approved at:** {when}\n"
            f"- **Triage session:** {session_url}\n"
            f"- **Evidence:** {dashboard}\n"
            f"- **Triage result:** `sha256:{request.triage_result_hash}` "
            f"(schema {request.triage_schema_version})\n\n"
            f"Label `{self.settings.github_remediation_label}` applied by the remediator. "
            "No remediation session is started by this comment."
        )

    async def _github_rejection_comment(
        self, session: AsyncSession, row: NotificationOutbox
    ) -> None:
        request, case, attempt = await self._load_request(session, row)
        if request.decision != ApprovalDecision.REJECTED:
            raise OutboxPermanentFailure(f"approval request is {request.decision.value}")
        if request.github_comment_id is not None:
            raise OutboxSkip("rejection comment already posted")
        if not self.settings.repository_allowed(case.repository):
            raise OutboxPermanentFailure(f"repository {case.repository} is not allowlisted")
        when = request.decided_at.isoformat(timespec="seconds") if request.decided_at else "?"
        reason = (
            f"\n- **Reason:** {_plain(request.decision_reason)}" if request.decision_reason else ""
        )
        body = (
            "### Remediation rejected\n\n"
            f"- **Rejected by:** Slack user `{request.decided_by_slack_user_id}`\n"
            f"- **Rejected at:** {when}\n"
            f"- **Triage session:** {attempt.devin_session_url or case.devin_session_url or 'n/a'}"
            f"{reason}\n\n"
            "No remediation label was applied and no remediation session will be started."
        )
        marker = comment_marker("rejection", str(request.id))
        request.github_comment_id = await self._ensure_comment(
            session, request, case, marker, body + f"\n\n{marker}"
        )
        record_event(
            session,
            request,
            "rejection_commented",
            "worker",
            f"rejection comment {request.github_comment_id} posted",
        )

    async def _github_not_feasible(self, session: AsyncSession, row: NotificationOutbox) -> None:
        case = await session.get(Case, row.case_id)
        if case is None:
            raise OutboxPermanentFailure("case no longer exists")
        if not self.settings.repository_allowed(case.repository):
            raise OutboxPermanentFailure(f"repository {case.repository} is not allowlisted")
        payload = row.payload
        questions = payload.get("blocking_questions") or []
        bullets = "".join(f"\n- {q}" for q in questions if isinstance(q, str))
        body = (
            "### Triage result: not a remediation candidate\n\n"
            f"- **Outcome:** `{payload.get('outcome', 'unknown')}`\n"
            f"- **Summary:** {payload.get('summary', '')}\n"
            + (f"\n**Open questions:**{bullets}\n" if bullets else "")
            + "\nNo remediation will be attempted for this issue."
        )
        await self.github.create_comment(case.repository, case.issue_number, body)
