"""Read helpers over the immutable observations table."""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, select

from app.core.config import Settings
from app.domain.schedule import at_local, in_weigh_in_window, local_date, next_local_midnight
from app.models import InstanceSettingsRow, Observation, SyncRun


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
    """The canonical weigh-in per local day in [start, end]: the earliest in-window one (D-009).

    Derived on read from immutable rows (D-027), so a late-synced earlier weigh-in
    correctly takes over until the day's data is complete. Days and the window are
    worked out from each reading's UTC time in `tz` (default: the instance's zone now),
    never from the day stored at sync time, so a time zone change applies (D-050).
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


def latest_complete_through(conn: Connection) -> dict[str, date]:
    """`complete_through` from the most recent successful sync (empty before any)."""
    data = conn.execute(
        select(SyncRun.complete_through)
        .where(SyncRun.status == "ok")
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return {metric: date.fromisoformat(day) for metric, day in (data or {}).items()}
