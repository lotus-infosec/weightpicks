from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.schedule import (
    in_weigh_in_window,
    local_date,
    next_local_midnight,
    sync_period_key,
    window_close,
)
from app.domain.units import grams_to_tenths

NY = ZoneInfo("America/New_York")


def at(y: int, m: int, d: int, hh: int, mm: int = 0, tz: ZoneInfo = NY) -> datetime:
    """A local wall-clock time, returned as aware UTC."""
    return datetime(y, m, d, hh, mm, tzinfo=tz).astimezone(UTC)


# ---- units --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("grams", "unit", "tenths"),
    [
        (90718, "lb", 2000),  # 1999.99 -> 200.0 lb
        (90719, "lb", 2000),  # 2000.01
        (90695, "lb", 1999),  # 1999.48 rounds down
        (100000, "lb", 2205),  # 2204.62
        (90718, "kg", 907),  # 907.18
        (150, "kg", 2),  # exactly 1.5 tenths -> half-up
        (149, "kg", 1),
        (90750, "kg", 908),  # exactly 907.5 -> half-up
    ],
)
def test_grams_to_tenths_rounds_half_up_once(grams: int, unit: str, tenths: int) -> None:
    assert grams_to_tenths(grams, unit) == tenths  # type: ignore[arg-type]


def test_grams_to_tenths_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        grams_to_tenths(-1, "lb")
    with pytest.raises(ValueError):
        grams_to_tenths(100, "stone")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        grams_to_tenths(90718.0, "lb")  # type: ignore[arg-type]


@given(st.integers(min_value=0, max_value=400_000))
def test_lb_conversion_is_within_half_a_tenth(grams: int) -> None:
    exact = grams * 10 / 453.59237
    assert abs(grams_to_tenths(grams, "lb") - exact) <= 0.5


# ---- weigh-in window ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("hh", "mm", "inside"),
    [(3, 59, False), (4, 0, True), (7, 30, True), (10, 59, True), (11, 0, False), (21, 0, False)],
)
def test_window_edges(hh: int, mm: int, inside: bool) -> None:
    assert in_weigh_in_window(at(2026, 10, 5, hh, mm), NY) is inside


@pytest.mark.parametrize(
    "day",
    [date(2026, 3, 8), date(2026, 11, 1)],  # spring forward, fall back
)
@pytest.mark.parametrize(("hh", "mm", "inside"), [(3, 59, False), (4, 0, True), (10, 59, True)])
def test_window_on_dst_days_uses_local_wall_clock(
    day: date, hh: int, mm: int, inside: bool
) -> None:
    ts = at(day.year, day.month, day.day, hh, mm)
    assert in_weigh_in_window(ts, NY) is inside
    assert local_date(ts, NY) == day


def test_local_date_differs_from_utc_date_late_evening() -> None:
    ts = at(2026, 10, 5, 22, 30)  # 02:30 UTC on Oct 6
    assert ts.date() == date(2026, 10, 6)
    assert local_date(ts, NY) == date(2026, 10, 5)


def test_window_close_and_midnight_across_dst() -> None:
    assert window_close(date(2026, 11, 1), NY) == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)
    assert window_close(date(2026, 10, 31), NY) == datetime(2026, 10, 31, 15, 0, tzinfo=UTC)
    # The fall-back day is 25 hours long.
    start = next_local_midnight(date(2026, 10, 31), NY)
    end = next_local_midnight(date(2026, 11, 1), NY)
    assert end - start == timedelta(hours=25)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="aware"):
        in_weigh_in_window(datetime(2026, 10, 5, 7), NY)  # noqa: DTZ001
    with pytest.raises(ValueError, match="aware"):
        local_date(datetime(2026, 10, 5, 7), NY)  # noqa: DTZ001


# ---- sync cadence ---------------------------------------------------------------------


def test_sync_every_15_minutes_inside_window() -> None:
    keys = {sync_period_key(at(2026, 10, 5, 4, 0) + timedelta(minutes=m), NY) for m in range(60)}
    assert keys == {
        "2026-10-05T04:00",
        "2026-10-05T04:15",
        "2026-10-05T04:30",
        "2026-10-05T04:45",
    }


def test_sync_every_two_hours_outside_window() -> None:
    day = [at(2026, 10, 5, 0) + timedelta(minutes=m) for m in range(24 * 60)]
    keys = [sync_period_key(ts, NY) for ts in day]
    outside = sorted({k for k in keys if k.endswith("h")})
    assert outside == [
        "2026-10-05T00h",
        "2026-10-05T02h",
        "2026-10-05T10h",  # 11:00-11:59: the post-window sync
        "2026-10-05T12h",
        "2026-10-05T14h",
        "2026-10-05T16h",
        "2026-10-05T18h",
        "2026-10-05T20h",
        "2026-10-05T22h",
    ]
    inside = {k for k in keys if not k.endswith("h")}
    assert len(inside) == 7 * 4  # 04:00-11:00 every 15 minutes
    # 11:00 is outside the window: it belongs to the 10:00-12:00 two-hour bucket.
    assert sync_period_key(at(2026, 10, 5, 11, 0), NY) == "2026-10-05T10h"
