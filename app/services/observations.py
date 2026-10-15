"""Read helpers over the immutable observations table."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, func, select

from app.core.config import Settings
from app.domain.schedule import at_local, in_weigh_in_window, local_date, next_local_midnight
from app.models import InstanceSettingsRow, Observation, SyncRun
from app.providers.base import COUNT_METRICS


@dataclass(frozen=True, slots=True)
class CanonicalWeighIn:
    local_date: date
    observation_id: int
    observed_at: datetime
    value: int  # tenths of the display unit
    source: str


def current_zone(conn: Connection) -> ZoneInfo:
    """The instance's time zone now (before /setup: the configured default)."""
    zone = conn.execute(
        select(InstanceSettingsRow.timezone).where(InstanceSettingsRow.id == 1)
    ).scalar_one_or_none()
    return ZoneInfo(zone or Settings.model_fields["wp_timezone"].default)


def _between(conn: Connection, metric: str, start: date, end: date, tz: ZoneInfo) -> list[Any]:
    """`metric` rows observed during local days [start, end] in `tz`, oldest first."""
    return list(
        conn.execute(
            select(
                Observation.id,
                Observation.observed_at,
                Observation.value,
                Observation.source,
            )
            .where(
                Observation.metric == metric,
                Observation.observed_at >= at_local(start, time(0), tz),
                Observation.observed_at < next_local_midnight(end, tz),
            )
            .order_by(Observation.observed_at, Observation.id)
        ).all()
    )


def canonical_weigh_ins(
    conn: Connection, start: date, end: date, tz: ZoneInfo | None = None
) -> list[CanonicalWeighIn]:
    """The canonical weigh-in per local day in [start, end]: the earliest in-window one.

    Derived on read from immutable rows, so a late-synced earlier weigh-in
    correctly takes over until the day's data is complete. Days and the window are
    worked out from each reading's UTC time in `tz` (default: the instance's zone now),
    never from the day stored at sync time, so a time zone change applies.
    """
    zone = tz or current_zone(conn)
    canonical: dict[date, CanonicalWeighIn] = {}
    for row in _between(conn, "weight", start, end, zone):
        day = local_date(row.observed_at, zone)
        if day not in canonical and in_weigh_in_window(row.observed_at, zone):
            canonical[day] = CanonicalWeighIn(day, row.id, row.observed_at, row.value, row.source)
    return [canonical[d] for d in sorted(canonical)]


def activity_counts(
    conn: Connection,
    start: date,
    end: date,
    tz: ZoneInfo | None = None,
    *,
    min_minutes: int = 0,
) -> dict[date, int]:
    """Activities of at least `min_minutes` per local day in [start, end] (days with
    none are left out), by start time in `tz` (default: the instance's zone now)."""
    zone = tz or current_zone(conn)
    counts: dict[date, int] = {}
    for row in _between(conn, "activity", start, end, zone):
        if row.value >= min_minutes:
            day = local_date(row.observed_at, zone)
            counts[day] = counts.get(day, 0) + 1
    return counts


@dataclass(frozen=True, slots=True)
class DailyTotalRow:
    observation_id: int
    metric: str
    local_date: date
    value: int


def latest_totals(
    conn: Connection, start: date, end: date, metrics: Sequence[str] = COUNT_METRICS
) -> list[DailyTotalRow]:
    """Each day's newest stored total per metric in [start, end]. A changed total is
    stored as a correction (a new row), so the newest row wins (issue #57)."""
    rank = (
        func.row_number()
        .over(
            partition_by=(Observation.metric, Observation.local_date),
            order_by=(Observation.observed_at.desc(), Observation.id.desc()),
        )
        .label("rank")
    )
    ranked = (
        select(Observation.id, Observation.metric, Observation.local_date, Observation.value, rank)
        .where(Observation.metric.in_(metrics), Observation.local_date.between(start, end))
        .subquery()
    )
    return [
        DailyTotalRow(r.id, r.metric, r.local_date, r.value)
        for r in conn.execute(
            select(ranked.c.id, ranked.c.metric, ranked.c.local_date, ranked.c.value)
            .where(ranked.c.rank == 1)
            .order_by(ranked.c.local_date, ranked.c.metric)
        )
    ]


def latest_complete_through(conn: Connection) -> dict[str, date]:
    """`complete_through` from the most recent successful sync (empty before any)."""
    data = conn.execute(
        select(SyncRun.complete_through)
        .where(SyncRun.status == "ok")
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return {metric: date.fromisoformat(day) for metric, day in (data or {}).items()}
