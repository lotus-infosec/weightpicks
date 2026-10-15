"""sync_runs.watch_upload_at / watch_device: the watch's last upload to Garmin Connect,
as seen by each sync (issue #57). Older runs stay NULL.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sync_runs", sa.Column("watch_upload_at", sa.DateTime(), nullable=True))
    op.add_column("sync_runs", sa.Column("watch_device", sa.String(length=80), nullable=True))


def downgrade() -> None:
    # Plain DROP COLUMN (SQLite 3.35+): a batch rebuild would break the foreign key
    # from observations.
    op.execute("ALTER TABLE sync_runs DROP COLUMN watch_device")
    op.execute("ALTER TABLE sync_runs DROP COLUMN watch_upload_at")
