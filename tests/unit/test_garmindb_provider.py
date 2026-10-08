"""Provider contract tests on synthetic files shaped like GarminDB's downloads.
No real data: every value here is made up."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.domain.schedule import in_weigh_in_window, local_date
from app.providers.garmindb import (
    GarminDBProvider,
    parse_activity,
    parse_daily_summary,
    parse_weigh_ins,
)
from app.providers.garmindb_runner import GarminRunError

NY = ZoneInfo("America/New_York")


def ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def weigh_in(pk: int, utc: datetime, grams: float, source: str = "MANUAL") -> dict[str, object]:
    local_as_utc = utc.astimezone(NY).replace(tzinfo=UTC)  # Garmin's misleading `date`
    return {
        "samplePk": pk,
        "calendarDate": utc.astimezone(NY).date().isoformat(),
        "date": ms(local_as_utc),
        "timestampGMT": ms(utc),
        "sourceType": source,
        "weight": grams,
        "bmi": None,
        "bodyFat": None,
    }


def day_file(entries: list[dict[str, object]]) -> dict[str, object]:
    return {"startDate": "x", "endDate": "x", "dateWeightList": entries, "totalAverage": {}}


def summary(day: date, steps: int | None = 6000) -> dict[str, object]:
    return {
        "calendarDate": day.isoformat(),
        "totalSteps": steps,
        "activeSeconds": 3600,
        "highlyActiveSeconds": 630,
        "moderateIntensityMinutes": 20,
        "vigorousIntensityMinutes": 5,
        "activeKilocalories": 412.0,
        "totalKilocalories": 3000.0,
        "includesWellnessData": True,
    }


def activity(activity_id: int, start_gmt: str, seconds: float) -> dict[str, object]:
    return {
        "activityId": activity_id,
        "startTimeGMT": start_gmt,
        "startTimeLocal": "ignored",
        "duration": seconds,
        "activityType": {"typeKey": "walking"},
    }


def write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# ---- parsing -----------------------------------------------------------------------


def test_weigh_in_uses_the_real_instant_not_the_local_date_field() -> None:
    at = datetime(2026, 9, 14, 12, 42, 10, tzinfo=UTC)  # 08:42:10 in New York (EDT)
    (w,) = parse_weigh_ins(day_file([weigh_in(1700000000123, at, 88450.4)]))
    assert w.at == at and w.grams == 88450 and w.source == "manual"
    assert w.ref == "garmin:w:1700000000123"
    assert in_weigh_in_window(w.at, NY)
    # The trap: reading `date` as UTC would put this at 04:42 local, a different answer
    # for an 06:00 window edge and for any zone near the date line.
    shifted = datetime.fromtimestamp(weigh_in(1, at, 1.0)["date"] / 1000, UTC)  # type: ignore[operator]
    assert shifted.astimezone(NY).hour == 4


def test_scale_entries_bad_rows_and_empty_days() -> None:
    at = datetime(2026, 3, 9, 11, 0, tzinfo=UTC)
    data = day_file(
        [
            weigh_in(1, at, 90000.4, "INDEX_SCALE"),
            {"samplePk": 2, "timestampGMT": None, "weight": 90000.0},
            {"samplePk": 3, "timestampGMT": ms(at), "weight": 0},
            {"timestampGMT": ms(at), "weight": 90000.0},
        ]
    )
    assert [(w.ref, w.source, w.grams) for w in parse_weigh_ins(data)] == [
        ("garmin:w:1", "scale", 90000)
    ]
    assert list(parse_weigh_ins(day_file([]))) == []
    assert list(parse_weigh_ins(None)) == []


def test_daily_summary_maps_garmin_fields() -> None:
    totals = {t.metric: t.value for t in parse_daily_summary(summary(date(2026, 10, 1)))}
    assert totals == {
        "steps": 6000,
        "active_minutes": 60 + 10,  # active + highly active seconds, in whole minutes
        "intensity_minutes": 20 + 2 * 5,  # Garmin counts vigorous minutes twice
        "kcal": 412,  # active calories, not the BMR-inclusive total
    }
    assert parse_daily_summary(summary(date(2026, 10, 1), steps=None)) == []  # no data != 0
    assert parse_daily_summary({"totalSteps": 5}) == []


def test_activity_parsing() -> None:
    a = parse_activity(activity(77, "2026-08-05 01:10:16", 2719.27))
    assert a is not None
    assert (a.ref, a.at, a.duration_min, a.kind) == (
        "garmin:a:77",
        datetime(2026, 8, 5, 1, 10, 16, tzinfo=UTC),
        45,
        "walking",
    )
    assert local_date(a.at, NY) == date(2026, 8, 4)  # an evening walk counts for the 4th
    assert parse_activity(activity(78, "2026-08-05 01:10:16", 9 * 60)) is None  # < 10 min
    assert parse_activity({"activityId": 1}) is None


# ---- the provider over a downloaded tree ---------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    data = tmp_path / "HealthData"
    write(
        data / "Weight" / "weight_2026-10-01.json",
        day_file([weigh_in(10, datetime(2026, 10, 1, 11, 5, tzinfo=UTC), 90100.0)]),
    )
    write(data / "Weight" / "weight_2026-10-02.json", day_file([]))
    (data / "Weight" / "weight_2026-09-30.json").write_text("{not json")  # corrupt: skipped
    write(  # GarminDB never fetches today; the recent file carries it (and repeats the 1st)
        data / "Weight" / "weight_recent.json",
        day_file(
            [
                weigh_in(10, datetime(2026, 10, 1, 11, 5, tzinfo=UTC), 90100.0),
                weigh_in(11, datetime(2026, 10, 3, 12, 51, tzinfo=UTC), 89900.0),
                weigh_in(12, datetime(2026, 10, 3, 13, 40, tzinfo=UTC), 89850.0, "INDEX_SCALE"),
            ]
        ),
    )
    for day in (date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)):
        write(
            data / "FitFiles" / "Monitoring" / "2026" / f"daily_summary_{day.isoformat()}.json",
            summary(day),
        )
    acts = data / "FitFiles" / "Activities"
    write(acts / "activity_501.json", activity(501, "2026-10-02 22:00:00", 1800))
    write(acts / "activity_details_501.json", {"activityId": 501, "detail": True})
    write(acts / "activity_502.json", activity(502, "2026-10-03 12:00:00", 1800))  # today
    return tmp_path


def provider(home: Path, calls: list[date] | None = None) -> GarminDBProvider:
    seen = calls if calls is not None else []
    return GarminDBProvider(home, NY, seen.append)


def test_fetch_reads_every_file_once_and_only_final_count_days(home: Path) -> None:
    calls: list[date] = []
    p = provider(home, calls)
    now = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)  # 11:00 local: window closed
    batch = p.fetch_since(datetime(2026, 9, 1, tzinfo=UTC), now)
    assert calls == [date(2026, 10, 3)]
    assert sorted(w.ref for w in batch.weigh_ins) == ["garmin:w:10", "garmin:w:11", "garmin:w:12"]
    # Today's steps are still growing and yesterday's only settle 2 h after midnight.
    assert {t.local_date for t in batch.daily_totals} == {date(2026, 10, 1), date(2026, 10, 2)}
    assert [a.ref for a in batch.activities] == ["garmin:a:501"]
    assert p.complete_through(now) == {
        "weight": date(2026, 10, 3),
        "workout": date(2026, 10, 2),
        "steps": date(2026, 10, 2),
        "active_minutes": date(2026, 10, 2),
        "intensity_minutes": date(2026, 10, 2),
        "kcal": date(2026, 10, 2),
    }


def test_completeness_waits_for_a_download_after_the_day_ends(home: Path) -> None:
    p = provider(home)
    early = datetime(2026, 10, 3, 5, 30, tzinfo=UTC)  # 01:30 local, before counts settle
    batch = p.fetch_since(early - timedelta(days=1), early)
    assert p.complete_through(early)["steps"] == date(2026, 10, 1)
    assert p.complete_through(early)["weight"] == date(2026, 10, 2)
    assert {t.local_date for t in batch.daily_totals} == {date(2026, 10, 1)}
    # Without any download yet, nothing beyond what an earlier run could have seen.
    fresh = provider(home)
    assert fresh.complete_through(early)["weight"] == date(2026, 10, 2)


def test_runner_failure_propagates_and_nothing_is_read(home: Path) -> None:
    def broken(_today: date) -> None:
        raise GarminRunError("Garmin login failed: rerun garmin-login")

    p = GarminDBProvider(home, NY, broken)
    with pytest.raises(GarminRunError, match="garmin-login"):
        p.fetch_since(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 3, 15, tzinfo=UTC))
    assert p.fetched_at is None


def test_missing_tree_is_an_empty_batch(tmp_path: Path) -> None:
    batch = provider(tmp_path).fetch_since(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 3, 15, tzinfo=UTC)
    )
    assert len(batch) == 0
