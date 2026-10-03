"""audit_log (append-only), busts, commands

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name=op.f("fk_audit_log_actor_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index("ix_audit_log_ts", ["ts"], unique=False)

    op.create_table(
        "busts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("busted_at", sa.DateTime(), nullable=False),
        sa.Column("bailed_out_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["season_id"], ["seasons.id"], name=op.f("fk_busts_season_id_seasons")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_busts_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_busts")),
    )
    with op.batch_alter_table("busts", schema=None) as batch_op:
        batch_op.create_index("ix_busts_season_id_user_id", ["season_id", "user_id"], unique=False)

    op.create_table(
        "commands",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("done_at", sa.DateTime(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name=op.f("ck_commands_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_commands_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commands")),
    )
    with op.batch_alter_table("commands", schema=None) as batch_op:
        batch_op.create_index("ix_commands_status", ["status"], unique=False)

    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_audit_log_no_{action.lower()} BEFORE {action} ON audit_log "
            "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
        )


def downgrade() -> None:
    for action in ("update", "delete"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_audit_log_no_{action}")
    with op.batch_alter_table("commands", schema=None) as batch_op:
        batch_op.drop_index("ix_commands_status")

    op.drop_table("commands")
    with op.batch_alter_table("busts", schema=None) as batch_op:
        batch_op.drop_index("ix_busts_season_id_user_id")

    op.drop_table("busts")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_log_ts")

    op.drop_table("audit_log")
