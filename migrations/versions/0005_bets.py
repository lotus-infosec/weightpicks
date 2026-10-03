"""bets, bet_legs, settlements, outbox

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("channel IN ('discord', 'email')", name=op.f("ck_outbox_channel_valid")),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'skipped', 'dead')", name=op.f("ck_outbox_status_valid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_outbox_dedupe_key")),
    )
    with op.batch_alter_table("outbox", schema=None) as batch_op:
        batch_op.create_index(
            "ix_outbox_status_next_attempt_at", ["status", "next_attempt_at"], unique=False
        )

    op.create_table(
        "bets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("stake_cents", sa.BigInteger(), nullable=False),
        sa.Column("potential_payout_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payout_cents", sa.BigInteger(), nullable=True),
        sa.Column("placed_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("client_key", sa.String(length=64), nullable=False),
        sa.CheckConstraint("kind IN ('single', 'parlay')", name=op.f("ck_bets_kind_valid")),
        sa.CheckConstraint(
            "status IN ('open', 'won', 'lost', 'push', 'void')", name=op.f("ck_bets_status_valid")
        ),
        sa.CheckConstraint("stake_cents >= 100", name=op.f("ck_bets_min_stake")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_bets_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["season_id"], ["seasons.id"], name=op.f("fk_bets_season_id_seasons")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_bets_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bets")),
        sa.UniqueConstraint("user_id", "client_key", name=op.f("uq_bets_user_id_client_key")),
    )
    with op.batch_alter_table("bets", schema=None) as batch_op:
        batch_op.create_index("ix_bets_status", ["status"], unique=False)

    op.create_table(
        "settlements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.JSON(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["market_id"], ["markets.id"], name=op.f("fk_settlements_market_id_markets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settlements")),
        sa.UniqueConstraint("market_id", name=op.f("uq_settlements_market_id")),
    )
    op.create_table(
        "bet_legs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bet_id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("selection_id", sa.Integer(), nullable=False),
        sa.Column("odds_version_id", sa.Integer(), nullable=False),
        sa.Column("american", sa.Integer(), nullable=False),
        sa.Column("line_x10", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'won', 'lost', 'push', 'void')",
            name=op.f("ck_bet_legs_status_valid"),
        ),
        sa.ForeignKeyConstraint(["bet_id"], ["bets.id"], name=op.f("fk_bet_legs_bet_id_bets")),
        sa.ForeignKeyConstraint(
            ["market_id"], ["markets.id"], name=op.f("fk_bet_legs_market_id_markets")
        ),
        sa.ForeignKeyConstraint(
            ["odds_version_id"],
            ["odds_versions.id"],
            name=op.f("fk_bet_legs_odds_version_id_odds_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["selection_id"], ["selections.id"], name=op.f("fk_bet_legs_selection_id_selections")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bet_legs")),
    )
    with op.batch_alter_table("bet_legs", schema=None) as batch_op:
        batch_op.create_index("ix_bet_legs_market_id_status", ["market_id", "status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("bet_legs", schema=None) as batch_op:
        batch_op.drop_index("ix_bet_legs_market_id_status")

    op.drop_table("bet_legs")
    op.drop_table("settlements")
    with op.batch_alter_table("bets", schema=None) as batch_op:
        batch_op.drop_index("ix_bets_status")

    op.drop_table("bets")
    with op.batch_alter_table("outbox", schema=None) as batch_op:
        batch_op.drop_index("ix_outbox_status_next_attempt_at")

    op.drop_table("outbox")
