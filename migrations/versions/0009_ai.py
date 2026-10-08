"""ai_runs, ai_proposals, admin_notes; markets.blurb (D-042)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("neurons_est", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("skip_reason", sa.String(length=64), nullable=True),
        sa.Column("raw_output", sa.Text(), nullable=True),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'skipped', 'error')", name=op.f("ck_ai_runs_status_valid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_runs")),
    )
    with op.batch_alter_table("ai_runs", schema=None) as batch_op:
        batch_op.create_index("ix_ai_runs_started_at", ["started_at"], unique=False)

    op.create_table(
        "ai_proposals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ai_run_id", sa.Integer(), nullable=False),
        sa.Column("template", sa.String(length=64), nullable=False),
        sa.Column("form", sa.JSON(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("blurb", sa.String(length=160), nullable=True),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("market_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name=op.f("ck_ai_proposals_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["ai_run_id"], ["ai_runs.id"], name=op.f("fk_ai_proposals_ai_run_id_ai_runs")
        ),
        sa.ForeignKeyConstraint(
            ["market_id"], ["markets.id"], name=op.f("fk_ai_proposals_market_id_markets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_proposals")),
    )
    with op.batch_alter_table("ai_proposals", schema=None) as batch_op:
        batch_op.create_index("ix_ai_proposals_status", ["status"], unique=False)

    op.create_table(
        "admin_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(length=200), nullable=False),
        sa.Column("active_from", sa.Date(), nullable=False),
        sa.Column("active_to", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_admin_notes_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_notes")),
    )

    with op.batch_alter_table("markets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("blurb", sa.String(length=160), nullable=True))
        batch_op.create_index("ix_markets_ai_run_id", ["ai_run_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("markets", schema=None) as batch_op:
        batch_op.drop_index("ix_markets_ai_run_id")
        batch_op.drop_column("blurb")
    op.drop_table("admin_notes")
    with op.batch_alter_table("ai_proposals", schema=None) as batch_op:
        batch_op.drop_index("ix_ai_proposals_status")
    op.drop_table("ai_proposals")
    with op.batch_alter_table("ai_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_ai_runs_started_at")
    op.drop_table("ai_runs")
