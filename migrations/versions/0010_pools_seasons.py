"""pools, pool_entries; seasons.goal_reached_at / goal_observation_id (D-043)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pools",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("rule", sa.String(length=32), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("buy_in_cents", sa.Integer(), nullable=False),
        sa.Column("lock_at", sa.DateTime(), nullable=False),
        sa.Column("settle_after", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result_x10", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.JSON(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("ai_run_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'locked', 'settled', 'refunded')",
            name=op.f("ck_pools_status_valid"),
        ),
        sa.CheckConstraint("buy_in_cents > 0", name=op.f("ck_pools_buy_in_positive")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_pools_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["ai_run_id"], ["ai_runs.id"], name=op.f("fk_pools_ai_run_id_ai_runs")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_pools_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["season_id"], ["seasons.id"], name=op.f("fk_pools_season_id_seasons")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pools")),
        sa.UniqueConstraint("account_id", name=op.f("uq_pools_account_id")),
    )
    with op.batch_alter_table("pools", schema=None) as batch_op:
        batch_op.create_index("ix_pools_status_lock_at", ["status", "lock_at"], unique=False)

    op.create_table(
        "pool_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pool_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("guess_x10", sa.Integer(), nullable=False),
        sa.Column("payout_cents", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("guess_x10 > 0", name=op.f("ck_pool_entries_guess_positive")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_pool_entries_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["pool_id"], ["pools.id"], name=op.f("fk_pool_entries_pool_id_pools")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_pool_entries_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pool_entries")),
        sa.UniqueConstraint("pool_id", "user_id", name=op.f("uq_pool_entries_pool_id_user_id")),
    )

    with op.batch_alter_table("seasons", schema=None) as batch_op:
        batch_op.add_column(sa.Column("goal_reached_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("goal_observation_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("seasons", schema=None) as batch_op:
        batch_op.drop_column("goal_observation_id")
        batch_op.drop_column("goal_reached_at")
    op.drop_table("pool_entries")
    with op.batch_alter_table("pools", schema=None) as batch_op:
        batch_op.drop_index("ix_pools_status_lock_at")
    op.drop_table("pools")
