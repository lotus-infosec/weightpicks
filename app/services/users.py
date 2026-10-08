"""Minimal user records for dev seeding and tests (accounts and auth: services.auth)."""

from sqlalchemy import Connection, insert, select

from app.core.clock import Clock
from app.models import User


def normalize_email(email: str) -> str:
    return email.strip().lower()


def ensure_player(conn: Connection, clock: Clock, email: str, display_name: str) -> int:
    """Return the id of the user with this email, creating a player with no password."""
    normalized = normalize_email(email)
    existing = conn.execute(select(User.id).where(User.email == normalized)).scalar_one_or_none()
    if existing is not None:
        return existing
    return conn.execute(
        insert(User)
        .values(
            email=normalized,
            display_name=display_name,
            password_hash=None,
            role="player",
            status="active",
            created_at=clock.now(),
        )
        .returning(User.id)
    ).scalar_one()
