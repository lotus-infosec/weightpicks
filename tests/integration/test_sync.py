from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.clock import SimClock
from app.core.config import Settings
from app.core.db import immediate
from app.domain.markets import Timeframe
from app.models import Market, Observation, OutboxMessage, SyncRun
from app.providers.base import Batch, DailyTotal, DataProvider, WeighIn
from app.providers.garmindb import GarminDBProvider
from app.providers.garmindb_runner import GarminRunError
from app.providers.simulated import SimulatedProvider
from app.services import markets, settlement
from app.services.instance import InstanceConfig
from app.services.ledger import open_season
from app.services.observations import canonical_weigh_ins, latest_complete_through
from app.services.sync import ProviderUnavailable, SyncResult, make_provider, run_sync
from app.worker.jobs.garmin_sync import GarminSyncJob
from app.worker.registry import run_due
from tests.integration.world import World

NY = ZoneInfo("America/New_York")


def local(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY).astimezone(UTC)


class ScriptedProvider:
    """Returns exactly the records it is given, filtered like a real provider."""

    name = "scripted"

    def __init__(self) -> None:
        self.weigh_ins: list[WeighIn] = []
        self.totals: list[tuple[DailyTotal, datetime]] = []
        self.complete: dict[str, date] = {}
        self.fail = False

    def fetch_since(self, since: datetime, now: datetime) -> Batch:
        if self.fail:
            raise ConnectionError("garmin down")
        return Batch(
            weigh_ins=[w for w in self.weigh_ins if since < w.at <= now],
            daily_totals=[t for t, ready in self.totals if since < ready <= now],
        )

    def complete_through(self, now: datetime) -> dict[str, date]:
        return dict(self.complete)


def _sync(engine: Engine, clock: SimClock, provider: DataProvider) -> SyncResult:
    return run_sync(engine, clock, provider, tz=NY, unit="lb")


def _count(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(Observation)).scalar_one()


def test_ingest_is_idempotent_and_overlap_safe(migrated_engine: Engine) -> None:
    clock = SimClock(local(2026, 10, 20, 12))
    provider = SimulatedProvider(
        preset="steady-loser", seed=1, anchor_date=date(2026, 10, 1), tz=NY
    )
    first = run_sync(migrated_engine, clock, provider, tz=NY, unit="lb")
    total = _count(migrated_engine)
    second = run_sync(migrated_engine, clock, provider, tz=NY, unit="lb")
    assert (first.status, second.status) == ("ok", "ok")
    assert first.rows_new == total > 50
    assert second.rows_new == 0
    assert _count(migrated_engine) == total
    # Later syncs fetch with a one-day overlap; still no duplicates.
    clock.advance(timedelta(hours=2))
    third = run_sync(migrated_engine, clock, provider, tz=NY, unit="lb")
    assert _count(migrated_engine) == total + third.rows_new
    with migrated_engine.connect() as conn:
        dupes = conn.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT metric, source_ref FROM observations "
                "GROUP BY 1, 2 HAVING COUNT(*) > 1)"
            )
        ).scalar_one()
    assert dupes == 0


def test_weight_is_converted_once_and_raw_grams_kept(migrated_engine: Engine) -> None:
    provider = ScriptedProvider()
    provider.weigh_ins = [WeighIn("w1", local(2026, 10, 5, 6, 10), 90718, "scale")]
    _sync(migrated_engine, SimClock(local(2026, 10, 5, 12)), provider)
    with migrated_engine.connect() as conn:
        row = conn.execute(select(Observation)).one()
    assert (row.metric, row.value, row.raw_value, row.source, row.in_window) == (
        "weight",
        2000,
        90718,
        "scale",
        True,
    )
    assert row.local_date == date(2026, 10, 5)
    assert row.observed_at == local(2026, 10, 5, 6, 10)


def test_canonical_is_earliest_in_window_including_late_backfill(migrated_engine: Engine) -> None:
    provider = ScriptedProvider()
    provider.weigh_ins = [
        WeighIn("early", local(2026, 10, 5, 3, 50), 91000, "scale"),  # before the window
        WeighIn("first", local(2026, 10, 5, 6, 10), 90718, "scale"),
        WeighIn("second", local(2026, 10, 5, 6, 40), 90800, "scale"),
        WeighIn("evening", local(2026, 10, 5, 19, 0), 91500, "scale"),
    ]
    clock = SimClock(local(2026, 10, 5, 20))
    _sync(migrated_engine, clock, provider)
    with migrated_engine.connect() as conn:
        canon = canonical_weigh_ins(conn, date(2026, 10, 5), date(2026, 10, 5))
    assert [(c.local_date, c.value, c.source) for c in canon] == [
        (date(2026, 10, 5), 2000, "scale")
    ]

    # A manual entry timestamped earlier arrives in a later sync: it becomes canonical.
    provider.weigh_ins.append(WeighIn("backfill", local(2026, 10, 5, 5, 30), 90300, "manual"))
    clock.advance(timedelta(minutes=15))
    run_sync(migrated_engine, clock, ScriptedProviderAll(provider), tz=NY, unit="lb")
    with migrated_engine.connect() as conn:
        canon = canonical_weigh_ins(conn, date(2026, 10, 5), date(2026, 10, 5))
    assert [(c.value, c.source) for c in canon] == [(1991, "manual")]
    assert canon[0].observed_at == local(2026, 10, 5, 5, 30)


