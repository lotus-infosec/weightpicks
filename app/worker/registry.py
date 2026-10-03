"""Job protocol and the exactly-once runner (BUILD_PLAN §1.4.6).

A job says *which period is due* (`due(now) -> period_key | None`). The runner
claims `(job, period_key)` in `job_runs` inside one BEGIN IMMEDIATE transaction;
the unique constraint means a period can only ever be claimed once, so repeated
ticks, restarts and catch-up never run anything twice. A failed run stays
recorded as `error` for that period.

A job with `every_tick = True` (e.g. `lock_markets`) is idempotent by itself and runs
on every tick without a `job_runs` row; it is reported as run when it returns a
truthy value (it did something).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import structlog
from sqlalchemy import Engine, insert, update
from sqlalchemy.exc import IntegrityError

from app.core.clock import Clock
from app.core.db import immediate
from app.models import JobRun

log = structlog.get_logger()
MAX_ERROR_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class JobContext:
    engine: Engine
    clock: Clock
    now: datetime
    period_key: str


class Job(Protocol):
    name: str

    def due(self, now: datetime) -> str | None: ...

    def run(self, ctx: JobContext) -> int | None: ...


def _claim(engine: Engine, job: str, period_key: str, now: datetime) -> int | None:
    try:
        with immediate(engine) as conn:
            result = conn.execute(
                insert(JobRun)
                .values(job=job, period_key=period_key, status="running", started_at=now)
                .returning(JobRun.id)
            )
            return result.scalar_one()
    except IntegrityError:
        return None


def _finish(engine: Engine, run_id: int, status: str, at: datetime, error: str | None) -> None:
    with immediate(engine) as conn:
        conn.execute(
            update(JobRun)
            .where(JobRun.id == run_id)
            .values(status=status, finished_at=at, error=error)
        )


def _run_every_tick(job: Job, engine: Engine, clock: Clock, now: datetime) -> bool:
    try:
        return bool(job.run(JobContext(engine=engine, clock=clock, now=now, period_key="tick")))
    except Exception:
        log.bind(job=job.name).exception("job_failed")
        return False


def run_due(
    jobs: Sequence[Job], engine: Engine, clock: Clock, seen: dict[str, str] | None = None
) -> list[str]:
    """Run every job whose current period has not been claimed yet. Returns names run.

    `seen` (job name -> last period key attempted by this process) skips the claim
    write when the period hasn't changed; `job_runs` uniqueness remains the guarantee.
    """
    ran: list[str] = []
    now = clock.now()
    for job in jobs:
        if getattr(job, "every_tick", False):
            if _run_every_tick(job, engine, clock, now):
                ran.append(job.name)
            continue
        period_key = job.due(now)
        if period_key is None:
            continue
        if seen is not None:
            if seen.get(job.name) == period_key:
                continue
            seen[job.name] = period_key
        run_id = _claim(engine, job.name, period_key, now)
        if run_id is None:
            continue
        bound = log.bind(job=job.name, period_key=period_key)
        try:
            job.run(JobContext(engine=engine, clock=clock, now=now, period_key=period_key))
        except Exception as exc:
            bound.exception("job_failed")
            _finish(engine, run_id, "error", clock.now(), repr(exc)[:MAX_ERROR_LENGTH])
        else:
            _finish(engine, run_id, "ok", clock.now(), None)
        ran.append(job.name)
    return ran
