"""Special events: Price Is Right pools and their entries (BUILD_PLAN §1.3, D-010, D-043).
Each pool has its own escrow account (kind `pool`); buy-ins, payouts and refunds move
money between it and the players' accounts."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime

POOL_STATUSES = "'open', 'locked', 'settled', 'refunded'"


class Pool(Base):
    __tablename__ = "pools"
    __table_args__ = (
        CheckConstraint(f"status IN ({POOL_STATUSES})", name="status_valid"),
        CheckConstraint("buy_in_cents > 0", name="buy_in_positive"),
        Index("ix_pools_status_lock_at", "status", "lock_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), unique=True)
    title: Mapped[str] = mapped_column(String(80))
    question: Mapped[str] = mapped_column(Text)
    rule: Mapped[str] = mapped_column(String(32))  # price_is_right
    metric: Mapped[str] = mapped_column(String(32))  # weight
    target_date: Mapped[date] = mapped_column(Date)
    buy_in_cents: Mapped[int]
    lock_at: Mapped[datetime] = mapped_column(UTCDateTime())
    settle_after: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16))
    result_x10: Mapped[int | None]
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    ai_run_id: Mapped[int | None] = mapped_column(ForeignKey("ai_runs.id"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class PoolEntry(Base):
    __tablename__ = "pool_entries"
    __table_args__ = (
        UniqueConstraint("pool_id", "user_id"),
        CheckConstraint("guess_x10 > 0", name="guess_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    guess_x10: Mapped[int]
    payout_cents: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
