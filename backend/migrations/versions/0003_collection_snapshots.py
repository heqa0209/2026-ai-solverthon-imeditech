"""Add collection snapshots and profile-scoped AI traceability."""

import sqlalchemy as sa
from alembic import op

revision = "0003_collection_snapshots"
down_revision = "0002_current_decision_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_stage_runs",
        sa.Column("company_profile_version_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_ai_stage_runs_company_profile_version_id",
        "ai_stage_runs",
        "company_profile_versions",
        ["company_profile_version_id"],
        ["id"],
    )
    op.create_index(
        "ix_ai_stage_runs_company_profile_version_id",
        "ai_stage_runs",
        ["company_profile_version_id"],
    )
    op.create_table(
        "collection_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("source_ids", sa.JSON(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("succeeded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_collection_snapshots_scope", "collection_snapshots", ["scope"])
    op.create_index(
        "ix_collection_snapshots_succeeded_at", "collection_snapshots", ["succeeded_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_collection_snapshots_succeeded_at", table_name="collection_snapshots")
    op.drop_index("ix_collection_snapshots_scope", table_name="collection_snapshots")
    op.drop_table("collection_snapshots")
    op.drop_index("ix_ai_stage_runs_company_profile_version_id", table_name="ai_stage_runs")
    op.drop_constraint(
        "fk_ai_stage_runs_company_profile_version_id",
        "ai_stage_runs",
        type_="foreignkey",
    )
    op.drop_column("ai_stage_runs", "company_profile_version_id")
