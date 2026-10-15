"""Issue #57: a day's totals count only after the watch synced past that day's cutoff;
otherwise the day waits until the grace period runs out, then is final with no data."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import watch
from app.domain.markets import Schedule

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 6)
CUTOFF, GRACE = time(22, 0), timedelta(hours=12)


def ny(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).astimezone(UTC)


NEXT = DAY + timedelta(days=1)


def final(seen: datetime, upload: datetime | None) -> date:
    return watch.final_through(seen, upload, CUTOFF, GRACE, NY)


def test_a_bedtime_sync_makes_the_day_final_at_2am() -> None:
    upload = ny(DAY, 22, 30)
    assert watch.synced(DAY, upload, CUTOFF, NY)
    assert final(ny(NEXT, 1, 59), upload) == DAY - timedelta(days=1)  # not looked at yet
    assert final(ny(NEXT, 2), upload) == DAY


def test_a_sync_before_the_cutoff_does_not_count() -> None:
    upload = ny(DAY, 21, 59)
    assert not watch.synced(DAY, upload, CUTOFF, NY)
    assert watch.synced(DAY - timedelta(days=1), upload, CUTOFF, NY)
    assert final(ny(NEXT, 2), upload) == DAY - timedelta(days=1)
    assert watch.waiting(ny(NEXT, 2), upload, CUTOFF, GRACE, NY) == (DAY, ny(NEXT, 14))


def test_without_a_sync_the_day_is_final_at_the_deadline() -> None:
    upload = ny(DAY, 8)
    assert final(ny(NEXT, 13, 59), upload) == DAY - timedelta(days=1)
    assert final(ny(NEXT, 14), upload) == DAY
    assert watch.waiting(ny(NEXT, 14), upload, CUTOFF, GRACE, NY) is None
    assert watch.final_through(ny(NEXT, 2), upload, CUTOFF, timedelta(0), NY) == DAY


def test_a_morning_sync_inside_the_grace_period_counts() -> None:
    upload = ny(NEXT, 7, 10)
    assert final(ny(NEXT, 7, 15), upload) == DAY
    assert watch.waiting(ny(NEXT, 7, 15), upload, CUTOFF, GRACE, NY) is None


def test_unknown_upload_never_counts_as_synced() -> None:
    assert not watch.synced(DAY, None, CUTOFF, NY)
    assert final(ny(NEXT, 2), None) == DAY - timedelta(days=1)


@given(
    seen_hours=st.integers(0, 24 * 10),
    upload_hours=st.one_of(st.none(), st.integers(-24 * 3, 24 * 10)),
    grace=st.integers(0, watch.MAX_GRACE_HOURS),
)
def test_final_days_are_consistent(seen_hours: int, upload_hours: int | None, grace: int) -> None:
    """Everything up to the answer is final, the next day isn't, and the answer never
    passes the last day that has ended."""
    start = ny(DAY, 0)
    seen = start + timedelta(hours=seen_hours)
    upload = None if upload_hours is None else start + timedelta(hours=upload_hours)
    g = timedelta(hours=grace)
    last = watch.final_through(seen, upload, CUTOFF, g, NY)
    assert seen >= watch.settles_at(last, NY)
    nxt = last + timedelta(days=1)
    assert seen < watch.settles_at(nxt, NY) or (
        not watch.synced(nxt, upload, CUTOFF, NY) and seen < watch.deadline(nxt, NY, g)
    )


def test_parse_settings() -> None:
    assert watch.parse_settings(" 22:30 ", "12") == (time(22, 30), 12)
    assert watch.parse_settings("21:00:45", "0") == (time(21, 0), 0)
    for cutoff, grace, message in (
        ("10pm", "12", "as a time"),
        ("22:00", "1.5", "whole hours"),
        ("22:00", "-1", "whole hours"),
        ("22:00", "49", "0 to 48"),
        ("08:00", "12", "at or after 12:00"),
    ):
        with pytest.raises(ValueError, match=message):
            watch.parse_settings(cutoff, grace)


def test_deadline_label_and_schedule_defaults() -> None:
    assert watch.deadline_label(12) == "14:00 the next day"
    assert watch.deadline_label(22) == "00:00 two days later"
    assert (Schedule().watch_sync_cutoff, Schedule().watch_sync_grace_hours) == (time(22), 12)
    with pytest.raises(ValueError, match="0 to 48"):
        Schedule(watch_sync_grace_hours=72)
