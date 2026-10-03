from datetime import datetime, timedelta

from sqlalchemy import Engine, select

from app.core.clock import SimClock
from app.core.config import Settings
from app.models import Heartbeat, JobRun
from app.services.instance import InstanceConfig
from app.worker.jobs import INFRA_JOBS, HeartbeatJob, domain_jobs
from app.worker.registry import JobContext, run_due


class CountingJob:
    def __init__(self, name: str = "counting") -> None:
        self.name = name
        self.calls: list[str] = []

    def due(self, now: datetime) -> str | None:
        return now.date().isoformat()

    def run(self, ctx: JobContext) -> None:
        self.calls.append(ctx.period_key)


class FailingJob(CountingJob):
    def run(self, ctx: JobContext) -> None:
        raise RuntimeError("kaboom")


class NeverDueJob(CountingJob):
    def due(self, now: datetime) -> None:
        return None


def _runs(engine: Engine) -> list[tuple[str, str, str]]:
    with engine.connect() as conn:
        query = select(JobRun.job, JobRun.period_key, JobRun.status).order_by(JobRun.id)
        return [(job, key, status) for job, key, status in conn.execute(query)]


def test_same_period_runs_exactly_once(migrated_engine: Engine, clock: SimClock) -> None:
    job = CountingJob()
    assert run_due([job], migrated_engine, clock) == ["counting"]
    assert run_due([job], migrated_engine, clock) == []
    clock.advance(timedelta(hours=3))  # same day -> same period
    assert run_due([job], migrated_engine, clock) == []
    assert job.calls == ["2026-10-05"]
    assert _runs(migrated_engine) == [("counting", "2026-10-05", "ok")]


def test_new_period_runs_again_and_catches_up(migrated_engine: Engine, clock: SimClock) -> None:
    job = CountingJob()
    run_due([job], migrated_engine, clock)
    clock.advance(timedelta(days=3))  # downtime: next tick runs the latest period once
    run_due([job], migrated_engine, clock)
    assert job.calls == ["2026-10-05", "2026-10-08"]


def test_failure_is_recorded_and_does_not_block_other_jobs(
    migrated_engine: Engine, clock: SimClock
) -> None:
    good = CountingJob("good")
    ran = run_due([FailingJob("bad"), NeverDueJob("idle"), good], migrated_engine, clock)
    assert ran == ["bad", "good"]
    assert good.calls == ["2026-10-05"]
    runs = _runs(migrated_engine)
    assert ("bad", "2026-10-05", "error") in runs
    with migrated_engine.connect() as conn:
        error = conn.execute(select(JobRun.error).where(JobRun.job == "bad")).scalar_one()
    assert error is not None and "kaboom" in error
    # The failed period is not retried on the next tick.
    assert run_due([FailingJob("bad")], migrated_engine, clock) == []


def test_heartbeat_job_upserts_clock_time(migrated_engine: Engine, clock: SimClock) -> None:
    run_due([HeartbeatJob()], migrated_engine, clock)
    clock.advance(timedelta(seconds=61))
    run_due([HeartbeatJob()], migrated_engine, clock)
    with migrated_engine.connect() as conn:
        beats = conn.execute(select(Heartbeat.component, Heartbeat.beat_at)).all()
    assert beats == [("worker", clock.now())]


def test_registered_jobs_have_unique_names(
    settings: Settings, instance_config: InstanceConfig
) -> None:
    names = [job.name for job in (*INFRA_JOBS, *domain_jobs(settings, instance_config))]
    assert len(names) == len(set(names))
    assert names == [
        "heartbeat",
        "garmin_sync",
        "daily_drop",
        "weekly_drop",
        "monthly_drop",
        "lock_markets",
        "ledger_verify",
    ]


def test_seen_cache_skips_repeat_claims_but_not_new_periods(
    migrated_engine: Engine, clock: SimClock
) -> None:
    job = CountingJob()
    seen: dict[str, str] = {}
    assert run_due([job], migrated_engine, clock, seen) == ["counting"]
    assert run_due([job], migrated_engine, clock, seen) == []
    assert seen == {"counting": "2026-10-05"}
    clock.advance(timedelta(days=1))
    assert run_due([job], migrated_engine, clock, seen) == ["counting"]
    # A fresh process (empty cache) still can't run a claimed period twice.
    assert run_due([job], migrated_engine, clock, {}) == []
