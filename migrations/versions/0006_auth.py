"""sessions, auth_attempts, registration_codes, banned_emails

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('login', 'register')", name=op.f("ck_auth_attempts_kind_valid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_attempts")),
    )
    with op.batch_alter_table("auth_attempts", schema=None) as batch_op:
        batch_op.create_index("ix_auth_attempts_kind_ip_ts", ["kind", "ip", "ts"], unique=False)

    op.create_table(
        "banned_emails",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("banned_at", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("email", name=op.f("pk_banned_emails")),
    )
    op.create_table(
        "registration_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_registration_codes")),
    )
    with op.batch_alter_table("registration_codes", schema=None) as batch_op:
        batch_op.create_index(
            "uq_registration_codes_one_active",
            ["active"],
            unique=True,
            sqlite_where=sa.text("active = 1"),
        )

    op.create_table(
        "sessions",
        sa.Column("id_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users")),
        sa.PrimaryKeyConstraint("id_hash", name=op.f("pk_sessions")),
    )
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sessions_expires_at"), ["expires_at"], unique=False)
        batch_op.create_index("ix_sessions_user_id", ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_sessions_user_id")
        batch_op.drop_index(batch_op.f("ix_sessions_expires_at"))

    op.drop_table("sessions")
    with op.batch_alter_table("registration_codes", schema=None) as batch_op:
        batch_op.drop_index("uq_registration_codes_one_active", sqlite_where=sa.text("active = 1"))

    op.drop_table("registration_codes")
    op.drop_table("banned_emails")
    with op.batch_alter_table("auth_attempts", schema=None) as batch_op:
        batch_op.drop_index("ix_auth_attempts_kind_ip_ts")

    op.drop_table("auth_attempts")
