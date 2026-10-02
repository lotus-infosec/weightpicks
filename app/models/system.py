from datetime import datetime

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class Heartbeat(Base):
    """Last time each long-running component proved it was alive."""

    __tablename__ = "heartbeats"

    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(UTCDateTime())


class JobRun(Base):
    """One row per (job, period): the guarantee that a scheduled job runs exactly once."""

    __tablename__ = "job_runs"
    __table_args__ = (UniqueConstraint("job", "period_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(64))
    period_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # running | ok | error
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)