class ScriptedProviderAll(ScriptedProvider):
    """Ignores `since`: models a provider that surfaces old records late (manual backfill)."""

    def __init__(self, inner: ScriptedProvider) -> None:
        super().__init__()
        self.weigh_ins = inner.weigh_ins
        self.name = inner.name

    def fetch_since(self, since: datetime, now: datetime) -> Batch:
        return Batch(weigh_ins=[w for w in self.weigh_ins if w.at <= now])


def test_canonical_on_dst_day(migrated_engine: Engine) -> None:
    provider = ScriptedProvider()
    provider.weigh_ins = [
        WeighIn("dst-early", local(2026, 11, 1, 3, 45), 90000, "scale"),
        WeighIn("dst-in", local(2026, 11, 1, 4, 30), 90718, "scale"),
    ]
    _sync(migrated_engine, SimClock(local(2026, 11, 1, 12)), provider)
    with migrated_engine.connect() as conn:
        canon = canonical_weigh_ins(conn, date(2026, 11, 1), date(2026, 11, 1))
    assert [c.observed_at for c in canon] == [local(2026, 11, 1, 4, 30)]


def test_complete_through_advances_only_after_midnight_data(migrated_engine: Engine) -> None:
    provider = SimulatedProvider(
        preset="steady-loser", seed=3, anchor_date=date(2026, 10, 1), tz=NY
    )
    clock = SimClock(local(2026, 10, 10, 23, 30))
    run_sync(migrated_engine, clock, provider, tz=NY, unit="lb")
    with migrated_engine.connect() as conn:
        before = latest_complete_through(conn)
    assert before["steps"] < date(2026, 10, 10)
    clock.set(provider.release_time(date(2026, 10, 10)) + timedelta(minutes=1))
    run_sync(migrated_engine, clock, provider, tz=NY, unit="lb")
    with migrated_engine.connect() as conn:
        after = latest_complete_through(conn)
        steps_10th = conn.execute(
            select(func.count()).where(
                Observation.metric == "steps", Observation.local_date == date(2026, 10, 10)
            )
        ).scalar_one()
    assert after["steps"] == date(2026, 10, 10)
    assert after["weight"] == date(2026, 10, 10)
    assert steps_10th in {0, 1}  # a no-record day still completes


def test_failed_provider_records_failure_and_recovers(migrated_engine: Engine) -> None:
    provider = ScriptedProvider()
    provider.fail = True
    clock = SimClock(local(2026, 10, 5, 7))
    result = run_sync(migrated_engine, clock, provider, tz=NY, unit="lb")
    assert result.status == "failed"
    assert result.error is not None and "garmin down" in result.error
    provider.fail = False
    provider.weigh_ins = [WeighIn("w", local(2026, 10, 5, 6), 90718, "scale")]
    clock.advance(timedelta(minutes=15))
    ok = run_sync(migrated_engine, clock, provider, tz=NY, unit="lb")
    assert (ok.status, ok.rows_new) == ("ok", 1)
    with migrated_engine.connect() as conn:
        statuses = conn.execute(select(SyncRun.status).order_by(SyncRun.id)).scalars().all()
    assert statuses == ["failed", "ok"]


def test_observations_are_append_only(migrated_engine: Engine) -> None:
    provider = ScriptedProvider()
    provider.weigh_ins = [WeighIn("w", local(2026, 10, 5, 6), 90718, "scale")]
    _sync(migrated_engine, SimClock(local(2026, 10, 5, 7)), provider)
    for statement in ("UPDATE observations SET value = 1", "DELETE FROM observations"):
        with pytest.raises(IntegrityError, match="append-only"), immediate(migrated_engine) as c:
            c.execute(text(statement))


def test_sync_job_period_key_and_run(
    migrated_engine: Engine, settings: Settings, instance_config: InstanceConfig
) -> None:
    dev = settings.model_copy(update={"data_provider": "simulated"})
    job = GarminSyncJob(dev, instance_config)
    clock = SimClock(local(2026, 10, 5, 6, 7))
    assert job.due(clock.now()) == "2026-10-05T06:00"
    assert run_due([job], migrated_engine, clock) == ["garmin_sync"]
    assert run_due([job], migrated_engine, clock) == []
    with migrated_engine.connect() as conn:
        assert conn.execute(select(SyncRun.status)).scalar_one() == "ok"


