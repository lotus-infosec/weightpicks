from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, func, select, update

from app.core.clock import SimClock
from app.core.config import Settings
from app.core.db import immediate
from app.domain.markets import InvalidTransition, MarketStatus, Timeframe
from app.models import InstanceSettingsRow, JobRun, Market, OddsVersion, Selection
from app.providers.simulated import SimulatedProvider
from app.services import instance, markets, sim
from app.services.instance import InstanceConfig
from app.services.ledger import open_season
from app.services.sync import run_sync
from app.worker.jobs import domain_jobs
from app.worker.jobs.markets import LOCK_RECHECK, LockMarketsJob, LockSchedule
from app.worker.registry import Job, JobContext, run_due

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 5)  # a Monday


def local(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY).astimezone(UTC)


@dataclass
class World:
    engine: Engine
    clock: SimClock
    config: InstanceConfig
    settings: Settings


def _sync(engine: Engine, clock: SimClock) -> None:
    provider = SimulatedProvider(preset="steady-loser", seed=3, anchor_date=date(2026, 9, 1), tz=NY)
    assert run_sync(engine, clock, provider, tz=NY, unit="lb").status == "ok"


@pytest.fixture
def world(migrated_engine: Engine, settings: Settings) -> World:
    """Five weeks of simulated history, a season, the settings row; 12:00 on Mon Oct 5."""
    clock = SimClock(local(2026, 10, 5, 12))
    _sync(migrated_engine, clock)
    with immediate(migrated_engine) as conn:
        open_season(conn, clock)
        config = instance.ensure(conn, clock, settings)
    return World(migrated_engine, clock, config, settings)


def market_rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(Market).order_by(Market.id)).all())


def count(engine: Engine, model: Any) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(model)).scalar_one()


def statuses(engine: Engine) -> Counter[str]:
    with engine.connect() as conn:
        return Counter(conn.execute(select(Market.status)).scalars())


def market_jobs(w: World) -> list[Job]:
    return [j for j in domain_jobs(w.settings, w.config) if j.name != "garmin_sync"]


# ---- drops -----------------------------------------------------------------------------


def test_daily_drop_posts_priced_open_markets(world: World) -> None:
    result = markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, DAY)
    assert len(result.created) == 5 and result.skipped == {}
    with world.engine.connect() as conn:
        rows = conn.execute(select(Market).order_by(Market.id)).all()
        weight = rows[0]
        assert weight.template == "weight_change_ou"
        assert (weight.status, weight.origin, weight.timeframe) == ("open", "core", "daily")
        assert weight.params == {"d0": "2026-10-05", "d1": "2026-10-06"}
        assert weight.correlation_keys == ["weight:2026-10-05", "weight:2026-10-06"]
        assert weight.opens_at == world.clock.now()
        assert weight.lock_at == local(2026, 10, 5, 22)
        assert weight.settle_after == local(2026, 10, 6, 11)
        assert [r.metric for r in rows] == [
            "weight",
            "steps",
            "active_minutes",
            "intensity_minutes",
            "kcal",
        ]
        sides = conn.execute(
            select(Selection.side).where(Selection.market_id == weight.id).order_by(Selection.id)
        ).scalars()
        assert list(sides) == ["over", "under"]
        version = conn.execute(select(OddsVersion).where(OddsVersion.market_id == weight.id)).one()
    assert (version.version, version.is_current) == (1, True)
    assert abs(version.line_x10) % 10 == 5
    assert set(version.odds) == {"over", "under"}
    assert version.model_inputs["start_known"] is True
    assert 0 < version.model_inputs["p_over"] < 1
    assert count(world.engine, Selection) == 10
    assert count(world.engine, OddsVersion) == 5


def test_repeat_drop_creates_nothing(world: World) -> None:
    first = markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, DAY)
    world.clock.advance(timedelta(minutes=30))
    again = markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, DAY)
    assert again.created == []
    assert set(again.skipped.values()) == {"exists"} and len(again.skipped) == len(first.created)
    assert count(world.engine, Market) == 5


