"""password_resets; auth_attempts kind 'reset'

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "password_resets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_password_resets_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_resets")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_password_resets_token_hash")),
    )
    with op.batch_alter_table("password_resets", schema=None) as batch_op:
        batch_op.create_index("ix_password_resets_user_id", ["user_id"], unique=False)
    with op.batch_alter_table("auth_attempts", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_auth_attempts_kind_valid"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_auth_attempts_kind_valid"), "kind IN ('login', 'register', 'setup', 'reset')"
        )


def downgrade() -> None:
    with op.batch_alter_table("auth_attempts", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_auth_attempts_kind_valid"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_auth_attempts_kind_valid"), "kind IN ('login', 'register', 'setup')"
        )
    with op.batch_alter_table("password_resets", schema=None) as batch_op:
        batch_op.drop_index("ix_password_resets_user_id")
    op.drop_table("password_resets")
