"""heartbeats and job_runs

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "heartbeats",
        sa.Column("component", sa.String(length=32), nullable=False),
        sa.Column("beat_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("component", name=op.f("pk_heartbeats")),
    )
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job", sa.String(length=64), nullable=False),
        sa.Column("period_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_runs")),
        sa.UniqueConstraint("job", "period_key", name=op.f("uq_job_runs_job_period_key")),
    )


def downgrade() -> None:
    op.drop_table("job_runs")
    op.drop_table("heartbeats")
