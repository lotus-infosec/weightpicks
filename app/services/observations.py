"""Read helpers over the immutable observations table."""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Connection, func, select

from app.models import Observation, SyncRun


@dataclass(frozen=True, slots=True)
class CanonicalWeighIn:
    local_date: date
    observation_id: int
    observed_at: datetime
    value: int  # tenths of the display unit
    source: str


def canonical_weigh_ins(conn: Connection, start: date, end: date) -> list[CanonicalWeighIn]:
    """The canonical weigh-in per local day in [start, end]: the earliest in-window one (D-009).

    Derived on read from immutable rows (D-027), so a late-synced earlier weigh-in
    correctly takes over until the day's data is complete.
    """
    rank = (
        func.row_number()
        .over(
            partition_by=Observation.local_date,
            order_by=(Observation.observed_at, Observation.id),
        )
        .label("rank")
    )
    ranked = (
        select(
            Observation.local_date,
            Observation.id,
            Observation.observed_at,
            Observation.value,
            Observation.source,
            rank,
        )
        .where(
            Observation.metric == "weight",
            Observation.in_window.is_(True),
            Observation.local_date.between(start, end),
        )
        .subquery()
    )
    rows = conn.execute(
        select(
            ranked.c.local_date,
            ranked.c.id,
            ranked.c.observed_at,
            ranked.c.value,
            ranked.c.source,
        )
        .where(ranked.c.rank == 1)
        .order_by(ranked.c.local_date)
    ).all()
    return [CanonicalWeighIn(*row) for row in rows]


def latest_complete_through(conn: Connection) -> dict[str, date]:
    """`complete_through` from the most recent successful sync (empty before any)."""
    data = conn.execute(
        select(SyncRun.complete_through)
        .where(SyncRun.status == "ok")
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return {metric: date.fromisoformat(day) for metric, day in (data or {}).items()}
