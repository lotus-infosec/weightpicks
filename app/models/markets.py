"""Markets, their two selections and their priced odds versions (BUILD_PLAN §1.3, §1.4.3).

Status changes are decided by `app.domain.markets.transition` only. `dedupe_key`
(template + canonical params) is unique, so a repeated or restarted drop can never
create a second copy of a market. At most one odds version per market is current.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime

MARKET_STATUSES = "'draft', 'pending_approval', 'open', 'locked', 'settled', 'voided', 'rejected'"


class Market(Base):
    __tablename__ = "markets"
    __table_args__ = (
        CheckConstraint(f"status IN ({MARKET_STATUSES})", name="status_valid"),
        CheckConstraint("origin IN ('core', 'ai', 'admin')", name="origin_valid"),
        CheckConstraint("window_end >= window_start", name="window_ordered"),
        Index("ix_markets_status_lock_at", "status", "lock_at"),
        Index("ix_markets_ai_run_id", "ai_run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    template: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(16))  # daily | weekly | monthly (+ later)
    metric: Mapped[str] = mapped_column(String(32))
    window_start: Mapped[date] = mapped_column(Date)
    window_end: Mapped[date] = mapped_column(Date)
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(16))
    opens_at: Mapped[datetime] = mapped_column(UTCDateTime())
    lock_at: Mapped[datetime] = mapped_column(UTCDateTime())
    settle_after: Mapped[datetime] = mapped_column(UTCDateTime())
    settle_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime())
    correlation_keys: Mapped[list[str]] = mapped_column(JSON)
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True)
    ai_run_id: Mapped[int | None]  # the ai_runs row that proposed it (origin 'ai')
    blurb: Mapped[str | None] = mapped_column(String(160))  # AI flavour text, never terms
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status_changed_at: Mapped[datetime] = mapped_column(UTCDateTime())


class Selection(Base):
    __tablename__ = "selections"
    __table_args__ = (
        UniqueConstraint("market_id", "side"),
        CheckConstraint("side IN ('over', 'under', 'yes', 'no')", name="side_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    side: Mapped[str] = mapped_column(String(8))


class OddsVersion(Base):
    __tablename__ = "odds_versions"
    __table_args__ = (
        UniqueConstraint("market_id", "version"),
        Index(
            "uq_odds_versions_one_current",
            "market_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    version: Mapped[int]
    line_x10: Mapped[int | None]  # None for yes/no markets
    # side -> American odds; None = that side is not offered
    odds: Mapped[dict[str, int | None]] = mapped_column(JSON)
    model_inputs: Mapped[dict[str, Any]] = mapped_column(JSON)
    is_current: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
