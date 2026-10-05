"""First-run setup: the one-time token, the setup session, the wizard draft and the
finish transaction (BUILD_PLAN §1.5, D-036).

- Setup is complete once `settings.setup_completed_at` is set (migration 0012 set it
  for databases bootstrapped before /setup existed).
- The token is 32 random bytes; only its SHA-256 is stored, valid 24 h. Entering it
  starts a setup session (HttpOnly cookie, hash stored in `setup_draft`, 2 h sliding).
  Wrong tokens are limited to 5 per IP per hour.
- The draft keeps every step so a refresh or a new session loses nothing. The admin
  password is kept only as its argon2 hash; secrets only as Fernet ciphertext.
- Finish writes the admin, season 1, settings, secrets and the registration code in
  one BEGIN IMMEDIATE transaction and deletes the token and the draft.
"""

import secrets as pysecrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, delete, func, insert, select, update

from app.core.clock import Clock
from app.core.crypto import decrypt, derive_fernet, encrypt
from app.core.db import immediate
from app.core.security import code_hash, new_registration_code, token_hash
from app.domain.economy import Economy
from app.domain.setup import REQUIRED, missing_steps
from app.models import (
    AuthAttempt,
    InstanceSettingsRow,
    RegistrationCode,
    Season,
    SetupDraft,
    SetupToken,
    User,
)
from app.services import instance, ledger
from app.services import secrets as secret_store
from app.services.instance import FLAG_DEFAULTS, schedule_from_json, schedule_to_json

log = structlog.get_logger()

TOKEN_TTL = timedelta(hours=24)
SESSION_TTL = timedelta(hours=2)
WRONG_TOKENS_PER_HOUR = 5


class SetupError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Draft:
    data: dict[str, Any]
    expires_at: datetime


def is_complete(conn: Connection) -> bool:
    done = conn.execute(
        select(InstanceSettingsRow.setup_completed_at).where(InstanceSettingsRow.id == 1)
    ).scalar_one_or_none()
    return done is not None


# ---- token ------------------------------------------------------------------------------


def issue_token(engine: Engine, clock: Clock) -> str:
    """Replace any token with a fresh one and return it (plaintext shown once)."""
    token = pysecrets.token_urlsafe(32)
    now = clock.now()
    with immediate(engine) as conn:
        if is_complete(conn):
            raise SetupError("complete", "Setup is already complete.")
        conn.execute(delete(SetupToken))
        conn.execute(
            insert(SetupToken).values(
                token_hash=token_hash(token), expires_at=now + TOKEN_TTL, created_at=now
            )
        )
    return token


def token_needed(engine: Engine, clock: Clock) -> bool:
    """True when setup is incomplete and no unexpired token exists (web startup)."""
    with engine.connect() as conn:
        if is_complete(conn):
            return False
        live = conn.execute(
            select(SetupToken.token_hash).where(SetupToken.expires_at > clock.now())
        ).first()
    return live is None


def start_session(engine: Engine, clock: Clock, *, token: str, ip: str) -> str:
    """Check the token and return a new setup-session cookie value."""
    now = clock.now()
    cookie = pysecrets.token_urlsafe(32)
    with immediate(engine) as conn:
        wrong = conn.execute(
            select(func.count())
            .select_from(AuthAttempt)
            .where(
                AuthAttempt.kind == "setup",
                AuthAttempt.ip == ip,
                AuthAttempt.ok.is_(False),
                AuthAttempt.ts > now - timedelta(hours=1),
            )
        ).scalar_one()
        valid = conn.execute(
            select(SetupToken.token_hash).where(
                SetupToken.token_hash == token_hash(token.strip()), SetupToken.expires_at > now
            )
        ).first()
        refusal: SetupError | None = None
        if is_complete(conn):
            refusal = SetupError("complete", "Setup is already complete.")
        elif wrong >= WRONG_TOKENS_PER_HOUR:
            refusal = SetupError("rate_limited", "Too many wrong tokens. Try again in an hour.")
        elif valid is None:
            refusal = SetupError("bad_token", "That setup token isn't valid (or has expired).")
        conn.execute(
            insert(AuthAttempt).values(kind="setup", ip=ip, email=None, ok=refusal is None, ts=now)
        )
        if refusal is None:
            existing = conn.execute(select(SetupDraft.id)).first()
            values = {
                "session_hash": token_hash(cookie),
                "session_expires_at": now + SESSION_TTL,
                "updated_at": now,
            }
            if existing:
                conn.execute(update(SetupDraft).where(SetupDraft.id == 1).values(**values))
            else:
                conn.execute(insert(SetupDraft).values(id=1, data={}, **values))
    if refusal is not None:
        raise refusal
    log.info("setup_session_started")
    return cookie


def resolve(engine: Engine, clock: Clock, cookie: str | None) -> Draft | None:
    if not cookie or len(cookie) > 100:
        return None
    now = clock.now()
    with engine.connect() as conn:
        row = conn.execute(
            select(SetupDraft.session_hash, SetupDraft.session_expires_at, SetupDraft.data)
        ).one_or_none()
        if row is None or is_complete(conn):
            return None
    if row.session_hash != token_hash(cookie) or row.session_expires_at <= now:
        return None
    expires = now + SESSION_TTL
    with immediate(engine) as conn:
        conn.execute(
            update(SetupDraft).where(SetupDraft.id == 1).values(session_expires_at=expires)
        )
    return Draft(dict(row.data), expires)


