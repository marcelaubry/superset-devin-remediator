import pytest

from remediator.devin.fake import FakeDevinClient


@pytest.mark.asyncio
async def test_fake_determinism() -> None:
    first = FakeDevinClient()
    second = FakeDevinClient()
    tags = {"repository": "apache/superset", "issue_number": "4213", "kind": "REMEDIATION"}
    assert (await first.create_session("", tags)).session_id == (
        await second.create_session("", tags)
    ).session_id


@pytest.mark.asyncio
async def test_fake_failure_and_block() -> None:
    client = FakeDevinClient()
    failed = await client.create_session(
        "", {"repository": "apache/superset", "issue_number": "4515", "kind": "TRIAGE"}
    )
    blocked = await client.create_session(
        "", {"repository": "apache/superset", "issue_number": "4529", "kind": "TRIAGE"}
    )
    assert (await client.get_session(failed.session_id)).status == "failed"
    assert (await client.get_session(blocked.session_id)).status == "blocked"


@pytest.mark.asyncio
async def test_fake_triage_verdict() -> None:
    client = FakeDevinClient()
    feasible = await client.create_session(
        "", {"repository": "apache/superset", "issue_number": "4213", "kind": "TRIAGE"}
    )
    infeasible = await client.create_session(
        "", {"repository": "apache/superset", "issue_number": "4533", "kind": "TRIAGE"}
    )
    for _ in range(3):
        feasible_result = await client.get_session(feasible.session_id)
        infeasible_result = await client.get_session(infeasible.session_id)
    assert feasible_result.output == {
        "remediation_feasible": True,
        "summary": "simulated triage: remediation feasible",
    }
    assert infeasible_result.output == {
        "remediation_feasible": False,
        "summary": "simulated triage: requires product decision",
    }
