"""settings.public_url (issue #17), an observations (metric, observed_at) index,
and the time zone in market dedupe keys

Weigh-in and workout days are worked out from `observed_at` in the instance's current
time zone, so reads range over `observed_at`. Plain ADD COLUMN and CREATE INDEX: no
table rebuild, so the append-only triggers on `observations` stay as they are.
Existing markets get the instance's zone appended to their dedupe key, matching the new
`template:{params}@Zone` format, so duplicate checks keep finding them.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("settings", sa.Column("public_url", sa.String(length=255), nullable=True))
    op.create_index("ix_observations_metric_observed_at", "observations", ["metric", "observed_at"])
    op.execute(
        "UPDATE markets SET dedupe_key = dedupe_key || '@' || "
        "(SELECT timezone FROM settings WHERE id = 1) "
        "WHERE instr(dedupe_key, '@') = 0 AND EXISTS (SELECT 1 FROM settings WHERE id = 1)"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE markets SET dedupe_key = substr(dedupe_key, 1, instr(dedupe_key, '@') - 1) "
        "WHERE instr(dedupe_key, '@') > 0"
    )
    op.drop_index("ix_observations_metric_observed_at", table_name="observations")
    with op.batch_alter_table("settings") as batch:
        batch.drop_column("public_url")
