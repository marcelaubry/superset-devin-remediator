"""Phase 2 spend-boundary flows exercised end-to-end against PostgreSQL with the fake client."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remediator.config import Settings
from remediator.devin.client import DevinTransportError, SessionSnapshot
from remediator.devin.fake import FakeDevinClient, FakeScenario, sample_triage_output
from remediator.devin.tags import OPERATION_TAG_PREFIX
from remediator.lifecycle import CaseState
from remediator.models import (
    Attempt,
    AttemptKind,
    AttemptStatus,
    Case,
    CreateState,
    EventStatus,
    NotificationOutbox,
    StateTransition,
    WebhookEvent,
)
from remediator.worker import Worker
from remediator.worker.processor import process_case, process_event

ELIGIBLE_BODY = (
    "Steps to reproduce:\n1. Run.\nExpected behavior works. "
    "Actual behavior fails. Acceptance criteria: fixed. Similar existing pattern."
)
SECRET = "apk_integration_secret_never_persisted"


def _payload(
    number: int, body: str = ELIGIBLE_BODY, labels: tuple[str, ...] = ("bug", "devin-candidate")
):
    return {
        "action": "opened",
        "repository": {"full_name": "apache/superset"},
        "issue": {
            "number": number,
            "title": "Fix issue",
            "body": body,
            "html_url": f"https://github.com/apache/superset/issues/{number}",
            "labels": [{"name": label} for label in labels],
        },
    }


def _event(number: int, delivery: str, **kwargs: object) -> WebhookEvent:
    return WebhookEvent(
        delivery_id=delivery,
        event_type="issues",
        action="opened",
        repository="apache/superset",
        payload=_payload(number, **kwargs),  # type: ignore[arg-type]
        status=EventStatus.PENDING,
    )


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "simulation_auto_approve_remediation": False,
        "devin_poll_interval_seconds": 0,
        "devin_triage_timeout_seconds": 5,
        "reconcile_retry_delay_seconds": 0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _attempts(session: AsyncSession, case: Case) -> list[Attempt]:
    return list(
        (
            await session.scalars(
                select(Attempt).where(Attempt.case_id == case.id).order_by(Attempt.started_at)
            )
        ).all()
    )


async def _states(session: AsyncSession, case: Case) -> list[CaseState]:
    rows = await session.scalars(
        select(StateTransition.to_state)
        .where(StateTransition.case_id == case.id)
        .order_by(StateTransition.seq)
    )
    return [CaseState(value) for value in rows.all()]


@pytest.fixture
async def db(integration_session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    async with integration_session_factory() as session:
        yield session


@pytest.mark.asyncio
async def test_eligible_issue_creates_exactly_one_intent_and_awaits_approval(
    db: AsyncSession,
) -> None:
    event = _event(4213, "p2-eligible")
    db.add(event)
    await db.commit()
    client = FakeDevinClient()
    await process_event(db, event, client, _settings())

    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    attempts = await _attempts(db, case)
    assert len(attempts) == 1 and client.create_calls == 1
    attempt = attempts[0]
    assert attempt.kind == AttemptKind.TRIAGE
    assert attempt.create_state == CreateState.CREATED
    assert attempt.status == AttemptStatus.SUCCEEDED
    assert attempt.operation_key.startswith(OPERATION_TAG_PREFIX)
    assert attempt.devin_tags and attempt.devin_tags[0] == attempt.operation_key
    assert attempt.devin_session_id and attempt.devin_session_url
    assert attempt.devin_status == "running" and attempt.devin_status_detail == "finished"
    assert attempt.base_sha and len(attempt.base_sha) == 40
    assert attempt.prompt_version == "triage_v1"
    assert attempt.max_acu_limit == _settings().devin_triage_max_acu
    assert attempt.poll_count >= 1 and attempt.first_polled_at and attempt.last_polled_at
    assert attempt.timeout_at and attempt.create_sent_at and attempt.finished_at
    assert (
        attempt.structured_output
        and attempt.structured_output["outcome"] == "remediation_candidate"
    )
    states = await _states(db, case)
    assert states == [
        CaseState.ELIGIBILITY_EVALUATED,
        CaseState.TRIAGE_CREATE_INTENT,
        CaseState.TRIAGING,
        CaseState.TRIAGED,
        CaseState.AWAITING_REMEDIATION_APPROVAL,
    ]
    outbox = list(
        (
            await db.scalars(
                select(NotificationOutbox).where(NotificationOutbox.case_id == case.id)
            )
        ).all()
    )
    assert [item.kind for item in outbox] == ["remediation_approval_requested"]


@pytest.mark.asyncio
async def test_ineligible_issue_creates_no_intent(db: AsyncSession) -> None:
    event = _event(4300, "p2-ineligible", body="It is broken.", labels=("devin-candidate",))
    db.add(event)
    await db.commit()
    client = FakeDevinClient()
    await process_event(db, event, client, _settings())
    case = await db.scalar(select(Case).where(Case.issue_number == 4300))
    assert case and case.state == CaseState.POLICY_REJECTED
    assert await _attempts(db, case) == []
    assert client.create_calls == 0


@pytest.mark.asyncio
async def test_duplicate_webhook_creates_no_duplicate_session(db: AsyncSession) -> None:
    first = _event(4213, "p2-dup-1")
    second = _event(4213, "p2-dup-2")
    db.add_all([first, second])
    await db.commit()
    client = FakeDevinClient()
    await process_event(db, first, client, _settings())
    await process_event(db, second, client, _settings())
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and len(await _attempts(db, case)) == 1
    assert client.create_calls == 1
    assert second.status == EventStatus.PROCESSED and second.case_id == case.id


@pytest.mark.asyncio
async def test_operation_key_is_unique_in_the_database(db: AsyncSession) -> None:
    case = Case(
        issue_number=4900,
        repository="apache/superset",
        issue_title="dup",
        issue_url="https://github.com/apache/superset/issues/4900",
        state=CaseState.TRIAGE_CREATE_INTENT,
    )
    db.add(case)
    await db.flush()
    case_id = case.id
    db.add(
        Attempt(
            case_id=case_id,
            kind=AttemptKind.TRIAGE,
            idempotency_key="a",
            operation_key="op:dup",
            status=AttemptStatus.SUCCEEDED,
            finished_at=datetime.now(UTC),
        )
    )
    await db.commit()
    db.add(
        Attempt(
            case_id=case_id,
            kind=AttemptKind.TRIAGE,
            idempotency_key="b",
            operation_key="op:dup",
            status=AttemptStatus.RUNNING,
        )
    )
    with pytest.raises(IntegrityError, match="operation_key"):
        await db.commit()
    await db.rollback()
    db.add_all(
        [
            Attempt(
                case_id=case_id,
                kind=AttemptKind.TRIAGE,
                idempotency_key="c",
                operation_key="op:c",
                status=AttemptStatus.RUNNING,
            ),
            Attempt(
                case_id=case_id,
                kind=AttemptKind.TRIAGE,
                idempotency_key="d",
                operation_key="op:d",
                status=AttemptStatus.RUNNING,
            ),
        ]
    )
    with pytest.raises(IntegrityError, match="uq_attempts_one_active_per_kind"):
        await db.commit()
    await db.rollback()


@pytest.mark.parametrize(
    ("scenario", "reason_fragment"),
    [
        (FakeScenario.MISSING_OUTPUT, "structured output missing"),
        (FakeScenario.MALFORMED_OUTPUT, "structured output invalid"),
    ],
)
@pytest.mark.asyncio
async def test_missing_or_malformed_output_fails_case(
    db: AsyncSession, scenario: FakeScenario, reason_fragment: str
) -> None:
    event = _event(4213, f"p2-{scenario}")
    db.add(event)
    await db.commit()
    client = FakeDevinClient(scenarios={4213: scenario})
    await process_event(db, event, client, _settings())
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.FAILED
    assert case.failure_reason and reason_fragment in case.failure_reason
    (attempt,) = await _attempts(db, case)
    assert attempt.status == AttemptStatus.FAILED
    assert attempt.devin_status_detail == "finished"
    assert client.create_calls == 1


@pytest.mark.asyncio
async def test_uncertain_create_reconciles_by_exact_tag_without_second_post(
    db: AsyncSession,
) -> None:
    event = _event(4213, "p2-uncertain")
    db.add(event)
    await db.commit()
    client = FakeDevinClient(scenarios={4213: FakeScenario.UNCERTAIN_CREATE})
    await process_event(db, event, client, _settings())
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    assert client.create_calls == 1
    (attempt,) = await _attempts(db, case)
    assert attempt.create_state == CreateState.RECONCILED
    assert attempt.status == AttemptStatus.SUCCEEDED
    assert attempt.reconciliation_reason is None
    states = await _states(db, case)
    assert CaseState.RECONCILING_CREATE in states
    assert states.index(CaseState.RECONCILING_CREATE) < states.index(CaseState.TRIAGING)


@pytest.mark.asyncio
async def test_unresolved_create_becomes_human_blocked(db: AsyncSession) -> None:
    event = _event(4213, "p2-unresolved")
    db.add(event)
    await db.commit()

    class VanishingClient(FakeDevinClient):
        async def create_session(self, request):  # type: ignore[no-untyped-def]
            self.create_calls += 1
            raise DevinTransportError("simulated: socket closed before response")

    client = VanishingClient()
    await process_event(db, event, client, _settings(reconcile_max_attempts=2))
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.HUMAN_BLOCKED
    assert client.create_calls == 1
    (attempt,) = await _attempts(db, case)
    assert attempt.create_state == CreateState.UNRESOLVED
    assert attempt.status == AttemptStatus.BLOCKED
    assert (
        attempt.reconciliation_reason and "create outcome unknown" in attempt.reconciliation_reason
    )
    assert case.failure_reason == attempt.reconciliation_reason
    # A subsequent pass must not create a fresh session either.
    await process_case(db, case, client, _settings())
    assert client.create_calls == 1 and case.state == CaseState.HUMAN_BLOCKED


@pytest.mark.asyncio
async def test_create_rejected_by_api_is_recorded_definitively(db: AsyncSession) -> None:
    event = _event(4213, "p2-rejected")
    db.add(event)
    await db.commit()
    client = FakeDevinClient(scenarios={4213: FakeScenario.CREATE_REJECTED})
    await process_event(db, event, client, _settings())
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.FAILED
    (attempt,) = await _attempts(db, case)
    assert attempt.create_state == CreateState.API_ERROR
    assert attempt.error and "422" in attempt.error
    assert attempt.devin_session_id is None


@pytest.mark.asyncio
async def test_non_allowlisted_repository_is_not_sent(db: AsyncSession) -> None:
    event = _event(4213, "p2-not-sent")
    event.repository = "evil/superset"
    event.payload["repository"] = {"full_name": "evil/superset"}  # type: ignore[index]
    db.add(event)
    await db.commit()
    client = FakeDevinClient()
    await process_event(db, event, client, _settings())
    case = await db.scalar(select(Case).where(Case.repository == "evil/superset"))
    assert case and case.state == CaseState.FAILED
    (attempt,) = await _attempts(db, case)
    assert attempt.create_state == CreateState.NOT_SENT
    assert client.create_calls == 0


@pytest.mark.asyncio
async def test_waiting_for_human_blocks_but_keeps_session(db: AsyncSession) -> None:
    event = _event(4213, "p2-waiting")
    db.add(event)
    await db.commit()
    client = FakeDevinClient(scenarios={4213: FakeScenario.WAITING_FOR_HUMAN})
    await process_event(db, event, client, _settings())
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.HUMAN_BLOCKED
    (attempt,) = await _attempts(db, case)
    assert attempt.status == AttemptStatus.BLOCKED
    assert attempt.devin_session_id and case.devin_session_url
    assert attempt.devin_status_detail == "waiting_for_user"
    assert client.terminate_calls == []
    await process_case(db, case, client, _settings())
    assert client.create_calls == 1


@pytest.mark.asyncio
async def test_quota_suspension_is_terminal_failure(db: AsyncSession) -> None:
    event = _event(4213, "p2-quota")
    db.add(event)
    await db.commit()
    client = FakeDevinClient(scenarios={4213: FakeScenario.QUOTA})
    await process_event(db, event, client, _settings())
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.FAILED
    (attempt,) = await _attempts(db, case)
    assert attempt.status == AttemptStatus.FAILED
    assert attempt.devin_status == "suspended"
    assert client.create_calls == 1 and client.terminate_calls == []


@pytest.mark.asyncio
async def test_unknown_status_reconciles_until_timeout_without_replacement(
    db: AsyncSession,
) -> None:
    event = _event(4213, "p2-unknown")
    db.add(event)
    await db.commit()
    client = FakeDevinClient(scenarios={4213: FakeScenario.UNKNOWN_STATUS})
    await process_event(db, event, client, _settings(devin_triage_timeout_seconds=0.2))
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.TIMED_OUT
    (attempt,) = await _attempts(db, case)
    assert attempt.status == AttemptStatus.TIMED_OUT
    assert client.create_calls == 1
    assert client.terminate_calls == [attempt.devin_session_id]


@pytest.mark.asyncio
async def test_final_get_prevents_false_timeout(db: AsyncSession) -> None:
    event = _event(4213, "p2-final-get")
    db.add(event)
    await db.commit()

    class LateFinisher(FakeDevinClient):
        """Reports the session as still working during polling and finished on the final GET."""

        def __init__(self) -> None:
            super().__init__(never_finish_issues={4213})
            self.gets = 0

        async def get_session(self, session_id: str) -> SessionSnapshot:
            self.gets += 1
            snapshot = await super().get_session(session_id)
            if datetime.now(UTC) >= self.deadline:
                return SessionSnapshot(
                    session_id=snapshot.session_id,
                    url=snapshot.url,
                    status="exit",
                    status_detail="finished",
                    tags=snapshot.tags,
                    structured_output=sample_triage_output(4213, "apache/superset"),
                    acus_consumed=1.5,
                )
            return snapshot

    client = LateFinisher()
    client.deadline = datetime.now(UTC) + timedelta(seconds=0.2)
    await process_event(db, event, client, _settings(devin_triage_timeout_seconds=0.2))
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    (attempt,) = await _attempts(db, case)
    assert attempt.status == AttemptStatus.SUCCEEDED
    assert attempt.devin_acus_consumed == 1.5
    assert client.terminate_calls == []


@pytest.mark.asyncio
async def test_timeout_terminates_remotely_then_marks_timed_out(db: AsyncSession) -> None:
    event = _event(4213, "p2-timeout")
    db.add(event)
    await db.commit()
    client = FakeDevinClient(scenarios={4213: FakeScenario.TIMEOUT})
    await process_event(db, event, client, _settings(devin_triage_timeout_seconds=0.1))
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.TIMED_OUT
    (attempt,) = await _attempts(db, case)
    assert attempt.status == AttemptStatus.TIMED_OUT
    assert client.terminate_calls == [attempt.devin_session_id]
    assert attempt.error and "terminated remotely" in attempt.error
    assert attempt.timeout_at and attempt.last_polled_at
    states = await _states(db, case)
    assert states[-1] == CaseState.TIMED_OUT and CaseState.TERMINATION_PENDING not in states
    await process_case(db, case, client, _settings())
    assert client.create_calls == 1


@pytest.mark.asyncio
async def test_failed_termination_becomes_termination_pending_then_recovers(
    db: AsyncSession,
) -> None:
    event = _event(4213, "p2-term-pending")
    db.add(event)
    await db.commit()
    client = FakeDevinClient(scenarios={4213: FakeScenario.TIMEOUT}, fail_terminate=True)
    await process_event(db, event, client, _settings(devin_triage_timeout_seconds=0.1))
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.TERMINATION_PENDING
    (attempt,) = await _attempts(db, case)
    assert attempt.status == AttemptStatus.TERMINATION_PENDING
    assert attempt.finished_at is None
    assert attempt.reconciliation_reason and "DELETE failed" in attempt.reconciliation_reason
    assert client.terminate_calls == [attempt.devin_session_id]

    client.fail_terminate = False
    await process_case(db, case, client, _settings())
    await db.refresh(attempt)
    assert case.state == CaseState.TIMED_OUT
    assert attempt.status == AttemptStatus.TIMED_OUT
    assert attempt.finished_at is not None
    assert client.terminate_calls == [attempt.devin_session_id] * 2
    assert client.create_calls == 1


@pytest.mark.asyncio
async def test_restart_recovery_resumes_polling_of_active_attempt(
    db: AsyncSession,
    integration_session_factory: async_sessionmaker[AsyncSession],
    test_database_url: str,
) -> None:
    event = _event(4213, "p2-restart")
    db.add(event)
    await db.commit()

    class Crashing(FakeDevinClient):
        async def get_session(self, session_id: str) -> SessionSnapshot:
            raise RuntimeError("worker process died mid-poll")

    crashing = Crashing()
    with pytest.raises(RuntimeError):
        await process_event(db, event, crashing, _settings())
    await db.rollback()
    case = await db.scalar(select(Case).where(Case.issue_number == 4213))
    assert case and case.state == CaseState.TRIAGING
    (attempt,) = await _attempts(db, case)
    assert attempt.status == AttemptStatus.RUNNING and attempt.devin_session_id
    event.status = EventStatus.PROCESSED
    case.claimed_by = "dead-worker"
    case.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    survivor = FakeDevinClient()
    survivor._sessions = crashing._sessions
    survivor._by_operation = crashing._by_operation
    worker = Worker(
        _settings(
            database_url=test_database_url,
            worker_lease_seconds=30,
            worker_poll_interval_seconds=0.01,
        )
    )
    worker.devin = survivor
    try:
        claimed = await worker._claim_case()
        assert claimed and claimed.id == case.id and claimed.claimed_by == worker.worker_id
        await worker._run_job(case=claimed)
    finally:
        await worker.base_commits.aclose()
        await worker.engine.dispose()

    await db.refresh(case)
    await db.refresh(attempt)
    assert case.state == CaseState.AWAITING_REMEDIATION_APPROVAL
    assert attempt.status == AttemptStatus.SUCCEEDED
    assert survivor.create_calls == 0 and len(await _attempts(db, case)) == 1


@pytest.mark.asyncio
async def test_restart_recovery_reconciles_pending_create(db: AsyncSession) -> None:
    case = Case(
        issue_number=4950,
        repository="apache/superset",
        issue_title="restart",
        issue_url="https://github.com/apache/superset/issues/4950",
        state=CaseState.TRIAGE_CREATE_INTENT,
    )
    db.add(case)
    await db.flush()
    attempt = Attempt(
        case_id=case.id,
        kind=AttemptKind.TRIAGE,
        idempotency_key=f"{case.id}:TRIAGE:1",
        operation_key=f"op:{case.id}:TRIAGE:1",
        create_state=CreateState.PENDING,
        status=AttemptStatus.RUNNING,
        create_sent_at=datetime.now(UTC),
    )
    db.add(attempt)
    await db.commit()
    client = FakeDevinClient()
    await process_case(db, case, client, _settings(reconcile_max_attempts=1))
    assert case.state == CaseState.HUMAN_BLOCKED
    assert client.create_calls == 0
    await db.refresh(attempt)
    assert attempt.create_state == CreateState.UNRESOLVED


@pytest.mark.asyncio
async def test_api_key_is_absent_from_logs_and_persistence(
    db: AsyncSession, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVIN_API_KEY", SECRET)
    monkeypatch.setenv("DEVIN_ORG_ID", "org_test")
    settings = _settings()
    assert settings.devin_api_key and settings.devin_api_key.get_secret_value() == SECRET
    event = _event(4213, "p2-secret")
    db.add(event)
    await db.commit()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("remediator").info("starting with %r", settings)
        await process_event(db, event, FakeDevinClient(), settings)
    for record in caplog.records:
        assert SECRET not in record.getMessage()
    for table in (
        "cases",
        "attempts",
        "state_transitions",
        "notification_outbox",
        "webhook_events",
    ):
        rows = (await db.execute(text(f"SELECT to_jsonb(t)::text FROM {table} t"))).scalars().all()
        assert all(SECRET not in row for row in rows), table
    assert SECRET not in repr(settings) and SECRET not in settings.model_dump_json()
    assert await db.scalar(select(func.count()).select_from(Attempt)) == 1