def test_weekly_and_monthly_drops(world: World) -> None:
    world.clock.set(local(2026, 10, 4, 18))  # Sunday drop
    weekly = markets.drop(
        world.engine, world.clock, world.config, Timeframe.WEEKLY, date(2026, 10, 4)
    )
    assert len(weekly.created) == 6
    world.clock.set(local(2026, 10, 31, 18))
    _sync(world.engine, world.clock)
    monthly = markets.drop(
        world.engine, world.clock, world.config, Timeframe.MONTHLY, date(2026, 10, 31)
    )
    assert len(monthly.created) == 6
    rows = [r for r in market_rows(world.engine) if r.timeframe == "monthly"]
    assert {(r.window_start, r.window_end) for r in rows[1:]} == {
        (date(2026, 11, 1), date(2026, 11, 30))
    }
    assert rows[0].lock_at == local(2026, 11, 29, 22)  # EST after the DST change


@pytest.mark.parametrize(
    ("now", "reason"),
    [
        (local(2026, 10, 5, 22, 0), "lock_passed"),
        (local(2026, 10, 6, 9, 0), "stale_drop"),  # caught up after its day ended
    ],
)
def test_drop_skips_late_markets(world: World, now: datetime, reason: str) -> None:
    world.clock.set(now)
    result = markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, DAY)
    assert result.created == []
    assert set(result.skipped.values()) == {reason}


def test_drop_needs_a_season(migrated_engine: Engine, settings: Settings) -> None:
    clock = SimClock(local(2026, 10, 5, 12))
    _sync(migrated_engine, clock)
    with immediate(migrated_engine) as conn:
        config = instance.ensure(conn, clock, settings)
    result = markets.drop(migrated_engine, clock, config, Timeframe.DAILY, DAY)
    assert result.created == [] and set(result.skipped.values()) == {"no_season"}


def test_counts_without_history_are_skipped(migrated_engine: Engine, settings: Settings) -> None:
    clock = SimClock(local(2026, 10, 5, 12))
    with immediate(migrated_engine) as conn:
        open_season(conn, clock)
        config = instance.ensure(conn, clock, settings)
    result = markets.drop(migrated_engine, clock, config, Timeframe.DAILY, DAY)
    # Weight still drops from the provisional prior; counts need history.
    assert len(result.created) == 1
    assert list(result.skipped.values()) == ["no_history"] * 4


def test_enabled_metrics_setting_is_respected(world: World) -> None:
    with immediate(world.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(enabled_metrics=["kcal", "sleep"]))
        config = instance.read(conn)
    assert config is not None and config.enabled_metrics == ("kcal",)
    result = markets.drop(world.engine, world.clock, config, Timeframe.DAILY, DAY)
    assert [r.metric for r in market_rows(world.engine)] == ["weight", "kcal"]
    assert len(result.created) == 2


# ---- restart safety --------------------------------------------------------------------


def test_crash_mid_drop_rolls_back_and_the_retry_creates_each_market_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = markets.insert_market
    calls = 0

    def flaky(*args: Any, **kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("worker killed mid-drop")
        return real(*args, **kwargs)

    monkeypatch.setattr(markets, "insert_market", flaky)
    with pytest.raises(RuntimeError):
        markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, DAY)
    assert count(world.engine, Market) == 0  # all or nothing
    assert count(world.engine, Selection) == 0
    monkeypatch.setattr(markets, "insert_market", real)
    assert (
        len(markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, DAY).created)
        == 5
    )


