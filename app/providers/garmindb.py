"""Real Garmin data via GarminDB. Read-only.

GarminDB downloads into `<home>/HealthData`; this provider reads the raw JSON it saved
rather than its SQLite tables, because GarminDB's `weight` table keeps one row per day
at midnight (no time of day, no manual/scale flag) and its range skips today
(`weight_recent.json`, written by garmin_recent.py, replaces it for weigh-ins).

Weigh-in times come from `timestampGMT`. Garmin's `date` field is the local wall clock
encoded as if it were UTC; reading it as UTC would shift every weigh-in by the offset.

Daily totals are stored only for days the watch synced after that day's cutoff, and only
from a summary file downloaded after the watch's latest upload (issue #57): a partial
total is never stored, because a stored day is final for settlement.
"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from app.domain import watch
from app.domain.schedule import local_date, window_close
from app.providers.base import (
    COUNT_METRICS,
    WORKOUT_MIN_MINUTES,
    Activity,
    Batch,
    CountMetric,
    DailyTotal,
    WeighIn,
    WeighInSource,
)
from app.providers.garmindb_runner import GarminRunError

log = structlog.get_logger()
LOOKBACK = timedelta(days=3)  # late manual entries and re-downloaded days
RECENT_DAYS = 7  # summaries garmin_recent.py refreshes after a watch upload
WATCH_FILE = "watch_sync.json"
MANUAL_SOURCES = {"MANUAL"}


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        log.warning("garmin_file_unreadable", file=path.name)
        return None


def _gmt(value: Any) -> datetime | None:
    if isinstance(value, int | float) and value > 0:
        return datetime.fromtimestamp(value / 1000, UTC)
    return None


def parse_weigh_ins(data: Any) -> Iterator[WeighIn]:
    entries = data.get("dateWeightList") if isinstance(data, dict) else None
    for entry in entries or []:
        at = _gmt(entry.get("timestampGMT"))
        grams = entry.get("weight")
        pk = entry.get("samplePk")
        if at is None or not isinstance(grams, int | float) or grams <= 0 or pk is None:
            continue
        source: WeighInSource = "manual" if entry.get("sourceType") in MANUAL_SOURCES else "scale"
        yield WeighIn(ref=f"garmin:w:{pk}", at=at, grams=round(grams), source=source)


def parse_watch(data: Any) -> tuple[datetime | None, str | None]:
    """The watch's latest upload and device name, from garmin_recent.py's file."""
    if not isinstance(data, dict):
        raise GarminRunError("the watch's last sync time is missing; it is fetched every sync")
    device = data.get("device")
    return _gmt(data.get("upload_ms")), device if isinstance(device, str) else None


def _minutes(seconds: Any) -> int:
    return int(seconds) // 60 if isinstance(seconds, int | float) else 0


def parse_daily_summary(data: Any) -> list[DailyTotal]:
    if not isinstance(data, dict) or not data.get("calendarDate"):
        return []
    day = date.fromisoformat(str(data["calendarDate"]))
    steps = data.get("totalSteps")
    if not isinstance(steps, int | float):
        return []  # no wearable data for that day: leave the day missing, not zero
    values: dict[CountMetric, int] = {
        "steps": int(steps),
        "active_minutes": _minutes(data.get("activeSeconds"))
        + _minutes(data.get("highlyActiveSeconds")),
        "intensity_minutes": int(data.get("moderateIntensityMinutes") or 0)
        + 2 * int(data.get("vigorousIntensityMinutes") or 0),
        "kcal": int(data.get("activeKilocalories") or 0),
    }
    return [
        DailyTotal(ref=f"garmin:{m}:{day.isoformat()}", local_date=day, metric=m, value=values[m])
        for m in COUNT_METRICS
    ]


def parse_activity(data: Any) -> Activity | None:
    if not isinstance(data, dict):
        return None
    activity_id = data.get("activityId")
    start = data.get("startTimeGMT")
    duration = data.get("duration") or data.get("elapsedDuration")
    if activity_id is None or not isinstance(start, str) or not isinstance(duration, int | float):
        return None
    at = datetime.fromisoformat(start).replace(tzinfo=UTC)
    minutes = int(duration) // 60
    if minutes < WORKOUT_MIN_MINUTES:
        return None
    kind = (data.get("activityType") or {}).get("typeKey") or "activity"
    return Activity(ref=f"garmin:a:{activity_id}", at=at, duration_min=minutes, kind=str(kind))


class GarminDBProvider:
    name = "garmindb"

    def __init__(
        self,
        home: Path,
        tz: ZoneInfo,
        runner: Callable[[date], None],
        *,
        cutoff: time = watch.DEFAULT_CUTOFF,
        grace: timedelta = timedelta(hours=watch.DEFAULT_GRACE_HOURS),
    ) -> None:
        self.home = home
        self.tz = tz
        self.runner = runner
        self.cutoff = cutoff
        self.grace = grace
        self.fetched_at: datetime | None = None
        self.watch_upload_at: datetime | None = None
        self.watch_device: str | None = None

    @property
    def data_dir(self) -> Path:
        return self.home / "HealthData"

    def weigh_ins(self) -> dict[str, WeighIn]:
        found: dict[str, WeighIn] = {}
        for path in sorted((self.data_dir / "Weight").glob("weight_*.json")):
            for w in parse_weigh_ins(_load(path)):
                found[w.ref] = w
        return found

    def daily_totals(
        self, first: date, last: date, fresh_after: datetime | None = None
    ) -> list[DailyTotal]:
        """Totals for [first, last], from files written at or after `fresh_after`."""
        totals: list[DailyTotal] = []
        day = first
        while day <= last:
            path = (
                self.data_dir
                / "FitFiles"
                / "Monitoring"
                / str(day.year)
                / f"daily_summary_{day.isoformat()}.json"
            )
            if path.exists() and (
                fresh_after is None or path.stat().st_mtime >= fresh_after.timestamp()
            ):
                totals += parse_daily_summary(_load(path))
            day += timedelta(days=1)
        return totals

    def activities(self) -> list[Activity]:
        found: list[Activity] = []
        for path in sorted((self.data_dir / "FitFiles" / "Activities").glob("activity_*.json")):
            if "details" in path.name:
                continue
            activity = parse_activity(_load(path))
            if activity:
                found.append(activity)
        return found

    def fetch_since(self, since: datetime, now: datetime) -> Batch:
        # Raises GarminRunError; the sync is then recorded as failed.
        self.runner(local_date(now, self.tz))
        upload, device = parse_watch(_load(self.data_dir / WATCH_FILE))
        self.watch_upload_at, self.watch_device = upload, device
        self.fetched_at = now
        floor = since - LOOKBACK
        counts_final = self.complete_through(now)["steps"]
        first = max(
            min(local_date(floor, self.tz), counts_final - timedelta(days=RECENT_DAYS)),
            counts_final - timedelta(days=120),
        )
        return Batch(
            weigh_ins=[w for w in self.weigh_ins().values() if floor < w.at <= now],
            daily_totals=[
                t
                for t in self.daily_totals(first, counts_final, fresh_after=upload)
                if watch.synced(t.local_date, upload, self.cutoff, self.tz)
            ],
            activities=[
                a
                for a in self.activities()
                if floor < a.at and local_date(a.at, self.tz) <= counts_final
            ],
            watch_upload_at=upload,
            watch_device=device,
            watch_waiting=watch.waiting(now, upload, self.cutoff, self.grace, self.tz),
        )

    def complete_through(self, now: datetime) -> dict[str, date]:
        """A day is final only once a successful download ran after it was over: after
        the weigh-in window for weight; for counts two hours after midnight, and only
        once the watch synced after the day's cutoff or the grace period ran out."""
        seen = self.fetched_at or datetime.min.replace(tzinfo=UTC)
        today = local_date(min(now, seen) if self.fetched_at else now, self.tz)
        weight = today if seen >= window_close(today, self.tz) else today - timedelta(days=1)
        counts = (
            watch.final_through(
                min(now, seen), self.watch_upload_at, self.cutoff, self.grace, self.tz
            )
            if self.fetched_at
            else today - timedelta(days=2)
        )
        result = {"weight": weight, "workout": counts}
        result.update(dict.fromkeys(COUNT_METRICS, counts))
        return result
