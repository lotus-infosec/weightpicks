"""D-050 (issue #30): weigh-in and workout days come from the reading's UTC time in the
instance's current zone, not the day stored at sync time. Garmin's daily totals keep
their own day."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, update

from app.core.clock import SimClock
from app.core.config import Settings
from app.core.db import immediate
from app.models import InstanceSettingsRow
from app.providers.base import Activity, Batch, DailyTotal, WeighIn
from app.services import instance
from app.services.observations import activity_counts, canonical_weigh_ins
from app.services.settlement import settlement_data
from app.services.sync import run_sync

NY = ZoneInfo("America/New_York")
LA = ZoneInfo("America/Los_Angeles")


class Fixed:
    name = "scripted"

    def __init__(self, batch: Batch) -> None:
        self.batch = batch

    def fetch_since(self, since: datetime, now: datetime) -> Batch:
        return self.batch

    def complete_through(self, now: datetime) -> dict[str, date]:
        return {}


def utc(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def _ingest(engine: Engine, settings: Settings, batch: Batch) -> None:
    clock = SimClock(utc(2026, 10, 7, 12))
    with immediate(engine) as conn:
        instance.ensure(conn, clock, settings)  # America/New_York
    run_sync(engine, clock, Fixed(batch), tz=NY, unit="lb")


def _switch(engine: Engine, zone: str) -> None:
    with immediate(engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(timezone=zone))


def test_weigh_in_days_follow_the_current_zone(migrated_engine: Engine, settings: Settings) -> None:
    _ingest(
        migrated_engine,
        settings,
        Batch(
            weigh_ins=[
                # 06:30 New York = 03:30 Los Angeles: in the window only in New York.
                WeighIn("a", utc(2026, 10, 5, 10, 30), 90000, "scale"),
                # 02:30 UTC Oct 6 = 22:30 New York Oct 5 / 19:30 LA Oct 5: outside both.
                # 13:00 UTC Oct 6 = 09:00 NY / 06:00 LA: in the window in both.
                WeighIn("b", utc(2026, 10, 6, 13, 0), 89900, "scale"),
            ]
        ),
    )
    with migrated_engine.connect() as conn:
        ny = canonical_weigh_ins(conn, date(2026, 10, 5), date(2026, 10, 6))
    assert [(c.local_date, c.value) for c in ny] == [
        (date(2026, 10, 5), 1984),
        (date(2026, 10, 6), 1982),
    ]

    _switch(migrated_engine, "America/Los_Angeles")
    with migrated_engine.connect() as conn:
        la = canonical_weigh_ins(conn, date(2026, 10, 5), date(2026, 10, 6))
        explicit = canonical_weigh_ins(conn, date(2026, 10, 5), date(2026, 10, 6), tz=NY)
    assert [(c.local_date, c.value) for c in la] == [(date(2026, 10, 6), 1982)]
    assert explicit == ny


def test_late_evening_reading_moves_to_the_next_day(
    migrated_engine: Engine, settings: Settings
) -> None:
    # 05:00 Tokyo Oct 6 = 16:00 New York Oct 5 (outside the window there).
    _ingest(
        migrated_engine,
        settings,
        Batch(weigh_ins=[WeighIn("t", utc(2026, 10, 5, 20), 90000, "scale")]),
    )
    with migrated_engine.connect() as conn:
        assert canonical_weigh_ins(conn, date(2026, 10, 4), date(2026, 10, 7)) == []
    _switch(migrated_engine, "Asia/Tokyo")
    with migrated_engine.connect() as conn:
        tokyo = canonical_weigh_ins(conn, date(2026, 10, 4), date(2026, 10, 7))
    assert [c.local_date for c in tokyo] == [date(2026, 10, 6)]


def test_workouts_follow_the_zone_and_daily_totals_do_not(
    migrated_engine: Engine, settings: Settings
) -> None:
    _ingest(
        migrated_engine,
        settings,
        Batch(
            daily_totals=[
                DailyTotal("s5", date(2026, 10, 5), "steps", 8000),
                DailyTotal("s6", date(2026, 10, 6), "steps", 9000),
            ],
            # 02:00 UTC Oct 6 = 22:00 NY Oct 5 = 19:00 LA Oct 5; 06:00 UTC = 02:00 NY / 23:00 LA
            activities=[
                Activity("w1", utc(2026, 10, 6, 2), 45, "run"),
                Activity("w2", utc(2026, 10, 6, 6), 40, "bike"),
            ],
        ),
    )
    with migrated_engine.connect() as conn:
        assert activity_counts(conn, date(2026, 10, 5), date(2026, 10, 6), min_minutes=30) == {
            date(2026, 10, 5): 1,
            date(2026, 10, 6): 1,
        }
    _switch(migrated_engine, "America/Los_Angeles")
    with migrated_engine.connect() as conn:
        assert activity_counts(conn, date(2026, 10, 5), date(2026, 10, 6), min_minutes=30) == {
            date(2026, 10, 5): 2
        }
        steps, _ = settlement_data(conn, "steps", date(2026, 10, 5), date(2026, 10, 6))
        workouts, _ = settlement_data(conn, "workouts", date(2026, 10, 5), date(2026, 10, 6))
    assert steps.daily_totals == {date(2026, 10, 5): 8000, date(2026, 10, 6): 9000}
    assert workouts.daily_totals == {date(2026, 10, 5): 2, date(2026, 10, 6): 0}
