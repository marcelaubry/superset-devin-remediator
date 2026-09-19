from sqlalchemy.ext.asyncio import AsyncSession

from .lifecycle import CaseState, transition
from .models import Case


async def approve_remediation(
    session: AsyncSession, case: Case, actor: str, claimed_by: str | None = None
) -> None:
    await transition(
        session,
        case,
        CaseState.REMEDIATION_CREATE_INTENT,
        "remediation approved",
        actor,
        expected_claimed_by=claimed_by,
    )


async def reject_remediation(
    session: AsyncSession, case: Case, actor: str, claimed_by: str | None = None
) -> None:
    await transition(
        session,
        case,
        CaseState.CANCELLED,
        "remediation rejected",
        actor,
        expected_claimed_by=claimed_by,
    )
