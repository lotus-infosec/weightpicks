"""markets.void_reason / void_note: why a market was voided, shown to players (issue #44)

Markets voided before this migration get their reason from the audit log: an admin void
(`market.void`) keeps the admin's typed reason; markets voided by Goal Reached are those
of a season whose goal was reached, voided at or after that moment.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("markets", sa.Column("void_reason", sa.String(length=32), nullable=True))
    op.add_column("markets", sa.Column("void_note", sa.String(length=200), nullable=True))
    op.execute(
        "UPDATE markets SET void_reason = 'admin', void_note = ("
        "  SELECT substr(trim(a.reason), 1, 200) FROM audit_log a"
        "  WHERE a.action = 'market.void' AND a.target_type = 'market'"
        "    AND a.target_id = markets.id ORDER BY a.id DESC LIMIT 1)"
        " WHERE status = 'voided' AND EXISTS ("
        "  SELECT 1 FROM audit_log a WHERE a.action = 'market.void'"
        "    AND a.target_type = 'market' AND a.target_id = markets.id)"
    )
    op.execute(
        "UPDATE markets SET void_reason = 'goal_reached'"
        " WHERE status = 'voided' AND void_reason IS NULL AND EXISTS ("
        "  SELECT 1 FROM seasons s WHERE s.id = markets.season_id"
        "    AND s.goal_reached_at IS NOT NULL"
        "    AND markets.status_changed_at >= s.goal_reached_at)"
    )


def downgrade() -> None:
    # Plain DROP COLUMN (SQLite 3.35+): a batch rebuild of `markets` would break the
    # foreign keys that point at it.
    op.execute("ALTER TABLE markets DROP COLUMN void_note")
    op.execute("ALTER TABLE markets DROP COLUMN void_reason")
