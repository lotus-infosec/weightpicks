"""Bets, their legs, and market settlements (BUILD_PLAN §1.3, §1.4.2).

A bet pins the odds version it was placed at; each leg snapshots the American odds
and line. `settlements.market_id` is unique: inserting that row in the same
transaction as the payouts is what makes settlement idempotent.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime

BET_STATUSES = "'open', 'won', 'lost', 'push', 'void'"


class Bet(Base):
    __tablename__ = "bets"
    __table_args__ = (
        UniqueConstraint("user_id", "client_key"),
        CheckConstraint("kind IN ('single', 'parlay')", name="kind_valid"),
        CheckConstraint(f"status IN ({BET_STATUSES})", name="status_valid"),
        CheckConstraint("stake_cents >= 100", name="min_stake"),
        Index("ix_bets_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    kind: Mapped[str] = mapped_column(String(16))
    stake_cents: Mapped[int] = mapped_column(BigInteger)
    potential_payout_cents: Mapped[int] = mapped_column(BigInteger)  # stake + profit if won
    status: Mapped[str] = mapped_column(String(16))
    payout_cents: Mapped[int | None] = mapped_column(BigInteger)  # total returned at settle
    placed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    client_key: Mapped[str] = mapped_column(String(64))


class BetLeg(Base):
    __tablename__ = "bet_legs"
    __table_args__ = (
        CheckConstraint(f"status IN ({BET_STATUSES})", name="status_valid"),
        Index("ix_bet_legs_market_id_status", "market_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bet_id: Mapped[int] = mapped_column(ForeignKey("bets.id"))
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    selection_id: Mapped[int] = mapped_column(ForeignKey("selections.id"))
    odds_version_id: Mapped[int] = mapped_column(ForeignKey("odds_versions.id"))
    american: Mapped[int]  # snapshot at placement
    line_x10: Mapped[int | None]  # snapshot at placement
    status: Mapped[str] = mapped_column(String(16))


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), unique=True)
    outcome: Mapped[dict[str, Any]] = mapped_column(JSON)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON)  # observation ids + values used
    engine_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