def test_garmindb_provider_needs_a_token(
    migrated_engine: Engine, settings: Settings, tmp_path: Path
) -> None:
    prod_like = settings.model_copy(update={"data_provider": "garmindb", "garmin_home": tmp_path})
    clock = SimClock(local(2026, 10, 5, 7))
    with pytest.raises(ProviderUnavailable, match="garmin-login"):
        make_provider(prod_like, migrated_engine, clock)
    (tmp_path / ".GarminDb").mkdir()
    (tmp_path / ".GarminDb" / "garmin_tokens.json").write_text("{}")
    provider = make_provider(prod_like, migrated_engine, clock)
    assert isinstance(provider, GarminDBProvider)
    assert make_provider(prod_like, migrated_engine, clock, provider) is provider


# ---- failure alerts and staleness ------------------------------------------


def _alerts(engine: Engine) -> list[tuple[str, dict[str, object]]]:
    with engine.connect() as conn:
        return [
            (key, payload)
            for key, payload in conn.execute(
                select(OutboxMessage.dedupe_key, OutboxMessage.payload)
                .where(OutboxMessage.category == "admin_alerts")
                .order_by(OutboxMessage.id)
            )
        ]


def test_a_failure_streak_alerts_once(migrated_engine: Engine) -> None:
    clock = SimClock(local(2026, 10, 5, 7))
    provider = ScriptedProvider()
    assert _sync(migrated_engine, clock, provider).status == "ok"
    provider.fail = True
    for _ in range(3):
        clock.advance(timedelta(minutes=15))
        result = _sync(migrated_engine, clock, provider)
        assert result.status == "failed" and "garmin down" in (result.error or "")
    alerts = _alerts(migrated_engine)
    assert len(alerts) == 1 and alerts[0][1]["kind"] == "sync_failed"
    provider.fail = False
    clock.advance(timedelta(minutes=15))
    assert _sync(migrated_engine, clock, provider).status == "ok"
    provider.fail = True
    clock.advance(timedelta(minutes=15))
    _sync(migrated_engine, clock, provider)
    assert len(_alerts(migrated_engine)) == 2  # a new streak alerts again


def test_garmin_errors_are_stored_as_given(migrated_engine: Engine) -> None:
    class Expired(ScriptedProvider):
        def fetch_since(self, since: datetime, now: datetime) -> Batch:
            raise GarminRunError("Garmin login failed: rerun garmin-login")

    result = _sync(migrated_engine, SimClock(local(2026, 10, 5, 7)), Expired())
    assert result.error == "Garmin login failed: rerun garmin-login"


def test_no_new_data_for_36_hours_fails_during_a_season(migrated_engine: Engine) -> None:
    clock = SimClock(local(2026, 10, 5, 7))
    provider = ScriptedProvider()
    provider.weigh_ins = [WeighIn("w1", local(2026, 10, 5, 6), 90_000, "scale")]
    assert _sync(migrated_engine, clock, provider).rows_new == 1
    clock.advance(timedelta(hours=40))
    assert _sync(migrated_engine, clock, provider).status == "ok"  # no season: no alarm
    with immediate(migrated_engine) as conn:
        open_season(conn, clock)
    clock.advance(timedelta(minutes=15))
    stale = _sync(migrated_engine, clock, provider)
    assert stale.status == "failed" and stale.error == "no new data for 40 h"
    assert _alerts(migrated_engine)[0][1]["error"] == "no new data for 40 h"
    provider.weigh_ins.append(WeighIn("w2", clock.now() - timedelta(minutes=5), 89_900, "scale"))
    clock.advance(timedelta(minutes=15))
    assert _sync(migrated_engine, clock, provider).status == "ok"


def test_stale_data_blocks_settlement(world: World) -> None:
    """A market whose window has ended does not settle while syncs are failing."""
    w = world
    markets.drop(w.engine, w.clock, w.config, Timeframe.DAILY, date(2026, 10, 5))
    provider = ScriptedProvider()
    provider.fail = True
    w.clock.advance(timedelta(days=2))
    markets.lock_due(w.engine, w.clock.now())
    for _ in range(4):
        w.clock.advance(timedelta(hours=2))
        assert _sync(w.engine, w.clock, provider).status == "failed"
    settle_pass = settlement.settle_due(w.engine, w.clock)
    assert settle_pass.settled == 0
    with w.engine.connect() as conn:
        statuses = set(conn.execute(select(Market.status)).scalars())
    assert "settled" not in statuses


def test_sync_job_without_a_token_records_a_failed_run(
    migrated_engine: Engine, settings: Settings, instance_config: InstanceConfig, tmp_path: Path
) -> None:
    real = settings.model_copy(update={"data_provider": "garmindb", "garmin_home": tmp_path})
    clock = SimClock(local(2026, 10, 5, 7))
    run_due([GarminSyncJob(real, instance_config)], migrated_engine, clock)
    with migrated_engine.connect() as conn:
        status, error = conn.execute(select(SyncRun.status, SyncRun.error)).one()
    assert status == "failed" and "garmin-login" in (error or "")
    assert _alerts(migrated_engine)[0][1]["kind"] == "sync_failed"
