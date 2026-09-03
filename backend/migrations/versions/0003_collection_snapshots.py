"""Add collection snapshots and profile-scoped AI traceability."""

import sqlalchemy as sa
from alembic import op

revision = "0003_collection_snapshots"
down_revision = "0002_current_decision_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("ai_stage_runs")}
    if "company_profile_version_id" not in columns:
        with op.batch_alter_table("ai_stage_runs") as batch:
            batch.add_column(
                sa.Column(
                    "company_profile_version_id",
                    sa.String(length=36),
                    sa.ForeignKey("company_profile_versions.id"),
                    nullable=True,
                )
            )
            batch.create_index(
                "ix_ai_stage_runs_company_profile_version_id",
                ["company_profile_version_id"],
            )
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "collection_snapshots" not in tables:
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
            "ix_collection_snapshots_succeeded_at",
            "collection_snapshots",
            ["succeeded_at"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "collection_snapshots" in tables:
        op.drop_index("ix_collection_snapshots_succeeded_at", table_name="collection_snapshots")
        op.drop_index("ix_collection_snapshots_scope", table_name="collection_snapshots")
        op.drop_table("collection_snapshots")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("ai_stage_runs")}
    if "company_profile_version_id" in columns:
        with op.batch_alter_table("ai_stage_runs") as batch:
            batch.drop_index("ix_ai_stage_runs_company_profile_version_id")
            batch.drop_column("company_profile_version_id")
