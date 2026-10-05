"""Workers AI bookkeeping (BUILD_PLAN §1.3, §1.4.4; D-042): every AI call or skip, the
review queue of AI proposals, and admin notes that feed the digest."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Date, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class AiRun(Base):
    """One AI cycle: a call (running -> ok/error) or a skip (no token, quota...). A call
    reserves its estimated neurons as `running` before the request goes out. `started_at`
    is real UTC time: the neuron cap is Cloudflare's and resets at 00:00 UTC."""

    __tablename__ = "ai_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'ok', 'skipped', 'error')", name="status_valid"),
        Index("ix_ai_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    neurons_est: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16))
    skip_reason: Mapped[str | None] = mapped_column(String(64))
    raw_output: Mapped[str | None] = mapped_column(Text)
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)


class AiProposal(Base):
    """A validated AI prop waiting for the admin (review mode). `form` is the same field
    set the admin prop form posts, so approval re-builds and re-prices from live data."""

    __tablename__ = "ai_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')", name="status_valid"
        ),
        Index("ix_ai_proposals_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ai_run_id: Mapped[int] = mapped_column(ForeignKey("ai_runs.id"))
    template: Mapped[str] = mapped_column(String(64))
    form: Mapped[dict[str, Any]] = mapped_column(JSON)
    title: Mapped[str] = mapped_column(String(200))  # code-built title at proposal time
    blurb: Mapped[str | None] = mapped_column(String(160))
    dedupe_key: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(200))
    market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AdminNote(Base):
    """Free-text context for the AI digest ("Traveling Thu-Sun"), active for a date range."""

    __tablename__ = "admin_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(String(200))
    active_from: Mapped[date] = mapped_column(Date)
    active_to: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
