import pytest

from remediator.devin.status import (
    BILLING_DETAILS,
    LIVE_STATUSES,
    Disposition,
    classify,
)


@pytest.mark.parametrize("status", sorted(LIVE_STATUSES))
@pytest.mark.parametrize("detail", [None, "", "working", "WORKING"])
def test_live_statuses_keep_polling(status: str, detail: str | None) -> None:
    mapping = classify(status, detail)
    assert mapping.disposition == Disposition.LIVE
    assert not mapping.remote_terminal


@pytest.mark.parametrize("status", sorted(LIVE_STATUSES))
def test_finished_detail_proceeds_to_output_validation(status: str) -> None:
    assert classify(status, "finished").disposition == Disposition.FINISHED


@pytest.mark.parametrize("status", sorted(LIVE_STATUSES))
@pytest.mark.parametrize("detail", ["waiting_for_user", "waiting_for_approval"])
def test_waiting_details_block_on_human_but_keep_session(status: str, detail: str) -> None:
    mapping = classify(status, detail)
    assert mapping.disposition == Disposition.WAITING_FOR_HUMAN
    assert not mapping.remote_terminal


def test_exit_is_terminal_and_validated() -> None:
    mapping = classify("exit", "finished")
    assert mapping.disposition == Disposition.FINISHED
    assert mapping.remote_terminal


def test_error_is_terminal_failure() -> None:
    mapping = classify("error", None)
    assert mapping.disposition == Disposition.FAILED
    assert mapping.remote_terminal


@pytest.mark.parametrize("detail", sorted(BILLING_DETAILS))
def test_billing_suspensions_are_terminal_failures(detail: str) -> None:
    mapping = classify("suspended", detail)
    assert mapping.disposition == Disposition.FAILED
    assert mapping.remote_terminal
    assert classify("running", detail).disposition == Disposition.FAILED


@pytest.mark.parametrize("detail", ["inactivity", "user_request"])
def test_resumable_suspensions_need_a_human(detail: str) -> None:
    assert classify("suspended", detail).disposition == Disposition.WAITING_FOR_HUMAN


def test_suspended_error_fails() -> None:
    assert classify("suspended", "error").disposition == Disposition.FAILED


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        ("suspended", "mystery"),
        ("hibernating", None),
        ("running", "meditating"),
        ("", None),
        (None, None),
    ],
)
def test_unknown_status_or_detail_enters_reconciliation(status: str | None, detail: str | None):
    mapping = classify(status, detail)
    assert mapping.disposition == Disposition.UNKNOWN
    assert "unknown" in mapping.reason.lower()