def test_restart_during_a_drop_does_not_duplicate(world: World) -> None:
    world.clock.set(local(2026, 10, 5, 11))
    assert "daily_drop" in run_due(market_jobs(world), world.engine, world.clock, seen={})
    assert count(world.engine, Market) == 5
    # The process dies after committing the markets but before recording the finish.
    with immediate(world.engine) as conn:
        conn.execute(update(JobRun).where(JobRun.job == "daily_drop").values(status="running"))
    # A fresh process (new jobs, empty seen-cache) ticks again at the same and later times.
    for minutes in (0, 1, 30):
        world.clock.set(local(2026, 10, 5, 11) + timedelta(minutes=minutes))
        ran = run_due(market_jobs(world), world.engine, world.clock, seen={})
        assert "daily_drop" not in ran
    # Even a forced re-run of the same drop is a no-op thanks to the dedupe key.
    markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, DAY)
    with world.engine.connect() as conn:
        keys = list(conn.execute(select(Market.dedupe_key)).scalars())
    assert len(keys) == len(set(keys)) == 5


# ---- locking ---------------------------------------------------------------------------


def test_markets_lock_at_their_lock_time(world: World) -> None:
    jobs = market_jobs(world)
    world.clock.set(local(2026, 10, 5, 11))
    run_due(jobs, world.engine, world.clock, seen={})
    world.clock.set(local(2026, 10, 5, 21, 59))
    assert "lock_markets" not in run_due(jobs, world.engine, world.clock, seen={})
    assert statuses(world.engine) == {"open": 5}
    world.clock.set(local(2026, 10, 5, 22, 0))
    assert "lock_markets" in run_due(jobs, world.engine, world.clock, seen={})
    assert statuses(world.engine) == {"locked": 5}
    with world.engine.connect() as conn:
        changed = set(conn.execute(select(Market.status_changed_at)).scalars())
    assert changed == {local(2026, 10, 5, 22, 0)}


def test_lock_cache_still_finds_markets_created_elsewhere(world: World) -> None:
    locks = LockSchedule()
    job = LockMarketsJob(locks)

    def tick() -> int:
        now = world.clock.now()
        return job.run(JobContext(world.engine, world.clock, now, "tick"))

    assert tick() == 0 and locks.next_lock is None
    # Another process drops markets; this process's cache is not told.
    markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, DAY)
    world.clock.set(local(2026, 10, 5, 22, 0))
    assert tick() == 5
    assert locks.next_lock is None


def test_lock_schedule_cache() -> None:
    locks = LockSchedule()
    t = local(2026, 10, 5, 12)
    assert locks.needs_check(t)
    locks.next_lock, locks.checked_at = t + timedelta(hours=10), t
    assert not locks.needs_check(t + timedelta(minutes=1))
    assert locks.needs_check(t + LOCK_RECHECK)
    assert locks.needs_check(t - timedelta(minutes=1))  # clock moved back: recheck
    locks.next_lock = t + timedelta(minutes=2)
    assert locks.needs_check(t + timedelta(minutes=2))
    locks.invalidate()
    assert locks.needs_check(t + timedelta(seconds=1))


def test_freezing_locks_every_open_market_and_stops_drops(world: World) -> None:
    markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, DAY)
    with immediate(world.engine) as conn:
        assert instance.set_instance_state(conn, world.clock, instance.FROZEN) == 5
    assert statuses(world.engine) == {"locked": 5}
    world.clock.set(local(2026, 10, 4, 18))
    result = markets.drop(
        world.engine, world.clock, world.config, Timeframe.WEEKLY, date(2026, 10, 4)
    )
    assert result.created == [] and set(result.skipped.values()) == {"frozen"}
    # Backstop: a market that appears while frozen is locked by the next lock pass.
    with immediate(world.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(instance_state="active"))
    markets.drop(world.engine, world.clock, world.config, Timeframe.WEEKLY, date(2026, 10, 4))
    with immediate(world.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(instance_state="frozen"))
    assert markets.lock_due(world.engine, world.clock.now()).locked == 6
    assert markets.lock_due(world.engine, world.clock.now()) == markets.LockPass(0, None)
    with immediate(world.engine) as conn:
        assert instance.set_instance_state(conn, world.clock, instance.ACTIVE) == 0
        assert instance.current_state(conn) == "active"


