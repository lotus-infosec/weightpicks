"""Local-time rules. Pure functions of an aware timestamp and a time zone;
all wall-clock math goes through zoneinfo so DST days behave."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEIGH_IN_WINDOW_START = time(4, 0)
WEIGH_IN_WINDOW_END = time(11, 0)  # exclusive
SYNC_MINUTES_IN_WINDOW = 15
SYNC_HOURS_OUTSIDE_WINDOW = 2


def _local(ts: datetime, tz: ZoneInfo) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("expected an aware datetime")
    return ts.astimezone(tz)


def local_date(ts: datetime, tz: ZoneInfo) -> date:
    return _local(ts, tz).date()


def in_weigh_in_window(ts: datetime, tz: ZoneInfo) -> bool:
    return WEIGH_IN_WINDOW_START <= _local(ts, tz).time() < WEIGH_IN_WINDOW_END


# Instants are returned in UTC: arithmetic between two datetimes that share one ZoneInfo
# is wall-clock arithmetic in Python and silently ignores DST (a 25 h day measures 24 h).


def window_close(day: date, tz: ZoneInfo) -> datetime:
    """The UTC instant the weigh-in window of `day` closes."""
    return datetime.combine(day, WEIGH_IN_WINDOW_END, tzinfo=tz).astimezone(UTC)


def next_local_midnight(day: date, tz: ZoneInfo) -> datetime:
    """The UTC instant local midnight starts the day after `day`."""
    return datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)


def sync_period_key(now: datetime, tz: ZoneInfo) -> str:
    """Every 15 minutes inside the weigh-in window, every 2 hours outside it."""
    loc = _local(now, tz)
    if in_weigh_in_window(now, tz):
        minute = loc.minute - loc.minute % SYNC_MINUTES_IN_WINDOW
        return f"{loc:%Y-%m-%dT%H}:{minute:02d}"
    hour = loc.hour - loc.hour % SYNC_HOURS_OUTSIDE_WINDOW
    return f"{loc:%Y-%m-%d}T{hour:02d}h"


# ---- market drop and lock schedule ---------------------------------------
# Wall-clock times are resolved per day from the local date, so a DST change moves the
# UTC instant but never the local time. A nonexistent spring-forward time (02:30 on the
# March change) resolves with the pre-change offset, i.e. 03:30 local; times in the
# repeated autumn hour resolve to the first occurrence (fold=0).


def at_local(day: date, wall: time, tz: ZoneInfo) -> datetime:
    """The UTC instant of wall-clock `wall` on local date `day`."""
    return datetime.combine(day, wall, tzinfo=tz).astimezone(UTC)


def last_day_of_month(day: date) -> date:
    first_of_next = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
    return first_of_next - timedelta(days=1)


def latest_daily(now: datetime, wall: time, tz: ZoneInfo) -> date:
    """The local date of the most recent `wall` time at or before `now`."""
    today = local_date(now, tz)
    return today if now >= at_local(today, wall, tz) else today - timedelta(days=1)


def latest_weekly(now: datetime, weekday: int, wall: time, tz: ZoneInfo) -> date:
    """The local date of the most recent `weekday` (Mon=0) `wall` time at or before `now`."""
    today = local_date(now, tz)
    day = today - timedelta(days=(today.weekday() - weekday) % 7)
    return day if now >= at_local(day, wall, tz) else day - timedelta(days=7)


def latest_month_end(now: datetime, wall: time, tz: ZoneInfo) -> date:
    """The most recent last-day-of-month `wall` time at or before `now`, as a local date."""
    today = local_date(now, tz)
    day = last_day_of_month(today)
    if day == today and now >= at_local(day, wall, tz):
        return day
    return today.replace(day=1) - timedelta(days=1)
