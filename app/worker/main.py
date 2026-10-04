"""Worker process: wait for the migrated schema, then tick every 60 s until SIGTERM."""

import signal
import threading
import time
from collections.abc import Sequence
from types import FrameType

import structlog
from sqlalchemy import Engine

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.db import make_engine
from app.core.logging import configure_logging
from app.core.migrations import is_at_head
from app.notify.dispatcher import Dispatcher
from app.services import instance
from app.services.instance import InstanceConfig
from app.services.sim import app_clock
from app.worker.jobs import INFRA_JOBS, domain_jobs
from app.worker.jobs.commands import CommandsJob
from app.worker.jobs.outbox import OutboxDispatchJob
from app.worker.registry import Job, run_due

TICK_SECONDS = 60
FAST_SECONDS = 2  # outbox fast loop while rows are waiting (BUILD_PLAN §1.4.5)
SCHEMA_POLL_SECONDS = 2

log = structlog.get_logger()


def tick(
    engine: Engine,
    *,
    infra: Sequence[Job],
    domain: Sequence[Job],
    system_clock: Clock,
    domain_clock: Clock,
    seen: dict[str, str],
) -> list[str]:
    """One worker tick: infrastructure jobs on real time, domain jobs on the app clock."""
    ran = run_due(infra, engine, system_clock, seen)
    return ran + run_due(domain, engine, domain_clock, seen)


def reload_if_changed(
    engine: Engine,
    clock: Clock,
    settings: Settings,
    config: InstanceConfig,
    jobs: Sequence[Job],
) -> tuple[InstanceConfig, Sequence[Job]]:
    """Rebuild the domain jobs when /setup or the admin changed the settings row."""
    latest = instance.load(engine, clock, settings)
    if latest == config:
        return config, jobs
    log.info("worker_settings_reloaded")
    return latest, domain_jobs(settings, latest)


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = make_engine(settings.db_url)
    clock = SystemClock()
    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("worker_stopping", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stop.is_set() and not is_at_head(engine):
        log.info("waiting_for_schema")
        stop.wait(SCHEMA_POLL_SECONDS)

    domain_clock = app_clock(settings, engine)
    config = instance.load(engine, domain_clock, settings)
    jobs: Sequence[Job] = domain_jobs(settings, config)
    dispatch = OutboxDispatchJob(Dispatcher(settings, clock, domain_clock))
    infra: tuple[Job, ...] = (*INFRA_JOBS, CommandsJob(settings, domain_clock), dispatch)
    seen: dict[str, str] = {}
    log.info(
        "worker_started",
        tick_seconds=TICK_SECONDS,
        jobs=[j.name for j in (*infra, *jobs)],
        sim_clock=settings.sim_clock,
    )
    next_tick = 0.0
    while not stop.is_set():
        try:
            if time.monotonic() >= next_tick:
                next_tick = time.monotonic() + TICK_SECONDS
                config, jobs = reload_if_changed(engine, domain_clock, settings, config, jobs)
                tick(
                    engine,
                    infra=infra,
                    domain=jobs,
                    system_clock=clock,
                    domain_clock=domain_clock,
                    seen=seen,
                )
            else:  # fast loop: only the outbox, while posts are still waiting
                run_due([dispatch], engine, clock)
        except Exception:
            log.exception("tick_failed")
        stop.wait(FAST_SECONDS if dispatch.more else max(0.0, next_tick - time.monotonic()))
    engine.dispose()
    log.info("worker_stopped")


if __name__ == "__main__":
    main()
