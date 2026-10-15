"""What every data provider returns.

Providers are read-only sources. Records carry a stable `ref` so ingest can be
replayed safely: (metric, ref) is unique in `observations`.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Protocol

WeighInSource = Literal["scale", "manual"]
CountMetric = Literal["steps", "active_minutes", "intensity_minutes", "kcal"]
COUNT_METRICS: tuple[CountMetric, ...] = ("steps", "active_minutes", "intensity_minutes", "kcal")
WORKOUT_MIN_MINUTES = 10  # a workout is a Garmin activity of 10+ minutes


@dataclass(frozen=True, slots=True)
class WeighIn:
    ref: str
    at: datetime  # aware UTC
    grams: int
    source: WeighInSource


@dataclass(frozen=True, slots=True)
class DailyTotal:
    ref: str
    local_date: date
    metric: CountMetric
    value: int


@dataclass(frozen=True, slots=True)
class Activity:
    ref: str
    at: datetime  # start, aware UTC
    duration_min: int
    kind: str


@dataclass(frozen=True, slots=True)
class Batch:
    weigh_ins: list[WeighIn] = field(default_factory=list)
    daily_totals: list[DailyTotal] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)
    # The watch's latest upload to Garmin Connect (issue #57); None when the provider
    # has no such notion (the simulator).
    watch_upload_at: datetime | None = None
    watch_device: str | None = None
    # The oldest day held back for a missing watch sync, and when its markets push.
    watch_waiting: tuple[date, datetime] | None = None

    def __len__(self) -> int:
        return len(self.weigh_ins) + len(self.daily_totals) + len(self.activities)


class DataProvider(Protocol):
    name: str

    def fetch_since(self, since: datetime, now: datetime, *, days: int | None = None) -> Batch:
        """Records that became available in (since, now]. `days` asks the provider to
        download that many recent days again (the admin's deep Sync now, issue #61)."""
        ...

    def complete_through(self, now: datetime) -> dict[str, date]:
        """Per metric, the last local date whose data is final as of `now`."""
        ...
