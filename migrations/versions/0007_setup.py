"""settings for /setup, seasons goal, secrets, setup token and draft

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-03

Adding columns uses plain ALTER TABLE (no table rebuild), so the seasons expression
index and the append-only triggers on other tables are untouched. The CHECK changes
rebuild only `settings` and `auth_attempts`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secrets",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_secrets")),
    )
    op.create_table(
        "setup_draft",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("session_expires_at", sa.DateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_setup_draft_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_setup_draft")),
    )
    op.create_table(
        "setup_tokens",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_setup_tokens")),
    )
    with op.batch_alter_table("seasons", schema=None) as batch_op:
        batch_op.add_column(sa.Column("start_weight_x10", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("goal_weight_x10", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("direction", sa.String(length=4), nullable=True))

    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "app_name", sa.String(length=40), server_default="WeightPicks", nullable=False
            )
        )
        batch_op.add_column(
            sa.Column("palette", sa.String(length=16), server_default="ember", nullable=False)
        )
        batch_op.add_column(sa.Column("subject_name", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("economy", sa.JSON(), server_default="{}", nullable=False))
        batch_op.add_column(
            sa.Column("ai_mode", sa.String(length=8), server_default="review", nullable=False)
        )
        batch_op.add_column(sa.Column("flags", sa.JSON(), server_default="{}", nullable=False))
        batch_op.add_column(sa.Column("smtp", sa.JSON(), server_default="{}", nullable=False))
        batch_op.add_column(sa.Column("setup_completed_at", sa.DateTime(), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_settings_ai_mode_valid"), "ai_mode IN ('review', 'auto')"
        )

    with op.batch_alter_table("auth_attempts", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_auth_attempts_kind_valid"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_auth_attempts_kind_valid"), "kind IN ('login', 'register', 'setup')"
        )


def downgrade() -> None:
    with op.batch_alter_table("auth_attempts", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_auth_attempts_kind_valid"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_auth_attempts_kind_valid"), "kind IN ('login', 'register')"
        )
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_settings_ai_mode_valid"), type_="check")
        batch_op.drop_column("setup_completed_at")
        batch_op.drop_column("smtp")
        batch_op.drop_column("flags")
        batch_op.drop_column("ai_mode")
        batch_op.drop_column("economy")
        batch_op.drop_column("subject_name")
        batch_op.drop_column("palette")
        batch_op.drop_column("app_name")

    with op.batch_alter_table("seasons", schema=None) as batch_op:
        batch_op.drop_column("direction")
        batch_op.drop_column("goal_weight_x10")
        batch_op.drop_column("start_weight_x10")
    # Dropping columns rebuilds `seasons`, which loses the expression index from 0002.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_seasons_one_open ON seasons ((ended_at IS NULL)) "
        "WHERE ended_at IS NULL"
    )

    op.drop_table("setup_tokens")
    op.drop_table("setup_draft")
    op.drop_table("secrets")
