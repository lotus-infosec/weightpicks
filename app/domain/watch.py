"""When a day's watch data counts as final (issue #57). Pure: no I/O, no clock.

An iPhone doesn't sync Garmin Connect in the background, so Garmin's servers can hold
a partial day (12 steps instead of 12,000) until the app is opened. A day's daily
totals therefore count only once the watch uploaded at or after that day's cutoff (the
bedtime sync). Without one the day waits, re-checked at every sync, until a grace
period after the usual settle time; then it is final with no data and its markets push.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.schedule import at_local, local_date, next_local_midnight

# Daily totals are looked at two hours after the day ends (the 02:00 sync).
COUNTS_SETTLE = timedelta(hours=2)
DEFAULT_CUTOFF = time(22, 0)
DEFAULT_GRACE_HOURS = 12
EARLIEST_CUTOFF = time(12, 0)
# GarminDB re-downloads only the last 3 days; the recent fetch refreshes a week.
MAX_GRACE_HOURS = 48


def check_settings(cutoff: time, grace_hours: int) -> None:
    if cutoff < EARLIEST_CUTOFF:
        raise ValueError("the watch sync cutoff must be at or after 12:00")
    if not 0 <= grace_hours <= MAX_GRACE_HOURS:
        raise ValueError(f"the grace period must be 0 to {MAX_GRACE_HOURS} hours")


def parse_settings(cutoff: str, grace_hours: str) -> tuple[time, int]:
    """The admin form's text, checked: ("22:00", "12") -> (time(22, 0), 12)."""
    try:
        wall = time.fromisoformat(cutoff.strip())
    except ValueError:
        raise ValueError("Enter the cutoff as a time, like 22:00.") from None
    text = grace_hours.strip()
    if not text.isdigit():
        raise ValueError("Enter the grace period as whole hours, like 12.")
    hours = int(text)
    try:
        check_settings(wall.replace(second=0, microsecond=0), hours)
    except ValueError as exc:
        raise ValueError(f"{str(exc)[0].upper()}{str(exc)[1:]}.") from None
    return wall.replace(second=0, microsecond=0), hours


def deadline_label(grace_hours: int) -> str:
    """When an unsynced day's markets push, in words: "14:00 the next day"."""
    hours = int(COUNTS_SETTLE.total_seconds()) // 3600 + grace_hours
    when = "the next day" if hours < 24 else "two days later"
    return f"{hours % 24:02d}:00 {when}"


def synced(day: date, upload: datetime | None, cutoff: time, tz: ZoneInfo) -> bool:
    """Whether the watch uploaded at or after `day`'s cutoff."""
    return upload is not None and upload >= at_local(day, cutoff, tz)


def settles_at(day: date, tz: ZoneInfo) -> datetime:
    """The first moment `day`'s totals are looked at."""
    return next_local_midnight(day, tz) + COUNTS_SETTLE


def deadline(day: date, tz: ZoneInfo, grace: timedelta) -> datetime:
    """After this, a day still without a watch sync is final with no data."""
    return settles_at(day, tz) + grace


def final_through(
    seen: datetime, upload: datetime | None, cutoff: time, grace: timedelta, tz: ZoneInfo
) -> date:
    """The last day whose daily totals are final, given a download at `seen` and the
    watch's latest upload. Days only get more final going back (earlier cutoffs and
    deadlines), so the answer is one date."""
    if grace < timedelta(0) or grace > timedelta(hours=MAX_GRACE_HOURS):
        raise ValueError("grace out of range")
    day = local_date(seen, tz) - timedelta(days=1)
    while not (
        seen >= settles_at(day, tz)
        and (synced(day, upload, cutoff, tz) or seen >= deadline(day, tz, grace))
    ):
        day -= timedelta(days=1)
    return day


def waiting(
    seen: datetime, upload: datetime | None, cutoff: time, grace: timedelta, tz: ZoneInfo
) -> tuple[date, datetime] | None:
    """The oldest day held back for a missing watch sync, with its deadline."""
    yesterday = local_date(seen, tz) - timedelta(days=1)
    for back in range(MAX_GRACE_HOURS // 24 + 1, -1, -1):
        day = yesterday - timedelta(days=back)
        if settles_at(day, tz) <= seen < deadline(day, tz, grace) and not synced(
            day, upload, cutoff, tz
        ):
            return day, deadline(day, tz, grace)
    return None
