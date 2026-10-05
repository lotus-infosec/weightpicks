"""Backfill settings.setup_completed_at for databases bootstrapped before /setup (STAGE16)

Before STAGE08 an admin was created from the CLI, and "an admin exists" counted as setup
done. That fallback is gone; this marks those databases as set up instead.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE settings SET setup_completed_at = CURRENT_TIMESTAMP "
        "WHERE setup_completed_at IS NULL "
        "AND EXISTS (SELECT 1 FROM users WHERE role = 'admin')"
    )


def downgrade() -> None:
    pass  # data only: a completed setup stays completed
