"""ledger: seasons, users, accounts, ledger_txns, ledger_entries

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02

Append-only triggers on ledger_txns/ledger_entries are created here. Any later
batch migration that rebuilds those tables must recreate them (a test checks
that they exist at head).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("ledger_txns", "ledger_entries")


def upgrade() -> None:
    op.create_table(
        "seasons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seasons")),
        sa.UniqueConstraint("number", name=op.f("uq_seasons_number")),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_seasons_one_open ON seasons ((ended_at IS NULL)) "
        "WHERE ended_at IS NULL"
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("balance_cents", sa.BigInteger(), nullable=False),
        sa.Column("pnl_cents", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('player', 'mint', 'house', 'pool')", name=op.f("ck_accounts_kind_valid")
        ),
        sa.CheckConstraint(
            "(kind = 'player') = (user_id IS NOT NULL)", name=op.f("ck_accounts_player_has_user")
        ),
        sa.CheckConstraint(
            "kind != 'player' OR balance_cents >= 0",
            name=op.f("ck_accounts_player_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["season_id"], ["seasons.id"], name=op.f("fk_accounts_season_id_seasons")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_accounts_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )
    op.create_index(
        "uq_accounts_player_per_season",
        "accounts",
        ["season_id", "user_id"],
        unique=True,
        sqlite_where=sa.text("kind = 'player'"),
    )
    op.create_index(
        "uq_accounts_system_per_season",
        "accounts",
        ["season_id", "kind"],
        unique=True,
        sqlite_where=sa.text("kind IN ('mint', 'house')"),
    )
    op.create_table(
        "ledger_txns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("ref_type", sa.String(length=32), nullable=True),
        sa.Column("ref_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_ledger_txns_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_txns")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_ledger_txns_idempotency_key")),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("txn_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.CheckConstraint("amount_cents != 0", name=op.f("ck_ledger_entries_amount_not_zero")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_ledger_entries_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["txn_id"], ["ledger_txns.id"], name=op.f("fk_ledger_entries_txn_id_ledger_txns")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_entries")),
    )
    op.create_index(op.f("ix_ledger_entries_account_id"), "ledger_entries", ["account_id"])
    op.create_index(op.f("ix_ledger_entries_txn_id"), "ledger_entries", ["txn_id"])

    for table in APPEND_ONLY:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_{action.lower()} BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
            )


def downgrade() -> None:
    for table in APPEND_ONLY:
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_{action}")
    op.drop_index(op.f("ix_ledger_entries_txn_id"), table_name="ledger_entries")
    op.drop_index(op.f("ix_ledger_entries_account_id"), table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_table("ledger_txns")
    op.drop_index("uq_accounts_system_per_season", table_name="accounts")
    op.drop_index("uq_accounts_player_per_season", table_name="accounts")
    op.drop_table("accounts")
    op.drop_table("users")
    op.execute("DROP INDEX IF EXISTS uq_seasons_one_open")
    op.drop_table("seasons")
