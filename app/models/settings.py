"""The singleton `settings` row (BUILD_PLAN §1.3), plus encrypted `secrets` and the
first-run `/setup` state (token hashes and the wizard draft)."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class InstanceSettingsRow(Base):
    __tablename__ = "settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("instance_state IN ('active', 'frozen')", name="state_valid"),
        CheckConstraint("unit IN ('lb', 'kg')", name="unit_valid"),
        CheckConstraint("ai_mode IN ('review', 'auto')", name="ai_mode_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    timezone: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(2))
    schedule: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled_metrics: Mapped[list[str]] = mapped_column(JSON)
    instance_state: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    # STAGE08: everything /setup collects (D-036). Server defaults keep older rows valid.
    app_name: Mapped[str] = mapped_column(String(40), server_default="WeightPicks")
    palette: Mapped[str] = mapped_column(String(16), server_default="ember")
    subject_name: Mapped[str | None] = mapped_column(String(40))
    economy: Mapped[dict[str, Any]] = mapped_column(JSON, server_default="{}")
    ai_mode: Mapped[str] = mapped_column(String(8), server_default="review")
    flags: Mapped[dict[str, Any]] = mapped_column(JSON, server_default="{}")
    smtp: Mapped[dict[str, Any]] = mapped_column(JSON, server_default="{}")  # no password
    setup_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Secret(Base):
    """Integration secrets (Workers AI token, webhook URLs, SMTP password), Fernet-encrypted
    with a key derived from APP_SECRET_KEY (app.core.crypto)."""

    __tablename__ = "secrets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class SetupToken(Base):
    """One-time setup tokens; only the SHA-256 is stored (BUILD_PLAN §1.5)."""

    __tablename__ = "setup_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class SetupDraft(Base):
    """The wizard in progress: who holds the setup session and what they've entered."""

    __tablename__ = "setup_draft"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_hash: Mapped[str] = mapped_column(String(64))
    session_expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