def save(
    engine: Engine, clock: Clock, cookie: str, key: str, values: dict[str, Any]
) -> dict[str, Any]:
    """Merge one step's clean values into the draft and return the whole draft."""
    if resolve(engine, clock, cookie) is None:
        raise SetupError("no_session", "Your setup session expired. Enter the token again.")
    with immediate(engine) as conn:
        data = dict(conn.execute(select(SetupDraft.data)).scalar_one())
        data[key] = values
        conn.execute(
            update(SetupDraft).where(SetupDraft.id == 1).values(data=data, updated_at=clock.now())
        )
    return data


def encrypt_for_draft(app_secret_key: str, value: str) -> str:
    return encrypt(derive_fernet(app_secret_key), value)


def new_code() -> str:
    return new_registration_code()


# ---- finish -------------------------------------------------------------------------------


def finish(engine: Engine, clock: Clock, cookie: str, *, app_secret_key: str) -> int:
    """Apply the draft in one transaction. Returns the admin's user id."""
    draft = resolve(engine, clock, cookie)
    if draft is None:
        raise SetupError("no_session", "Your setup session expired. Enter the token again.")
    data = draft.data
    gaps = missing_steps(data)
    if gaps:
        raise SetupError(
            "incomplete", "Finish these steps first: " + ", ".join(s.title for s in gaps)
        )
    encrypted: dict[str, str] = data.get("secrets", {})
    fernet = derive_fernet(app_secret_key) if encrypted else None
    plain = {name: decrypt(fernet, value) for name, value in encrypted.items()} if fernet else {}
    now = clock.now()
    admin, subject, sched = data["admin"], data["subject"], data["schedule"]
    with immediate(engine) as conn:
        if is_complete(conn):
            raise SetupError("complete", "Setup is already complete.")
        if (
            conn.execute(select(Season.id)).first()
            or conn.execute(select(User.id).where(User.email == admin["email"])).first()
        ):
            raise SetupError(
                "not_empty", "This database already has data; set up a fresh instance."
            )
        user_id: int = conn.execute(
            insert(User)
            .values(
                email=admin["email"],
                display_name=admin["display_name"],
                password_hash=admin["password_hash"],
                role="admin",
                status="active",
                created_at=now,
            )
            .returning(User.id)
        ).scalar_one()
        season_id = ledger.open_season(conn, clock)
        conn.execute(
            update(Season)
            .where(Season.id == season_id)
            .values(
                start_weight_x10=subject["start_weight_x10"],
                goal_weight_x10=subject["goal_weight_x10"],
                direction=subject["direction"],
            )
        )
        _write_settings(conn, now, data, sched, subject)
        for name, value in plain.items():
            secret_store.put(conn, clock, app_secret_key, name, value)
        conn.execute(update(RegistrationCode).values(active=False))
        conn.execute(
            insert(RegistrationCode).values(
                code_hash=code_hash(data["registration"]["code"]), active=True, created_at=now
            )
        )
        conn.execute(delete(SetupToken))
        conn.execute(delete(SetupDraft))
    log.info("setup_completed", admin_id=user_id, season_id=season_id, secrets=sorted(plain))
    return user_id


def _write_settings(
    conn: Connection,
    now: datetime,
    data: dict[str, Any],
    sched: dict[str, Any],
    subject: dict[str, Any],
) -> None:
    values = {
        "timezone": sched["timezone"],
        "unit": subject["unit"],
        "schedule": schedule_to_json(schedule_from_json(sched)),
        "enabled_metrics": data["stats"]["enabled_metrics"],
        "instance_state": instance.ACTIVE,
        "updated_at": now,
        "app_name": data["appearance"]["app_name"],
        "palette": data["appearance"]["palette"],
        "subject_name": subject["subject_name"],
        "economy": Economy.from_json(data["economy"]).to_json(),
        "ai_mode": "review",
        "flags": dict(FLAG_DEFAULTS),
        "smtp": {k: v for k, v in data.get("smtp", {}).items() if k != "password"},
        "setup_completed_at": now,
    }
    if conn.execute(select(InstanceSettingsRow.id)).first():
        conn.execute(
            update(InstanceSettingsRow).where(InstanceSettingsRow.id == 1).values(**values)
        )
    else:
        conn.execute(insert(InstanceSettingsRow).values(id=1, **values))


__all__ = ["REQUIRED", "Draft", "SetupError"]


def admin_values(
    email: str, display_name: str, password: str, confirm: str, keep_hash: str | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate the admin step; the password is returned only as its argon2 hash. An empty
    password keeps `keep_hash` (already saved in the draft)."""
    from app.core.security import hash_password, password_problem
    from app.services.users import normalize_email

    errors: dict[str, str] = {}
    email = normalize_email(email)
    name = " ".join(display_name.split())
    if "@" not in email or len(email) > 320:
        errors["email"] = "Enter an email address."
    if not 1 <= len(name) <= 40:
        errors["display_name"] = "Enter a display name (up to 40 characters)."
    password_hash = keep_hash or ""
    if not (keep_hash and not password and not confirm):
        problem = password_problem(password)
        if problem:
            errors["password"] = problem
        elif password != confirm:
            errors["confirm"] = "The passwords don't match."
        elif not errors:
            password_hash = hash_password(password)
    if errors:
        return {}, errors
    return {"email": email, "display_name": name, "password_hash": password_hash}, {}
