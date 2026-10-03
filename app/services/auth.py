"""Registration, login, sessions and the bootstrap helpers (BUILD_PLAN §1.6, D-034).

Every write path is one short BEGIN IMMEDIATE transaction; password hashing (slow by
design) runs outside it where possible. Rate limits are counted from `auth_attempts`.
A refused attempt is still recorded: the transaction commits the attempt row and
returns the refusal, which is raised after the commit. Attempts older than a day and
expired sessions are pruned as new rows are written. Emails are never logged.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import Connection, Engine, delete, func, insert, select, update

from app.core.clock import Clock
from app.core.db import immediate
from app.core.security import (
    code_hash,
    hash_password,
    needs_rehash,
    new_registration_code,
    new_token,
    password_problem,
    token_hash,
    verify_password,
)
from app.models import AuthAttempt, BannedEmail, RegistrationCode, Session, User
from app.services import instance, ledger
from app.services.users import normalize_email

log = structlog.get_logger()

PLAYER_SESSION = timedelta(days=30)
ADMIN_SESSION = timedelta(hours=12)
SLIDE_EVERY = timedelta(minutes=5)
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_FAILS_PER_EMAIL = 5
LOGIN_ATTEMPTS_PER_IP = 30
REGISTER_WINDOW = timedelta(hours=1)
REGISTERS_PER_IP = 5
KEEP_ATTEMPTS = timedelta(days=1)
MAX_NAME = 40
MAX_EMAIL = 320


class AuthError(Exception):
    """A refused auth action. `code` is stable for the UI and tests."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass(frozen=True, slots=True)
class SessionInfo:
    user_id: int
    role: str
    status: str
    display_name: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class NewSession:
    token: str
    info: SessionInfo


def _record(
    conn: Connection, kind: str, ip: str, email: str | None, ok: bool, now: datetime
) -> None:
    conn.execute(insert(AuthAttempt).values(kind=kind, ip=ip, email=email, ok=ok, ts=now))
    conn.execute(delete(AuthAttempt).where(AuthAttempt.ts < now - KEEP_ATTEMPTS))


def _attempts(conn: Connection, kind: str, ip: str, since: datetime, **extra: object) -> int:
    query = (
        select(func.count())
        .select_from(AuthAttempt)
        .where(AuthAttempt.kind == kind, AuthAttempt.ip == ip, AuthAttempt.ts > since)
    )
    if "email" in extra:
        query = query.where(AuthAttempt.email == extra["email"])
    if "ok" in extra:
        query = query.where(AuthAttempt.ok.is_(bool(extra["ok"])))
    count: int = conn.execute(query).scalar_one()
    return count


def ttl(role: str) -> timedelta:
    return ADMIN_SESSION if role == "admin" else PLAYER_SESSION


# ---- registration -------------------------------------------------------------------------


def register(
    engine: Engine,
    clock: Clock,
    *,
    email: str,
    display_name: str,
    password: str,
    code: str,
    ip: str,
) -> int:
    email = normalize_email(email)
    name = " ".join(display_name.split())
    if "@" not in email or len(email) > MAX_EMAIL or not 1 <= len(name) <= MAX_NAME:
        raise AuthError(
            "invalid", "Enter an email address and a display name (up to 40 characters)."
        )
    problem = password_problem(password)
    if problem:
        raise AuthError("weak_password", problem)
    password_hash = hash_password(password)  # slow: before the write lock
    now = clock.now()
    with immediate(engine) as conn:
        if _attempts(conn, "register", ip, now - REGISTER_WINDOW) >= REGISTERS_PER_IP:
            refusal: AuthError | None = AuthError(
                "rate_limited", "Too many sign-ups from this network. Try again in an hour."
            )
        else:
            refusal = _registration_refusal(conn, email, name, code)
        _record(conn, "register", ip, email, refusal is None, now)
        if refusal is None:
            user_id = _create_player(conn, clock, email, name, password_hash, now)
    if refusal is not None:
        raise refusal
    log.info("user_registered", user_id=user_id)
    return user_id


def _registration_refusal(conn: Connection, email: str, name: str, code: str) -> AuthError | None:
    active = conn.execute(
        select(RegistrationCode.code_hash).where(RegistrationCode.active.is_(True))
    ).scalar_one_or_none()
    if active is None or active != code_hash(code):
        return AuthError("bad_code", "That registration code isn't valid.")
    if conn.execute(select(BannedEmail.email).where(BannedEmail.email == email)).first():
        return AuthError("banned", "This email can't register.")
    if conn.execute(select(User.id).where(User.email == email)).first():
        return AuthError("email_taken", "That email already has an account.")
    if conn.execute(select(User.id).where(func.lower(User.display_name) == name.lower())).first():
        return AuthError("name_taken", "That display name is taken.")
    return None


def _create_player(
    conn: Connection, clock: Clock, email: str, name: str, password_hash: str, now: datetime
) -> int:
    user_id: int = conn.execute(
        insert(User)
        .values(
            email=email,
            display_name=name,
            password_hash=password_hash,
            role="player",
            status="active",
            created_at=now,
        )
        .returning(User.id)
    ).scalar_one()
    season_id = ledger.active_season_id(conn)
    if season_id is not None:
        account_id = ledger.open_player_account(conn, clock, season_id, user_id)
        ledger.grant_starting(
            conn,
            clock,
            account_id,
            instance.economy(conn).starting_bankroll_cents,
            idempotency_key=f"register:grant:season{season_id}:user{user_id}",
        )
    return user_id


