"""Outbox: every external side effect is a row written in the same transaction as the
business change, delivered later by the worker."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class OutboxMessage(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint("channel IN ('discord', 'email')", name="channel_valid"),
        CheckConstraint("status IN ('pending', 'sent', 'skipped', 'dead')", name="status_valid"),
        Index("ix_outbox_status_next_attempt_at", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
