"""Database-backed concurrency limits (Phase 5).

Every paid or exclusive step (a Devin triage/remediation session, a probe run, a job that
touches a declared high-conflict resource) first acquires a `CapacityLease`. Leases are
rows, so they survive worker restarts and are shared by every worker instance; acquisition
runs under a per-kind PostgreSQL transaction advisory lock, so two workers can never both
observe free capacity. A lease held by a dead worker becomes free at `expires_at`; live
holders extend it from the worker heartbeat.

Nothing here spends anything: when capacity is missing the caller parks the case
(`Case.waiting_for`) and retries later.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, CursorResult, and_, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from remediator.config import Settings
from remediator.models import (
    ACTIVE_ATTEMPT_STATUSES,
    Attempt,
    CapacityLease,
    CapacityLeaseKind,
    Case,
)
from remediator.probes.registry import validate_resource_key

__all__ = [
    "CapacityDenied",
    "CapacityLimits",
    "CapacityManager",
    "clear_waiting",
    "mark_waiting",
    "validate_resource_key",
    "waiting_count",
]

log = logging.getLogger(__name__)


def _rowcount(result: object) -> int:
    return int(result.rowcount or 0) if isinstance(result, CursorResult) else 0


@dataclass(frozen=True)
class CapacityLimits:
    triage: int
    remediation: int
    probes: int
    remediation_per_repository: int
    lease_seconds: int

    def for_kind(self, kind: CapacityLeaseKind) -> int:
        if kind is CapacityLeaseKind.TRIAGE:
            return self.triage
        if kind is CapacityLeaseKind.REMEDIATION:
            return self.remediation
        if kind is CapacityLeaseKind.PROBE:
            return self.probes
        return 0  # RESOURCE keys have no global limit; each key is a per-scope mutex


@dataclass(frozen=True)
class CapacityDenied:
    kind: CapacityLeaseKind
    scope: str
    limit: int
    in_use: int

    @property
    def label(self) -> str:
        scope = f":{self.scope}" if self.scope else ""
        return f"{self.kind.value}{scope} ({self.in_use}/{self.limit})"


def _active_clause(now: datetime) -> ColumnElement[bool]:
    return and_(CapacityLease.released_at.is_(None), CapacityLease.expires_at > now)


def advisory_key(kind: CapacityLeaseKind) -> int:
    digest = hashlib.sha256(f"remediator.capacity.{kind.value}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def limits_from_settings(settings: Settings) -> CapacityLimits:
    return CapacityLimits(
        triage=settings.max_concurrent_triage,
        remediation=settings.max_concurrent_remediation,
        probes=settings.max_concurrent_probes,
        remediation_per_repository=settings.max_concurrent_remediation_per_repository,
        lease_seconds=settings.capacity_lease_grace_seconds,
    )


class CapacityManager:
    def __init__(self, limits: CapacityLimits, owner: str) -> None:
        self.limits = limits
        self.owner = owner

    @classmethod
    def from_settings(cls, settings: Settings, owner: str) -> CapacityManager:
        return cls(limits_from_settings(settings), owner)

    async def reconcile_finished(self, session: AsyncSession) -> int:
        """Safety net: a lease bound to an attempt that is no longer live (finished, failed,
        cancelled after confirmed termination) is released even if the holder crashed
        between settling the attempt and releasing."""
        now = datetime.now(UTC)
        finished = select(Attempt.id).where(
            (Attempt.status.not_in(ACTIVE_ATTEMPT_STATUSES)) | (Attempt.finished_at.is_not(None))
        )
        result = await session.execute(
            update(CapacityLease)
            .where(
                CapacityLease.released_at.is_(None),
                CapacityLease.attempt_id.is_not(None),
                CapacityLease.attempt_id.in_(finished),
            )
            .values(released_at=now, release_reason="attempt no longer active (reconciled)")
        )
        return _rowcount(result)

    async def in_use(
        self, session: AsyncSession, kind: CapacityLeaseKind, scope: str | None = None
    ) -> int:
        now = datetime.now(UTC)
        stmt = (
            select(func.count())
            .select_from(CapacityLease)
            .where(CapacityLease.kind == kind, _active_clause(now))
        )
        if scope is not None:
            stmt = stmt.where(CapacityLease.scope == scope)
        return int(await session.scalar(stmt) or 0)

    async def acquire(
        self,
        session: AsyncSession,
        *,
        kind: CapacityLeaseKind,
        case_id: uuid.UUID,
        scope: str = "",
        attempt_id: uuid.UUID | None = None,
        per_scope_limit: int | None = None,
        force: bool = False,
    ) -> CapacityLease | CapacityDenied:
        """Acquire (or re-find) this case's lease of `kind`/`scope` inside the caller's
        transaction. Idempotent: a live lease already held by this case is renewed and
        returned, so a worker crash between acquire and commit never double-counts."""
        now = datetime.now(UTC)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)").bindparams(key=advisory_key(kind))
        )
        await self.reconcile_finished(session)
        existing = await session.scalar(
            select(CapacityLease).where(
                CapacityLease.kind == kind,
                CapacityLease.scope == scope,
                CapacityLease.case_id == case_id,
                CapacityLease.released_at.is_(None),
            )
        )
        expires = now + timedelta(seconds=self.limits.lease_seconds)
        if existing is not None and existing.expires_at > now:
            existing.expires_at = expires
            existing.owner = self.owner
            if attempt_id is not None:
                existing.attempt_id = attempt_id
            return existing
        if existing is not None:
            # Our own lease lapsed (e.g. this worker was paused); it no longer counts, and
            # another case may legitimately hold the slot now. Compete again from scratch.
            existing.released_at = now
            existing.release_reason = "lease expired before renewal"
            await session.flush()
        if kind is CapacityLeaseKind.RESOURCE:
            per_scope_limit = 1
        limit = self.limits.for_kind(kind)
        if limit and not force:
            total = await self.in_use(session, kind)
            if total >= limit:
                return CapacityDenied(kind, "", limit, total)
        if per_scope_limit is not None and scope and not force:
            scoped = await self.in_use(session, kind, scope)
            if scoped >= per_scope_limit:
                return CapacityDenied(kind, scope, per_scope_limit, scoped)
        lease = CapacityLease(
            kind=kind,
            scope=scope,
            case_id=case_id,
            attempt_id=attempt_id,
            owner=self.owner,
            acquired_at=now,
            expires_at=expires,
        )
        session.add(lease)
        await session.flush()
        return lease

    async def release(
        self,
        session: AsyncSession,
        *,
        kind: CapacityLeaseKind,
        case_id: uuid.UUID,
        scope: str | None = None,
        reason: str,
    ) -> int:
        now = datetime.now(UTC)
        stmt = (
            update(CapacityLease)
            .where(
                CapacityLease.kind == kind,
                CapacityLease.case_id == case_id,
                CapacityLease.released_at.is_(None),
            )
            .values(released_at=now, release_reason=reason[:500])
        )
        if scope is not None:
            stmt = stmt.where(CapacityLease.scope == scope)
        result = await session.execute(stmt)
        return _rowcount(result)

    async def release_all_for_case(
        self, session: AsyncSession, case_id: uuid.UUID, reason: str
    ) -> int:
        now = datetime.now(UTC)
        result = await session.execute(
            update(CapacityLease)
            .where(CapacityLease.case_id == case_id, CapacityLease.released_at.is_(None))
            .values(released_at=now, release_reason=reason[:500])
        )
        return _rowcount(result)

    async def heartbeat(self, session: AsyncSession, case_ids: list[uuid.UUID]) -> None:
        if not case_ids:
            return
        now = datetime.now(UTC)
        await session.execute(
            update(CapacityLease)
            .where(
                CapacityLease.case_id.in_(case_ids),
                CapacityLease.owner == self.owner,
                CapacityLease.released_at.is_(None),
            )
            .values(expires_at=now + timedelta(seconds=self.limits.lease_seconds))
        )

    async def expire_stale(self, session: AsyncSession) -> int:
        """Mark expired-but-unreleased leases as released so the ledger stays readable.
        Counting already ignores them; this is bookkeeping for operators and metrics."""
        now = datetime.now(UTC)
        result = await session.execute(
            update(CapacityLease)
            .where(CapacityLease.released_at.is_(None), CapacityLease.expires_at <= now)
            .values(released_at=now, release_reason="lease expired (holder presumed dead)")
        )
        count = _rowcount(result)
        if count:
            log.warning("capacity: reclaimed %d expired lease(s)", count)
        return count

    async def utilisation(self, session: AsyncSession) -> dict[str, tuple[int, int]]:
        now = datetime.now(UTC)
        rows = await session.execute(
            select(CapacityLease.kind, func.count())
            .where(_active_clause(now), CapacityLease.kind != CapacityLeaseKind.RESOURCE)
            .group_by(CapacityLease.kind)
        )
        counts = {kind: int(n) for kind, n in rows.all()}
        return {
            kind.value.lower(): (counts.get(kind, 0), self.limits.for_kind(kind))
            for kind in (
                CapacityLeaseKind.TRIAGE,
                CapacityLeaseKind.REMEDIATION,
                CapacityLeaseKind.PROBE,
            )
        }


async def mark_waiting(session: AsyncSession, case: Case, denied: CapacityDenied) -> None:
    if case.waiting_for != denied.label:
        log.info("case %s waiting for capacity: %s", case.id, denied.label)
    if case.waiting_since is None:
        case.waiting_since = datetime.now(UTC)
    case.waiting_for = denied.label


async def clear_waiting(session: AsyncSession, case: Case) -> None:
    case.waiting_for = None
    case.waiting_since = None


async def waiting_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Case).where(Case.waiting_for.is_not(None))
        )
        or 0
    )
