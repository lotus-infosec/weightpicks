"""settings, markets, selections, odds_versions

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("unit", sa.String(length=2), nullable=False),
        sa.Column("schedule", sa.JSON(), nullable=False),
        sa.Column("enabled_metrics", sa.JSON(), nullable=False),
        sa.Column("instance_state", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "instance_state IN ('active', 'frozen')", name=op.f("ck_settings_state_valid")
        ),
        sa.CheckConstraint("unit IN ('lb', 'kg')", name=op.f("ck_settings_unit_valid")),
        sa.CheckConstraint("id = 1", name=op.f("ck_settings_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settings")),
    )
    op.create_table(
        "markets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("template", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("opens_at", sa.DateTime(), nullable=False),
        sa.Column("lock_at", sa.DateTime(), nullable=False),
        sa.Column("settle_after", sa.DateTime(), nullable=False),
        sa.Column("settle_deadline", sa.DateTime(), nullable=True),
        sa.Column("correlation_keys", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False),
        sa.Column("ai_run_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "origin IN ('core', 'ai', 'admin')", name=op.f("ck_markets_origin_valid")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'open', 'locked', 'settled', 'voided', "
            "'rejected')",
            name=op.f("ck_markets_status_valid"),
        ),
        sa.CheckConstraint("window_end >= window_start", name=op.f("ck_markets_window_ordered")),
        sa.ForeignKeyConstraint(
            ["season_id"], ["seasons.id"], name=op.f("fk_markets_season_id_seasons")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_markets")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_markets_dedupe_key")),
    )
    with op.batch_alter_table("markets", schema=None) as batch_op:
        batch_op.create_index("ix_markets_status_lock_at", ["status", "lock_at"], unique=False)

    op.create_table(
        "odds_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("line_x10", sa.Integer(), nullable=True),
        sa.Column("odds", sa.JSON(), nullable=False),
        sa.Column("model_inputs", sa.JSON(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["market_id"], ["markets.id"], name=op.f("fk_odds_versions_market_id_markets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_odds_versions")),
        sa.UniqueConstraint(
            "market_id", "version", name=op.f("uq_odds_versions_market_id_version")
        ),
    )
    with op.batch_alter_table("odds_versions", schema=None) as batch_op:
        batch_op.create_index(
            "uq_odds_versions_one_current",
            ["market_id"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        )

    op.create_table(
        "selections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.CheckConstraint(
            "side IN ('over', 'under', 'yes', 'no')", name=op.f("ck_selections_side_valid")
        ),
        sa.ForeignKeyConstraint(
            ["market_id"], ["markets.id"], name=op.f("fk_selections_market_id_markets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_selections")),
        sa.UniqueConstraint("market_id", "side", name=op.f("uq_selections_market_id_side")),
    )


def downgrade() -> None:
    op.drop_table("selections")
    with op.batch_alter_table("odds_versions", schema=None) as batch_op:
        batch_op.drop_index("uq_odds_versions_one_current", sqlite_where=sa.text("is_current = 1"))

    op.drop_table("odds_versions")
    with op.batch_alter_table("markets", schema=None) as batch_op:
        batch_op.drop_index("ix_markets_status_lock_at")

    op.drop_table("markets")
    op.drop_table("settings")
