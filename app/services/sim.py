"""Dev simulation state: the persisted SimClock and the simulator's preset/seed."""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Connection, insert, select, update

from app.core.clock import Clock
from app.domain.schedule import local_date
from app.models import SimState

DEFAULT_PRESET = "steady-loser"


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
    """Load the simulation state, creating it from the real clock on first use."""
    from zoneinfo import ZoneInfo

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


def save_now(conn: Connection, when: datetime) -> None:
    conn.execute(update(SimState).where(SimState.id == 1).values(sim_now=when))
