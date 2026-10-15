"""markets.lock_note: why the admin locked a market early, shown to players (issue #60)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("markets", sa.Column("lock_note", sa.String(length=200), nullable=True))


def downgrade() -> None:
    # Plain DROP COLUMN (SQLite 3.35+): a batch rebuild of `markets` would break the
    # foreign keys that point at it.
    op.execute("ALTER TABLE markets DROP COLUMN lock_note")
