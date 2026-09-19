import pytest

from remediator.devin.client import CreateSessionRequest, DevinApiError, DevinTransportError
from remediator.devin.fake import FakeDevinClient, FakeScenario
from remediator.devin.tags import correlation_tags, operation_key
from remediator.devin.triage import TRIAGE_OUTPUT_SCHEMA, validate_triage_output


def request(issue: int, kind: str = "TRIAGE", ordinal: int = 1) -> CreateSessionRequest:
    key = operation_key("case", kind, ordinal)
    return CreateSessionRequest(
        prompt="",
        repository="apache/superset",
        base_sha="0" * 40,
        max_acu_limit=1,
        operation_key=key,
        tags=correlation_tags("apache/superset", issue, kind, "case", "attempt"),
        structured_output_schema=TRIAGE_OUTPUT_SCHEMA,
    )


async def poll_until_settled(client: FakeDevinClient, session_id: str, limit: int = 10):
    snapshot = await client.get_session(session_id)
    for _ in range(limit):
        if snapshot.status_detail not in {None, "working"} or snapshot.status not in {
            "new",
            "claimed",
            "running",
        }:
            break
        snapshot = await client.get_session(session_id)
    return snapshot


@pytest.mark.asyncio
async def test_fake_determinism_and_operation_tag() -> None:
    first = await FakeDevinClient().create_session(request(4213))
    second = await FakeDevinClient().create_session(request(4213))
    assert first.session_id == second.session_id
    assert first.tags[0] == operation_key("case", "TRIAGE", 1)
    assert first.status == "new"


@pytest.mark.asyncio
async def test_fake_rejects_duplicate_operation_key() -> None:
    client = FakeDevinClient()
    await client.create_session(request(4213))
    with pytest.raises(DevinApiError) as excinfo:
        await client.create_session(request(4213))
    assert excinfo.value.status_code == 409
    found = await client.find_sessions_by_tag(operation_key("case", "TRIAGE", 1))
    assert len(found) == 1


@pytest.mark.asyncio
async def test_fake_scenarios_use_v3_status_vocabulary() -> None:
    client = FakeDevinClient()
    failed = await client.create_session(request(4515))
    waiting = await client.create_session(request(4529, ordinal=2))
    quota = await client.create_session(request(4633, ordinal=3))
    assert (await poll_until_settled(client, failed.session_id)).status == "error"
    waiting_snapshot = await poll_until_settled(client, waiting.session_id)
    assert (waiting_snapshot.status, waiting_snapshot.status_detail) == (
        "running",
        "waiting_for_user",
    )
    quota_snapshot = await poll_until_settled(client, quota.session_id)
    assert (quota_snapshot.status, quota_snapshot.status_detail) == ("suspended", "out_of_credits")


@pytest.mark.asyncio
async def test_fake_success_output_validates_and_malformed_does_not() -> None:
    client = FakeDevinClient()
    good = await client.create_session(request(4213))
    bad = await client.create_session(request(4611, ordinal=2))
    good_snapshot = await poll_until_settled(client, good.session_id)
    assert (good_snapshot.status, good_snapshot.status_detail) == ("running", "finished")
    result = validate_triage_output(good_snapshot.structured_output)
    assert result.remediation_candidate
    bad_snapshot = await poll_until_settled(client, bad.session_id)
    assert bad_snapshot.structured_output == {
        "schema_version": "triage.v1",
        "outcome": "maybe",
        "summary": 1,
    }


@pytest.mark.asyncio
async def test_fake_uncertain_create_registers_session_once() -> None:
    client = FakeDevinClient()
    with pytest.raises(DevinTransportError):
        await client.create_session(request(4622))
    found = await client.find_sessions_by_tag(operation_key("case", "TRIAGE", 1))
    assert [s.session_id for s in found] == [f"fake-triage-4622-{found[0].session_id[-12:]}"]
    assert client.create_calls == 1


@pytest.mark.asyncio
async def test_fake_timeout_and_terminate() -> None:
    client = FakeDevinClient(scenarios={4213: FakeScenario.TIMEOUT})
    created = await client.create_session(request(4213))
    for _ in range(5):
        assert (await client.get_session(created.session_id)).status_detail == "working"
    terminated = await client.terminate_session(created.session_id)
    assert terminated and terminated.status == "exit"
    assert client.terminate_calls == [created.session_id]
    with pytest.raises(DevinTransportError):
        await FakeDevinClient(fail_terminate=True).terminate_session("anything")
