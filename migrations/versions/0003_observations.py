"""observations, sync_runs, sim_state

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02

`observations` is append-only (triggers below); any later batch migration that
rebuilds it must recreate them (a test checks they exist at head).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rows_new", sa.Integer(), nullable=False),
        sa.Column("complete_through", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index(op.f("ix_sync_runs_started_at"), "sync_runs", ["started_at"])
    op.create_table(
        "observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("raw_value", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_ref", sa.String(length=200), nullable=False),
        sa.Column("in_window", sa.Boolean(), nullable=True),
        sa.Column("sync_run_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["sync_run_id"], ["sync_runs.id"], name=op.f("fk_observations_sync_run_id_sync_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observations")),
    )
    op.create_index("ix_observations_metric_local_date", "observations", ["metric", "local_date"])
    op.create_index(
        "uq_observations_metric_source_ref", "observations", ["metric", "source_ref"], unique=True
    )
    op.create_table(
        "sim_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sim_now", sa.DateTime(), nullable=False),
        sa.Column("preset", sa.String(length=32), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_sim_state_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sim_state")),
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_observations_no_{action.lower()} BEFORE {action} ON observations "
            "BEGIN SELECT RAISE(ABORT, 'observations is append-only'); END"
        )


def downgrade() -> None:
    for action in ("update", "delete"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_observations_no_{action}")
    op.drop_table("sim_state")
    op.drop_index("uq_observations_metric_source_ref", table_name="observations")
    op.drop_index("ix_observations_metric_local_date", table_name="observations")
    op.drop_table("observations")
    op.drop_index(op.f("ix_sync_runs_started_at"), table_name="sync_runs")
    op.drop_table("sync_runs")
