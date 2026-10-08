"""Password reset by email (BUILD_PLAN §1.5 `/reset/*`).

Only while SMTP is configured (and APP_SECRET_KEY is set). Requesting a reset always
looks the same, so it never reveals whether an address has an account. A link carries a
256-bit token; only its SHA-256 is stored, it expires after an hour and works once. The
outbox row holds the token encrypted (Fernet) until the email is sent, then drops it.
Setting a new password ends every session of that account.
"""

import secrets as pysecrets
from datetime import timedelta

import structlog
from sqlalchemy import Connection, Engine, insert, select, update

from app.core.clock import Clock
from app.core.config import Settings
from app.core.crypto import SecretKeyMissing, derive_fernet
from app.core.db import immediate
from app.core.security import hash_password, password_problem, token_hash
from app.models import AuthAttempt, PasswordReset, User
from app.notify import email
from app.services import audit
from app.services.audit import Actor
from app.services.auth import count_attempts, revoke_all
from app.services.outbox import Category, enqueue
from app.services.users import normalize_email

log = structlog.get_logger()

TOKEN_TTL = timedelta(hours=1)
PER_IP_PER_HOUR = 5
PER_EMAIL_PER_HOUR = 3


class ResetError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def enabled(conn: Connection, settings: Settings) -> bool:
    return email.configured(conn) and bool(settings.app_secret_key.get_secret_value())


def request(engine: Engine, clock: Clock, settings: Settings, *, address: str, ip: str) -> None:
    """Send a reset link if the address belongs to an active account. Silent otherwise;
    raises ResetError only for rate limits (same answer for any address)."""
    normalized = normalize_email(address)
    with immediate(engine) as conn:
        if not enabled(conn, settings):
            raise ResetError("disabled", "Password reset by email isn't available.")
        now = clock.now()
        hour = now - timedelta(hours=1)
        if (
            count_attempts(conn, "reset", hour, ip=ip) >= PER_IP_PER_HOUR
            or count_attempts(conn, "reset", hour, email=normalized) >= PER_EMAIL_PER_HOUR
        ):
            raise ResetError("rate_limited", "Too many requests. Try again in an hour.")
        conn.execute(
            insert(AuthAttempt).values(kind="reset", ip=ip, email=normalized, ok=True, ts=now)
        )
        user = conn.execute(
            select(User.id).where(User.email == normalized, User.status == "active")
        ).scalar_one_or_none()
        if user is None:
            log.info("password_reset_requested", known=False)
            return
        token = pysecrets.token_urlsafe(32)
        try:
            sealed = derive_fernet(settings.app_secret_key.get_secret_value()).encrypt(
                token.encode()
            )
        except SecretKeyMissing as exc:
            raise ResetError("disabled", "Password reset by email isn't available.") from exc
        reset_id = conn.execute(
            insert(PasswordReset)
            .values(
                user_id=user,
                token_hash=token_hash(token),
                created_at=now,
                expires_at=now + TOKEN_TTL,
            )
            .returning(PasswordReset.id)
        ).scalar_one()
        enqueue(
            conn,
            clock,
            category=Category.ACCOUNT_EMAIL,
            channel="email",
            payload={"kind": "password_reset", "user_id": user, "sealed": sealed.decode()},
            dedupe_key=f"password_reset:{reset_id}",
        )
    log.info("password_reset_requested", known=True)


def check(engine: Engine, clock: Clock, token: str) -> bool:
    """Is this link still usable (for showing the form)?"""
    with engine.connect() as conn:
        return _usable(conn, clock, token) is not None


def _usable(conn: Connection, clock: Clock, token: str) -> int | None:
    row = conn.execute(
        select(
            PasswordReset.id, PasswordReset.user_id, PasswordReset.expires_at, PasswordReset.used_at
        ).where(PasswordReset.token_hash == token_hash(token))
    ).one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= clock.now():
        return None
    status = conn.execute(select(User.status).where(User.id == row.user_id)).scalar_one()
    return int(row.id) if status == "active" else None


def complete(engine: Engine, clock: Clock, *, token: str, password: str, confirm: str) -> None:
    problem = password_problem(password)
    if problem:
        raise ResetError("weak", problem)
    if password != confirm:
        raise ResetError("mismatch", "The two passwords don't match.")
    new_hash = hash_password(password)
    with immediate(engine) as conn:
        reset_id = _usable(conn, clock, token)
        if reset_id is None:
            raise ResetError(
                "expired", "That link has expired or was already used. Ask for a new one."
            )
        user_id = conn.execute(
            select(PasswordReset.user_id).where(PasswordReset.id == reset_id)
        ).scalar_one()
        now = clock.now()
        conn.execute(update(PasswordReset).where(PasswordReset.id == reset_id).values(used_at=now))
        # Any other open links for this account stop working too.
        conn.execute(
            update(PasswordReset)
            .where(PasswordReset.user_id == user_id, PasswordReset.used_at.is_(None))
            .values(used_at=now)
        )
        conn.execute(update(User).where(User.id == user_id).values(password_hash=new_hash))
        ended = revoke_all(conn, user_id)
        audit.record(
            conn,
            clock,
            Actor(user_id),
            action="user.password_reset_by_email",
            target=("user", user_id),
            after={"sessions_ended": ended},
        )
    log.info("password_reset_completed", user_id=user_id)