def test_set_status_enforces_the_state_machine(world: World) -> None:
    created = markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, DAY).created
    now = world.clock.now()
    with immediate(world.engine) as conn:
        with pytest.raises(InvalidTransition):
            markets.set_status(conn, created, MarketStatus.OPEN, MarketStatus.SETTLED, now)
        # Only rows actually in `current` move.
        assert (
            markets.set_status(conn, created[:2], MarketStatus.OPEN, MarketStatus.LOCKED, now) == 2
        )
        assert markets.set_status(conn, created, MarketStatus.OPEN, MarketStatus.VOIDED, now) == 3
        assert markets.set_status(conn, [], MarketStatus.LOCKED, MarketStatus.VOIDED, now) == 0
    assert statuses(world.engine) == {"locked": 2, "voided": 3}


# ---- settings row ----------------------------------------------------------------------


def test_instance_settings_row(migrated_engine: Engine, settings: Settings) -> None:
    clock = SimClock(local(2026, 10, 5, 12))
    with migrated_engine.connect() as conn:
        assert instance.read(conn) is None
        assert instance.current_state(conn) == "active"
    with immediate(migrated_engine) as conn, pytest.raises(LookupError):
        instance.set_instance_state(conn, clock, instance.FROZEN)
    config = instance.load(migrated_engine, clock, settings)
    assert config == instance.load(migrated_engine, clock, settings)
    assert (config.timezone, config.unit, config.state) == ("America/New_York", "lb", "active")
    assert config.schedule.daily_drop == time(11) and config.schedule.bet_lock == time(22)
    assert (
        instance.schedule_from_json(instance.schedule_to_json(config.schedule)) == config.schedule
    )
    assert instance.schedule_from_json({"bet_lock": "21:30"}).bet_lock == time(21, 30)
    with immediate(migrated_engine) as conn, pytest.raises(ValueError, match="state"):
        instance.set_instance_state(conn, clock, "paused")


# ---- measure of success ------------------------------------------------------------------


def test_fourteen_simulated_days_drop_and_lock_on_schedule(
    migrated_engine: Engine, settings: Settings
) -> None:
    """STAGE05 measure, across the Nov 1 DST change and the Oct 31 monthly drop."""
    dev = settings.model_copy(update={"data_provider": "simulated"})
    start = local(2026, 10, 26, 0, 30)
    with immediate(migrated_engine) as conn:
        sim.ensure_state(conn, SimClock(start), seed=7, tz_name="America/New_York")
        open_season(conn, SimClock(start))
    end = sim.advance(migrated_engine, dev, timedelta(days=14)).end
    rows = market_rows(migrated_engine)
    by_kind = Counter((r.timeframe, r.metric) for r in rows)
    assert by_kind[("daily", "weight")] == 14
    assert by_kind[("weekly", "weight")] == 2
    assert by_kind[("monthly", "weight")] == 1
    # Day 1 has no completed count history yet; every later day prices 4 daily counts.
    for metric in ("steps", "active_minutes", "intensity_minutes", "kcal"):
        assert by_kind[("daily", metric)] == 13
    for metric in ("steps", "active_minutes", "intensity_minutes", "kcal", "workouts"):
        assert by_kind[("weekly", metric)] == 2
        assert by_kind[("monthly", metric)] == 1
    assert ("daily", "workouts") not in by_kind
    for r in rows:
        assert r.lock_at.astimezone(NY).time() == time(22, 0), r.title
        assert r.status == ("locked" if r.lock_at <= end else "open"), r.title
    daily_weight = sorted(
        r.window_start for r in rows if r.timeframe == "daily" and r.metric == "weight"
    )
    assert daily_weight == [date(2026, 10, 26) + timedelta(days=k) for k in range(14)]
