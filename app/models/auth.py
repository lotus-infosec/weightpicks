"""Sessions, auth attempts (rate limits), registration codes and banned emails
(BUILD_PLAN §1.3, §1.6). Tokens and codes are stored only as SHA-256 hashes."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_user_id", "user_id"),)

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(token) hex
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    csrf_token: Mapped[str] = mapped_column(String(64))  # synchronizer token (D-034)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(200))


class AuthAttempt(Base):
    __tablename__ = "auth_attempts"
    __table_args__ = (
        CheckConstraint("kind IN ('login', 'register', 'setup', 'reset')", name="kind_valid"),
        Index("ix_auth_attempts_kind_ip_ts", "kind", "ip", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    ip: Mapped[str] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(320))  # normalized
    ok: Mapped[bool]
    ts: Mapped[datetime] = mapped_column(UTCDateTime())


class RegistrationCode(Base):
    __tablename__ = "registration_codes"
    __table_args__ = (
        Index(
            "uq_registration_codes_one_active",
            "active",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class BannedEmail(Base):
    __tablename__ = "banned_emails"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)  # normalized
    banned_at: Mapped[datetime] = mapped_column(UTCDateTime())
    reason: Mapped[str | None] = mapped_column(String(200))


class PasswordReset(Base):
    """A password-reset link sent by email (STAGE15): only the token's hash is stored;
    single use, short-lived."""

    __tablename__ = "password_resets"
    __table_args__ = (Index("ix_password_resets_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
