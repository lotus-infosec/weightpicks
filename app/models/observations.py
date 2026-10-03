"""Immutable snapshots of provider data (BUILD_PLAN §1.3). Settlement reads only these.

`observations` rejects UPDATE and DELETE (triggers in migration 0003). The canonical
weigh-in of a day is derived (earliest in-window weigh-in), never stored (D-027).
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, CheckConstraint, Date, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16))  # running | ok | failed
    rows_new: Mapped[int] = mapped_column(default=0)
    complete_through: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # metric -> ISO date
    error: Mapped[str | None] = mapped_column(Text)


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (
        Index("ix_observations_metric_local_date", "metric", "local_date"),
        Index("uq_observations_metric_source_ref", "metric", "source_ref", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    metric: Mapped[str] = mapped_column(String(32))  # weight | steps | ... | activity
    local_date: Mapped[date] = mapped_column(Date)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    value: Mapped[int] = mapped_column(BigInteger)  # weight: tenths of the unit
    raw_value: Mapped[int | None] = mapped_column(BigInteger)  # weight: grams
    source: Mapped[str] = mapped_column(String(16))  # scale | manual | <provider name>
    source_ref: Mapped[str] = mapped_column(String(200))
    in_window: Mapped[bool | None]  # weight only: inside the weigh-in window at ingest
    sync_run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id"))


class SimState(Base):
    """Dev only: the persisted simulation clock and simulator settings (one row)."""

    __tablename__ = "sim_state"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sim_now: Mapped[datetime] = mapped_column(UTCDateTime())
    preset: Mapped[str] = mapped_column(String(32))
    seed: Mapped[int]
    anchor_date: Mapped[date] = mapped_column(Date)
