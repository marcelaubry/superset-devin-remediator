"""attempts.ordinal: allocated under a case lock, unique per (case, kind)"""

import sqlalchemy as sa

from alembic import op

revision = "0005_attempt_ordinal"
down_revision = "0004_phase4_remediation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("ordinal", sa.Integer(), nullable=True))
    # Existing rows encoded the ordinal as the last ':'-separated field of idempotency_key.
    op.execute(
        sa.text(
            "UPDATE attempts SET ordinal = CAST(split_part(idempotency_key, ':', 3) AS INTEGER) "
            "WHERE idempotency_key ~ '^[0-9a-f-]{36}:[A-Z_]+:[0-9]+$'"
        )
    )
    op.create_index(
        "uq_attempts_case_kind_ordinal",
        "attempts",
        ["case_id", "kind", "ordinal"],
        unique=True,
        postgresql_where=sa.text("ordinal IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_attempts_case_kind_ordinal", table_name="attempts")
    op.drop_column("attempts", "ordinal")
