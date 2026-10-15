"""Issue #57: an iPhone doesn't sync Garmin Connect in the background, so a day's totals
count only once the watch synced after that day's cutoff (22:00). Without one, the day's
markets wait (re-checked every sync) until 14:00, then push. A changed total is stored
as a correction; settled markets never change. Synthetic files only, no real data."""

import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.domain.markets import Timeframe
from app.models import Bet, Market, OutboxMessage, Settlement
from app.providers.garmindb import GarminDBProvider
from app.providers.garmindb_runner import GarminRunError
from app.services import board, ledger, markets, settlement
from app.services.observations import latest_totals
from app.services.sync import SyncResult, run_sync
from tests.integration.test_bets_settlement import bet, player
from tests.integration.world import NY, World, local

DAY = date(2026, 10, 6)  # the daily drop on the 5th posts count markets for the 6th


class FakeGarmin:
    """Writes what GarminDB and the recent fetch would: summaries and the watch file."""

    def __init__(self, home: Path) -> None:
        self.data = home / "HealthData"
        self.steps: dict[date, int] = {}
        self.upload: datetime | None = None
        self.fail = False
        self.asked: list[int | None] = []  # days to re-download, per run

    def __call__(self, today: date, days: int | None = None) -> None:
        self.asked.append(days)
        if self.fail:
            raise GarminRunError("Garmin login failed: the token expired")
        for day, steps in self.steps.items():
            path = self.data / "FitFiles" / "Monitoring" / str(day.year)
            path.mkdir(parents=True, exist_ok=True)
            summary = {
                "calendarDate": day.isoformat(),
                "totalSteps": steps,
                "activeSeconds": 1800,
                "moderateIntensityMinutes": 10,
                "activeKilocalories": 300,
            }
            file = path / f"daily_summary_{day}.json"
            file.write_text(json.dumps(summary))
            if self.upload:  # downloaded after the watch's upload, as the real fetch does
                stamp = self.upload.timestamp() + 60
                os.utime(file, (stamp, stamp))
        upload = int(self.upload.timestamp() * 1000) if self.upload else None
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "watch_sync.json").write_text(json.dumps({"upload_ms": upload}))


@pytest.fixture
def garmin(tmp_path: Path) -> FakeGarmin:
    return FakeGarmin(tmp_path)


def provider(garmin: FakeGarmin) -> GarminDBProvider:
    home = garmin.data.parent
    return GarminDBProvider(home, NY, garmin, cutoff=time(22), grace=timedelta(hours=12))


def steps_market(w: World) -> int:
    markets.drop(w.engine, w.clock, w.config, Timeframe.DAILY, date(2026, 10, 5))
    with w.engine.connect() as conn:
        return int(
            conn.execute(
                select(Market.id).where(
                    Market.metric == "steps", Market.timeframe == "daily", Market.window_end == DAY
                )
            ).scalar_one()
        )


def sync_and_settle(
    w: World, p: GarminDBProvider, at: datetime, deep_days: int | None = None
) -> SyncResult:
    w.clock.set(at)
    result = run_sync(w.engine, w.clock, p, tz=NY, unit="lb", deep_days=deep_days)
    markets.lock_due(w.engine, w.clock.now())
    settlement.settle_due(w.engine, w.clock)
    return result


def outcome(w: World, market_id: int) -> dict[str, object] | None:
    with w.engine.connect() as conn:
        found = conn.execute(
            select(Settlement.outcome).where(Settlement.market_id == market_id)
        ).scalar_one_or_none()
    return found


def stored_steps(w: World, day: date) -> int | None:
    with w.engine.connect() as conn:
        rows = latest_totals(conn, day, day, ("steps",))
    return rows[0].value if rows else None


def alerts(w: World, prefix: str) -> list[str]:
    with w.engine.connect() as conn:
        return [
            k
            for (k,) in conn.execute(select(OutboxMessage.dedupe_key))
            if str(k).startswith(prefix)
        ]


