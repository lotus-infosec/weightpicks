"""Shared helpers for market, bet and settlement tests."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from app.core.clock import SimClock
from app.core.config import Settings
from app.providers.simulated import SimulatedProvider
from app.services.instance import InstanceConfig
from app.services.sync import run_sync

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


def sync_sim(engine: Engine, clock: SimClock) -> None:
    """Ingest the simulator (anchored Sep 1, 2026) up to `clock.now()`."""
    provider = SimulatedProvider(preset="steady-loser", seed=3, anchor_date=date(2026, 9, 1), tz=NY)
    assert run_sync(engine, clock, provider, tz=NY, unit="lb").status == "ok"


def mark_setup_done(engine: Engine, settings: Settings) -> None:
    """Pretend /setup already ran: the settings row exists with setup_completed_at."""
    from sqlalchemy import update

    from app.core.db import immediate
    from app.models import InstanceSettingsRow
    from app.services import instance

    clock = SimClock(local(2026, 10, 1, 9))
    with immediate(engine) as conn:
        instance.ensure(conn, clock, settings)
        conn.execute(update(InstanceSettingsRow).values(setup_completed_at=clock.now()))
