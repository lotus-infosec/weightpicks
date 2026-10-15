"""Provider contract tests on synthetic files shaped like GarminDB's downloads.
No real data: every value here is made up."""

import json
import os
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


def watch_file(upload: datetime | None, device: str = "Test watch") -> dict[str, object]:
    return {"upload_ms": ms(upload) if upload else None, "device": device}


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
    # The watch last synced at 02:00 on the 3rd: after the 1st's and 2nd's 22:00 cutoffs.
    write(data / "watch_sync.json", watch_file(datetime(2026, 10, 3, 6, 0, tzinfo=UTC)))
    acts = data / "FitFiles" / "Activities"
    write(acts / "activity_501.json", activity(501, "2026-10-02 22:00:00", 1800))
    write(acts / "activity_details_501.json", {"activityId": 501, "detail": True})
    write(acts / "activity_502.json", activity(502, "2026-10-03 12:00:00", 1800))  # today
    return tmp_path


def provider(home: Path, calls: list[date] | None = None) -> GarminDBProvider:
    seen = calls if calls is not None else []

    def runner(today: date, days: int | None = None) -> None:
        seen.append(today)

    return GarminDBProvider(home, NY, runner)


def test_fetch_reads_every_file_once_and_only_final_count_days(home: Path) -> None:
    calls: list[date] = []
    p = provider(home, calls)
    now = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)  # 11:00 local: window closed
    batch = p.fetch_since(datetime(2026, 9, 1, tzinfo=UTC), now)
    assert calls == [date(2026, 10, 3)]
    assert (batch.watch_upload_at, batch.watch_device, batch.watch_waiting) == (
        datetime(2026, 10, 3, 6, 0, tzinfo=UTC),
        "Test watch",
        None,
    )
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
    def broken(today: date, days: int | None = None) -> None:
        raise GarminRunError("Garmin login failed: rerun garmin-login")

    p = GarminDBProvider(home, NY, broken)
    with pytest.raises(GarminRunError, match="garmin-login"):
        p.fetch_since(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 3, 15, tzinfo=UTC))
    assert p.fetched_at is None


def test_missing_tree_is_an_empty_batch(tmp_path: Path) -> None:
    write(tmp_path / "HealthData" / "watch_sync.json", watch_file(None))
    batch = provider(tmp_path).fetch_since(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 3, 15, tzinfo=UTC)
    )
    assert len(batch) == 0


def test_without_the_watch_sync_time_the_sync_fails(tmp_path: Path) -> None:
    """Unknown is not "the watch didn't sync": failing keeps markets waiting, never pushing."""
    p = provider(tmp_path)
    with pytest.raises(GarminRunError, match="last sync time"):
        p.fetch_since(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 3, 15, tzinfo=UTC))
    assert p.fetched_at is None


# ---- the watch's last sync (issue #57) ---------------------------------------------------


def set_watch(home: Path, upload: datetime) -> None:
    write(home / "HealthData" / "watch_sync.json", watch_file(upload))


def test_a_day_the_watch_never_synced_is_held_back_then_final_without_data(home: Path) -> None:
    set_watch(home, datetime(2026, 10, 2, 22, 0, tzinfo=UTC))  # 18:00 on the 2nd: too early
    p = provider(home)
    at_2am = datetime(2026, 10, 3, 6, 30, tzinfo=UTC)  # 02:30 on the 3rd
    batch = p.fetch_since(at_2am - timedelta(days=1), at_2am)
    assert {t.local_date for t in batch.daily_totals} == {date(2026, 10, 1)}  # never the 2nd
    assert p.complete_through(at_2am)["steps"] == date(2026, 10, 1)
    deadline = datetime(2026, 10, 3, 18, 0, tzinfo=UTC)  # 14:00 on the 3rd
    assert batch.watch_waiting == (date(2026, 10, 2), deadline)

    after = deadline + timedelta(minutes=30)
    batch = p.fetch_since(after - timedelta(days=1), after)
    assert p.complete_through(after)["steps"] == date(2026, 10, 2)  # final: its markets push
    assert date(2026, 10, 2) not in {t.local_date for t in batch.daily_totals}
    assert batch.watch_waiting is None


def test_a_morning_sync_fills_the_day_in(home: Path) -> None:
    set_watch(home, datetime(2026, 10, 3, 11, 5, tzinfo=UTC))  # 07:05 on the 3rd
    p = provider(home)
    now = datetime(2026, 10, 3, 11, 30, tzinfo=UTC)
    batch = p.fetch_since(now - timedelta(days=1), now)
    assert date(2026, 10, 2) in {t.local_date for t in batch.daily_totals}
    assert p.complete_through(now)["steps"] == date(2026, 10, 2)


def test_a_summary_saved_before_the_upload_is_not_trusted(home: Path) -> None:
    upload = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
    set_watch(home, upload)
    stale = (
        home / "HealthData" / "FitFiles" / "Monitoring" / "2026" / "daily_summary_2026-10-02.json"
    )
    old = (upload - timedelta(hours=3)).timestamp()
    os.utime(stale, (old, old))  # downloaded before the watch's last upload: maybe partial
    now = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
    batch = provider(home).fetch_since(now - timedelta(days=1), now)
    assert {t.local_date for t in batch.daily_totals} == {date(2026, 10, 1)}