def test_a_bedtime_sync_settles_at_2am(world: World, garmin: FakeGarmin) -> None:
    market = steps_market(world)
    garmin.steps = {date(2026, 10, 5): 8_000, DAY: 9_000}
    garmin.upload = local(2026, 10, 6, 22, 30)
    sync_and_settle(world, provider(garmin), local(2026, 10, 7, 2, 5))
    result = outcome(world, market)
    assert result is not None and result["value_x10"] == 90_000
    assert result["reason"] in {"over", "under", "tie"}


def test_no_sync_waits_then_a_morning_sync_settles_on_the_real_total(
    world: World, garmin: FakeGarmin
) -> None:
    market = steps_market(world)
    p = provider(garmin)
    garmin.steps, garmin.upload = {DAY: 1_200}, local(2026, 10, 6, 18)  # partial, too early
    sync_and_settle(world, p, local(2026, 10, 7, 2, 5))
    assert outcome(world, market) is None and stored_steps(world, DAY) is None
    assert alerts(world, "watch_sync_waiting:") == ["watch_sync_waiting:2026-10-06"]

    garmin.steps, garmin.upload = {DAY: 11_000}, local(2026, 10, 7, 7, 5)  # the weigh-in sync
    sync_and_settle(world, p, local(2026, 10, 7, 7, 15))
    result = outcome(world, market)
    assert result is not None and result["value_x10"] == 110_000
    assert stored_steps(world, DAY) == 11_000


def test_no_sync_by_the_deadline_pushes_then_stats_fill_in(
    world: World, garmin: FakeGarmin
) -> None:
    market = steps_market(world)
    user = player(world, 1)
    placed = bet(world, user, market, "over")
    p = provider(garmin)
    garmin.steps, garmin.upload = {DAY: 1_200}, local(2026, 10, 6, 18)
    sync_and_settle(world, p, local(2026, 10, 7, 2, 5))
    sync_and_settle(world, p, local(2026, 10, 7, 13, 55))
    assert outcome(world, market) is None  # still inside the grace period

    sync_and_settle(world, p, local(2026, 10, 7, 14, 5))
    pushed = outcome(world, market)
    assert pushed is not None
    assert (pushed["winner"], pushed["value_x10"], pushed["reason"]) == (
        None,
        None,
        "no_watch_sync",
    )
    with world.engine.connect() as conn:
        assert conn.execute(select(Bet.status).where(Bet.id == placed.bet_id)).scalar_one() == (
            "push"
        )
        mine = {b.bet_id: b for b in board.my_bets(conn, user)}
        assert ledger.verify(conn).ok
    assert mine[placed.bet_id].reason == "Push: the watch didn't sync that night"
    # Every count market of the day pushed; the admin hears about the day once.
    assert alerts(world, "watch_sync_push:") == ["watch_sync_push:2026-10-06"]
    assert stored_steps(world, DAY) is None  # the partial 1,200 was never stored

    garmin.steps, garmin.upload = {DAY: 10_500}, local(2026, 10, 8, 7)
    sync_and_settle(world, p, local(2026, 10, 8, 7, 30))
    assert stored_steps(world, DAY) == 10_500  # Stats and the odds get the real day
    assert outcome(world, market) == pushed


def test_a_failed_sync_never_pushes(world: World, garmin: FakeGarmin) -> None:
    market = steps_market(world)
    p = provider(garmin)
    garmin.steps, garmin.upload = {DAY: 1_200}, local(2026, 10, 6, 18)
    sync_and_settle(world, p, local(2026, 10, 7, 2, 5))
    garmin.fail = True
    result = sync_and_settle(world, p, local(2026, 10, 7, 16))
    assert result.status == "failed"
    assert outcome(world, market) is None  # waits for a good sync instead
    with world.engine.connect() as conn:
        status = conn.execute(select(Market.status).where(Market.id == market)).scalar_one()
    assert status == "locked"


