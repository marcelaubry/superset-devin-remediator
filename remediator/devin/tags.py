SYSTEM_TAG = "superset-devin-remediator"
OPERATION_TAG_PREFIX = "op:"


def operation_key(case_id: object, kind: str, ordinal: int) -> str:
    return f"{OPERATION_TAG_PREFIX}{case_id}:{kind}:{ordinal}"


def remediation_operation_key(
    case_id: object, triage_result_hash: str, base_sha: str, ordinal: int
) -> str:
    """Unique per case, phase, approved triage result, pinned base commit and attempt."""
    return (
        f"{OPERATION_TAG_PREFIX}{case_id}:REMEDIATION:{triage_result_hash[:12]}:"
        f"{base_sha[:12]}:{ordinal}"
    )


def correlation_tags(
    repository: str, issue_number: int, kind: str, case_id: object, attempt_id: object
) -> tuple[str, ...]:
    return (
        SYSTEM_TAG,
        f"repo:{repository}",
        f"issue:{issue_number}",
        f"kind:{kind}",
        f"case:{case_id}",
        f"attempt:{attempt_id}",
    )


def tag_value(tags: tuple[str, ...] | list[str], prefix: str) -> str | None:
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None
