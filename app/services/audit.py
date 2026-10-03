"""The admin audit log (BUILD_PLAN §1.5: every admin mutation is recorded). Rows are
append-only (triggers). Values under sensitive-looking keys are never stored."""

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, insert

from app.core.clock import Clock
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


def record(
    conn: Connection,
    clock: Clock,
    *,
    actor_id: int | None,
    action: str,
    target: tuple[str, int] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
    ip: str | None = None,
    ts: datetime | None = None,
) -> None:
    """`ts` defaults to the clock; callers pass the real time an admin acted."""
    conn.execute(
        insert(AuditEntry).values(
            actor_id=actor_id,
            action=action,
            target_type=target[0] if target else None,
            target_id=target[1] if target else None,
            before=redact(before),
            after=redact(after),
            reason=reason,
            ip=ip,
            ts=ts or clock.now(),
        )
    )
