"""The singleton `settings` row (BUILD_PLAN §1.3). STAGE05 holds the minimum: zone,
unit, schedule, enabled metrics and instance state; /setup (STAGE08) adds the rest."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class InstanceSettingsRow(Base):
    __tablename__ = "settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("instance_state IN ('active', 'frozen')", name="state_valid"),
        CheckConstraint("unit IN ('lb', 'kg')", name="unit_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    timezone: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(2))
    schedule: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled_metrics: Mapped[list[str]] = mapped_column(JSON)
    instance_state: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