# ---- login / sessions -----------------------------------------------------------------------


def login(
    engine: Engine, clock: Clock, *, email: str, password: str, ip: str, user_agent: str | None
) -> NewSession:
    email = normalize_email(email)[:MAX_EMAIL]
    now = clock.now()
    with engine.connect() as conn:
        row = conn.execute(
            select(User.id, User.password_hash, User.role, User.status, User.display_name).where(
                User.email == email
            )
        ).one_or_none()
        locked = (
            _attempts(conn, "login", ip, now - LOGIN_WINDOW, email=email, ok=False)
            >= LOGIN_FAILS_PER_EMAIL
            or _attempts(conn, "login", ip, now - LOGIN_WINDOW) >= LOGIN_ATTEMPTS_PER_IP
        )
    # Verify even when locked or unknown so timing reveals nothing (argon2, outside the lock).
    good = verify_password(row.password_hash if row else None, password)
    rehash = hash_password(password) if good and row and needs_rehash(row.password_hash) else None

    token, csrf = new_token(), new_token()
    with immediate(engine) as conn:
        if locked:
            refusal: AuthError | None = AuthError(
                "rate_limited", "Too many attempts. Wait 15 minutes and try again."
            )
        elif row is None or not good:
            refusal = AuthError("bad_credentials", "Email or password is wrong.")
        elif row.status == "banned":
            refusal = AuthError("banned", "This account has been removed.")
        else:
            refusal = None
        _record(conn, "login", ip, email, refusal is None, now)
        if refusal is None and row is not None:
            if rehash:
                conn.execute(update(User).where(User.id == row.id).values(password_hash=rehash))
            conn.execute(delete(Session).where(Session.expires_at <= now))
            expires = now + ttl(row.role)
            conn.execute(
                insert(Session).values(
                    id_hash=token_hash(token),
                    user_id=row.id,
                    csrf_token=csrf,
                    created_at=now,
                    last_seen_at=now,
                    expires_at=expires,
                    ip=ip[:64],
                    user_agent=(user_agent or "")[:200] or None,
                )
            )
    if refusal is not None or row is None:
        raise refusal or AuthError("bad_credentials")
    log.info("user_login", user_id=row.id, role=row.role)
    return NewSession(
        token, SessionInfo(row.id, row.role, row.status, row.display_name, csrf, expires)
    )


def resolve(engine: Engine, clock: Clock, token: str | None) -> SessionInfo | None:
    """The live session for a cookie token, sliding its expiry (writes at most every 5 min)."""
    if not token or len(token) > 100:
        return None
    now = clock.now()
    key = token_hash(token)
    with engine.connect() as conn:
        row = conn.execute(
            select(
                Session.user_id,
                Session.csrf_token,
                Session.expires_at,
                Session.last_seen_at,
                User.role,
                User.status,
                User.display_name,
            )
            .join(User, User.id == Session.user_id)
            .where(Session.id_hash == key)
        ).one_or_none()
    if row is None or row.expires_at <= now or row.status == "banned":
        return None
    expires = row.expires_at
    if now - row.last_seen_at >= SLIDE_EVERY:
        expires = now + ttl(row.role)
        with immediate(engine) as conn:
            conn.execute(
                update(Session)
                .where(Session.id_hash == key)
                .values(last_seen_at=now, expires_at=expires)
            )
    return SessionInfo(row.user_id, row.role, row.status, row.display_name, row.csrf_token, expires)


def logout(engine: Engine, token: str | None) -> None:
    if token:
        with immediate(engine) as conn:
            conn.execute(delete(Session).where(Session.id_hash == token_hash(token)))


def revoke_all(conn: Connection, user_id: int) -> int:
    """Delete every session of a user (bans, password resets)."""
    return conn.execute(delete(Session).where(Session.user_id == user_id)).rowcount


# ---- bootstrap (CLI until /setup exists, D-034) ------------------------------------------------


def rotate_registration_code(engine: Engine, clock: Clock) -> str:
    """Deactivate the current code and return a new one (plaintext shown once)."""
    code = new_registration_code()
    with immediate(engine) as conn:
        conn.execute(update(RegistrationCode).values(active=False))
        conn.execute(
            insert(RegistrationCode).values(
                code_hash=code_hash(code), active=True, created_at=clock.now()
            )
        )
    return code


def create_admin(
    engine: Engine, clock: Clock, *, email: str, display_name: str, password: str
) -> int:
    email = normalize_email(email)
    name = " ".join(display_name.split())
    if "@" not in email or not 1 <= len(name) <= MAX_NAME:
        raise AuthError("invalid", "Enter an email address and a display name.")
    problem = password_problem(password)
    if problem:
        raise AuthError("weak_password", problem)
    password_hash = hash_password(password)
    with immediate(engine) as conn:
        if conn.execute(select(User.id).where(User.role == "admin")).first():
            raise AuthError("admin_exists", "An admin already exists (one per instance).")
        if conn.execute(select(User.id).where(User.email == email)).first():
            raise AuthError("email_taken", "That email already has an account.")
        user_id: int = conn.execute(
            insert(User)
            .values(
                email=email,
                display_name=name,
                password_hash=password_hash,
                role="admin",
                status="active",
                created_at=clock.now(),
            )
            .returning(User.id)
        ).scalar_one()
    log.info("admin_created", user_id=user_id)
    return user_id