def test_a_changed_total_is_a_correction_and_settled_markets_stay(
    world: World, garmin: FakeGarmin
) -> None:
    market = steps_market(world)
    p = provider(garmin)
    garmin.steps, garmin.upload = {DAY: 9_000}, local(2026, 10, 6, 22, 30)
    sync_and_settle(world, p, local(2026, 10, 7, 2, 5))
    first = outcome(world, market)
    assert first is not None and first["value_x10"] == 90_000

    garmin.steps, garmin.upload = {DAY: 9_400}, local(2026, 10, 7, 7)  # the late steps arrive
    assert sync_and_settle(world, p, local(2026, 10, 7, 7, 15)).rows_corrected == 1
    assert stored_steps(world, DAY) == 9_400
    assert outcome(world, market) == first
    again = sync_and_settle(world, p, local(2026, 10, 7, 7, 30))
    assert (again.rows_corrected, stored_steps(world, DAY)) == (0, 9_400)


def test_the_sync_run_records_the_watch(world: World, garmin: FakeGarmin) -> None:
    from app.models import SyncRun

    garmin.upload = datetime(2026, 10, 7, 2, 30, tzinfo=UTC)
    sync_and_settle(world, provider(garmin), local(2026, 10, 7, 2, 45))
    with world.engine.connect() as conn:
        latest = conn.execute(
            select(SyncRun.watch_upload_at).order_by(SyncRun.id.desc()).limit(1)
        ).scalar_one()
    assert latest == garmin.upload


def test_the_bedtime_reminder_posts_once_at_the_cutoff(world: World) -> None:
    from app.worker.jobs.garmin_sync import WatchSyncReminderJob
    from app.worker.registry import run_due

    job = WatchSyncReminderJob(world.config)
    world.clock.set(local(2026, 10, 5, 21, 59))
    assert job.due(world.clock.now()) == "2026-10-04"
    world.clock.set(local(2026, 10, 5, 22))
    run_due([job], world.engine, world.clock)
    run_due([job], world.engine, world.clock)
    assert alerts(world, "watch_sync_reminder:") == ["watch_sync_reminder:2026-10-05"]
    with world.engine.connect() as conn:
        payload = conn.execute(
            select(OutboxMessage.payload).where(
                OutboxMessage.dedupe_key == "watch_sync_reminder:2026-10-05"
            )
        ).scalar_one()
    assert payload["cutoff"] == "22:00"
    # A worker that was down at 22:00 and wakes after midnight stays quiet.
    late = WatchSyncReminderJob(world.config)
    world.clock.set(local(2026, 10, 7, 0, 30))
    run_due([late], world.engine, world.clock)
    assert alerts(world, "watch_sync_reminder:2026-10-06") == []


def test_sync_now_rereads_a_month_and_corrects_old_days(world: World, garmin: FakeGarmin) -> None:
    """Issue #61: the deep sync corrects a day too old for the scheduled sync to re-read;
    settled markets stay as they were."""
    market = steps_market(world)
    p = provider(garmin)
    garmin.steps, garmin.upload = {DAY: 5_000}, local(2026, 10, 6, 22, 30)
    sync_and_settle(world, p, local(2026, 10, 7, 2, 5))
    settled = outcome(world, market)
    assert settled is not None and settled["value_x10"] == 50_000

    garmin.upload = local(2026, 10, 16, 7)  # syncs keep running; the stored day is old now
    assert sync_and_settle(world, p, local(2026, 10, 16, 8)).rows_corrected == 0
    garmin.steps, garmin.upload = {DAY: 10_200}, local(2026, 10, 17, 7)  # Garmin changed it
    scheduled = sync_and_settle(world, p, local(2026, 10, 17, 8))
    assert scheduled.rows_corrected == 0 and stored_steps(world, DAY) == 5_000  # too old
    deep = sync_and_settle(world, p, local(2026, 10, 17, 8, 15), deep_days=30)
    assert deep.rows_corrected == 1 and stored_steps(world, DAY) == 10_200
    assert garmin.asked[-2:] == [None, 30]
    assert outcome(world, market) == settled
    again = sync_and_settle(world, p, local(2026, 10, 17, 8, 30), deep_days=30)
    assert again.rows_corrected == 0
