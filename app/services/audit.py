"""The admin audit log (BUILD_PLAN §1.5: every admin mutation is recorded). Rows are
append-only (triggers). Values under sensitive-looking keys are never stored."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, insert

from app.core.clock import Clock
from app.core.db import immediate
from app.models import AuditEntry

SENSITIVE = ("password", "token", "secret", "webhook", "code_hash")
REDACTED = "[redacted]"


def redact(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if data is None:
        return None
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if any(word in key.lower() for word in SENSITIVE):
            clean[key] = REDACTED
        elif isinstance(value, dict):
            clean[key] = redact(value)
        else:
            clean[key] = value
    return clean


@dataclass(frozen=True, slots=True)
class Actor:
    """Who did it: the user, their IP, and the real time of the click (audit rows use it;
    in dev the app's SimClock differs)."""

    user_id: int
    ip: str | None = None
    acted_at: datetime | None = None


def record(
    conn: Connection,
    clock: Clock,
    actor: Actor | None,
    *,
    action: str,
    target: tuple[str, int] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    """`actor` is None for the system (jobs, AI auto mode, maintenance)."""
    conn.execute(
        insert(AuditEntry).values(
            actor_id=actor.user_id if actor else None,
            action=action,
            target_type=target[0] if target else None,
            target_id=target[1] if target else None,
            before=redact(before),
            after=redact(after),
            reason=reason,
            ip=actor.ip if actor else None,
            ts=(actor.acted_at if actor else None) or clock.now(),
        )
    )


def record_alone(
    engine: Engine,
    clock: Clock,
    actor: Actor | None,
    *,
    action: str,
    target: tuple[str, int] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """An audit row in its own transaction, for actions with nothing else to write
    (downloads, staged maintenance, file changes)."""
    with immediate(engine) as conn:
        record(conn, clock, actor, action=action, target=target, after=after)
