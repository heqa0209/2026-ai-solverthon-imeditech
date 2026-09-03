"""Guarantee one current decision for each user and announcement."""

import sqlalchemy as sa
from alembic import op

revision = "0002_current_decision_unique"
down_revision = "0001_mvp_schema"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_current_decision_user_announcement"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "eligibility_decisions",
        ["user_id", "announcement_id"],
        unique=True,
        if_not_exists=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="eligibility_decisions", if_exists=True)
