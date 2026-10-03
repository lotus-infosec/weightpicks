"""Dev simulation: the persisted SimClock, simulator settings and fast-forward.

`advance()` replays every one-minute worker tick between `sim_now` and the target
through the same `run_due()` the worker uses, so a simulated season exercises the
real job code paths (hard rule 8).
"""

import time as perf
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, insert, select, update

from app.core.clock import Clock, SimClock, SystemClock
from app.core.config import Settings
from app.core.db import immediate
from app.domain.schedule import local_date
from app.models import SimState
from app.providers.simulated import PRESETS

DEFAULT_PRESET = "steady-loser"
TICK = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class SimConfig:
    sim_now: datetime
    preset: str
    seed: int
    anchor_date: date


def _row(conn: Connection) -> SimConfig | None:
    row = conn.execute(
        select(SimState.sim_now, SimState.preset, SimState.seed, SimState.anchor_date).where(
            SimState.id == 1
        )
    ).one_or_none()
    return None if row is None else SimConfig(*row)


def ensure_state(conn: Connection, clock: Clock, *, seed: int, tz_name: str) -> SimConfig:
    """Load the simulation state, creating it from `clock` on first use."""
    state = _row(conn)
    if state is not None:
        return state
    now = clock.now().replace(second=0, microsecond=0)
    anchor = local_date(now, ZoneInfo(tz_name))
    conn.execute(
        insert(SimState).values(
            id=1, sim_now=now, preset=DEFAULT_PRESET, seed=seed, anchor_date=anchor
        )
    )
    return SimConfig(now, DEFAULT_PRESET, seed, anchor)


def load_state(engine: Engine, settings: Settings) -> SimConfig:
    with immediate(engine) as conn:
        return ensure_state(
            conn, SystemClock(), seed=settings.sim_seed, tz_name=settings.wp_timezone
        )


def save_now(conn: Connection, when: datetime) -> None:
    conn.execute(update(SimState).where(SimState.id == 1).values(sim_now=when))


class PersistedSimClock:
    """Dev clock: `now()` is `sim_state.sim_now`. It only moves when advanced or set."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def now(self) -> datetime:
        with self._engine.connect() as conn:
            value = conn.execute(select(SimState.sim_now).where(SimState.id == 1)).scalar_one()
        return value


def app_clock(settings: Settings, engine: Engine) -> Clock:
    """The clock for domain logic: the persisted SimClock in dev, real time otherwise."""
    if settings.is_dev:
        load_state(engine, settings)
        return PersistedSimClock(engine)
    return SystemClock()


@dataclass(slots=True)
class AdvanceResult:
    start: datetime
    end: datetime
    ticks: int = 0
    jobs_run: Counter[str] = field(default_factory=Counter)
    seconds: float = 0.0


def advance(engine: Engine, settings: Settings, delta: timedelta) -> AdvanceResult:
    """Fast-forward the dev clock, running every worker tick in between."""
    from app.worker.jobs import domain_jobs
    from app.worker.registry import run_due

    if delta <= timedelta(0):
        raise ValueError("can only advance forward")
    started = perf.perf_counter()
    state = load_state(engine, settings)
    end = state.sim_now + delta
    clock = SimClock(state.sim_now)
    result = AdvanceResult(start=state.sim_now, end=end)
    jobs = domain_jobs(settings)
    seen: dict[str, str] = {}
    last_saved_day = state.sim_now.date()
    while clock.now() < end:
        clock.set(min(clock.now() + TICK, end))
        result.jobs_run.update(run_due(jobs, engine, clock, seen))
        result.ticks += 1
        if clock.now().date() != last_saved_day:
            last_saved_day = clock.now().date()
            with immediate(engine) as conn:
                save_now(conn, clock.now())
    with immediate(engine) as conn:
        save_now(conn, end)
    result.seconds = perf.perf_counter() - started
    return result


def set_now(engine: Engine, settings: Settings, when: datetime) -> None:
    """Jump the dev clock forward without running the ticks in between."""
    state = load_state(engine, settings)
    if when.tzinfo is None:
        raise ValueError("expected an aware datetime")
    if when < state.sim_now:
        raise ValueError("the simulation clock only moves forward")
    with immediate(engine) as conn:
        save_now(conn, when)


def reseed(engine: Engine, settings: Settings, *, preset: str, seed: int) -> None:
    """Change the simulator; already-ingested observations are immutable and stay."""
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}; choose from {sorted(PRESETS)}")
    load_state(engine, settings)
    with immediate(engine) as conn:
        conn.execute(update(SimState).where(SimState.id == 1).values(preset=preset, seed=seed))
