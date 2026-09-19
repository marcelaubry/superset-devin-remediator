from sqlalchemy.ext.asyncio import AsyncSession

from .lifecycle import CaseState, transition
from .models import Case


async def approve_remediation(session: AsyncSession, case: Case, actor: str) -> None:
    await transition(
        session,
        case,
        CaseState.REMEDIATION_CREATE_INTENT,
        "remediation approved",
        actor,
    )


async def reject_remediation(session: AsyncSession, case: Case, actor: str) -> None:
    await transition(session, case, CaseState.CANCELLED, "remediation rejected", actor)
