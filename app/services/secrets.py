"""Integration secrets stored encrypted in the `secrets` table (BUILD_PLAN §1.6).

Names: `workers_ai.account_id`, `workers_ai.token`, `webhook.<category>`,
`smtp.password`. Values are never logged. Without APP_SECRET_KEY, or with the wrong
one, integrations behave as unconfigured and the app keeps working (hard rule 9).
"""

import structlog
from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.sqlite import insert

from app.core.clock import Clock
from app.core.crypto import (
    KEY_CHECK_NAME,
    KEY_CHECK_PLAINTEXT,
    SecretKeyMissing,
    WrongSecretKey,
    decrypt,
    derive_fernet,
    encrypt,
)
from app.models import Secret

log = structlog.get_logger()
WEBHOOK_CATEGORIES = (
    "bets_placed",
    "high_roller",
    "bet_results",
    "parlay_results",
    "market_settlements",
    "new_markets",
    "special_events",
    "hype",
    "weekly_standings",
    "busts",
    "goal_reached",
    "admin_alerts",
)


def _upsert(conn: Connection, clock: Clock, name: str, ciphertext: str) -> None:
    stmt = insert(Secret).values(name=name, ciphertext=ciphertext, updated_at=clock.now())
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["name"],
            set_={"ciphertext": stmt.excluded.ciphertext, "updated_at": stmt.excluded.updated_at},
        )
    )


def check_key(conn: Connection, app_secret_key: str) -> None:
    """Raise SecretKeyMissing / WrongSecretKey if secrets can't be used with this key."""
    fernet = derive_fernet(app_secret_key)
    stored = conn.execute(
        select(Secret.ciphertext).where(Secret.name == KEY_CHECK_NAME)
    ).scalar_one_or_none()
    if stored is not None and decrypt(fernet, stored) != KEY_CHECK_PLAINTEXT:
        raise WrongSecretKey("key check value mismatch")


def put(conn: Connection, clock: Clock, app_secret_key: str, name: str, value: str) -> None:
    if name == KEY_CHECK_NAME:
        raise ValueError("reserved secret name")
    check_key(conn, app_secret_key)
    fernet = derive_fernet(app_secret_key)
    _upsert(conn, clock, KEY_CHECK_NAME, encrypt(fernet, KEY_CHECK_PLAINTEXT))
    _upsert(conn, clock, name, encrypt(fernet, value))


def get(conn: Connection, app_secret_key: str, name: str) -> str | None:
    """The secret, or None if unset or unusable (missing or wrong key: logged once)."""
    stored = conn.execute(select(Secret.ciphertext).where(Secret.name == name)).scalar_one_or_none()
    if stored is None:
        return None
    try:
        check_key(conn, app_secret_key)
        return decrypt(derive_fernet(app_secret_key), stored)
    except (SecretKeyMissing, WrongSecretKey) as exc:
        log.error("secret_unavailable", name=name, reason=type(exc).__name__)
        return None


def remove(conn: Connection, name: str) -> None:
    conn.execute(delete(Secret).where(Secret.name == name))


def names(conn: Connection) -> list[str]:
    return [
        n
        for n in conn.execute(select(Secret.name).order_by(Secret.name)).scalars()
        if n != KEY_CHECK_NAME
    ]
