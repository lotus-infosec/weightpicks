"""The only source of time in the app (AGENTS.md hard rule 7).

Everything that needs "now" takes a `Clock`. Production uses `SystemClock`;
tests and the dev simulator use `SimClock`, which only moves when told to.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware, in UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SimClock:
    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = _require_aware(when)

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _require_aware(when: datetime) -> datetime:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("SimClock needs a timezone-aware datetime")
    return when.astimezone(UTC)
