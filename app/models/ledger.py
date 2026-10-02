"""Seasons, users (minimal), accounts and the append-only ledger (BUILD_PLAN §1.3).

Database backstops for the money rules (the service enforces them first):
- player balances can never be negative (CHECK, D-026);
- `ledger_txns` and `ledger_entries` reject UPDATE and DELETE (triggers in migration 0002).
"""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = (
        # At most one season that has not ended.
        Index(
            "uq_seasons_one_open",
            text("(ended_at IS NULL)"),
            unique=True,
            sqlite_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(String(16))  # active | frozen | ended
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # normalized lowercase
    display_name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))  # admin | player
    status: Mapped[str] = mapped_column(String(16))  # active | frozen | banned
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("kind IN ('player', 'mint', 'house', 'pool')", name="kind_valid"),
        CheckConstraint("(kind = 'player') = (user_id IS NOT NULL)", name="player_has_user"),
        CheckConstraint("kind != 'player' OR balance_cents >= 0", name="player_not_negative"),
        Index(
            "uq_accounts_player_per_season",
            "season_id",
            "user_id",
            unique=True,
            sqlite_where=text("kind = 'player'"),
        ),
        Index(
            "uq_accounts_system_per_season",
            "season_id",
            "kind",
            unique=True,
            sqlite_where=text("kind IN ('mint', 'house')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)  # cache of Σ entries
    pnl_cents: Mapped[int] = mapped_column(BigInteger, default=0)  # cache of Σ betting entries
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class LedgerTxn(Base):
    __tablename__ = "ledger_txns"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[int | None]
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    memo: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (CheckConstraint("amount_cents != 0", name="amount_not_zero"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    txn_id: Mapped[int] = mapped_column(ForeignKey("ledger_txns.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
