SYSTEM_TAG = "superset-devin-remediator"
OPERATION_TAG_PREFIX = "op:"


def operation_key(case_id: object, kind: str, ordinal: int) -> str:
    return f"{OPERATION_TAG_PREFIX}{case_id}:{kind}:{ordinal}"


def remediation_operation_key(
    case_id: object, triage_result_hash: str, base_sha: str, ordinal: int
) -> str:
    """Unique per case, phase, approved triage result, pinned base commit and attempt.

    The full hashes are kept (a 64-hex sha256 plus a 40-hex SHA): the key is ~165
    characters, fits the 255-character `attempts.operation_key` column and is used verbatim
    as the Devin session tag, so the durable identity and the external tag are the same
    exact string.
    """
    if len(triage_result_hash) != 64 or len(base_sha) != 40:
        raise ValueError("operation key requires the full triage hash and base SHA")
    return f"{OPERATION_TAG_PREFIX}{case_id}:REMEDIATION:{triage_result_hash}:{base_sha}:{ordinal}"


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
