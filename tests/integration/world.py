"""Shared helpers for market, bet and settlement tests."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, insert, update

from app.core.clock import Clock, SimClock
from app.core.config import Settings
from app.core.db import immediate
from app.core.security import hash_password
from app.models import InstanceSettingsRow, User
from app.providers.simulated import SimulatedProvider
from app.services.instance import InstanceConfig
from app.services.sync import run_sync

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 5)  # a Monday


def create_admin(
    engine: Engine, clock: Clock, *, email: str, display_name: str, password: str
) -> int:
    """The instance admin, as /setup leaves it (tests skip the wizard): the user, and
    setup marked complete on the settings row if there is one."""
    with immediate(engine) as conn:
        user_id: int = conn.execute(
            insert(User)
            .values(
                email=email.strip().lower(),
                display_name=display_name,
                password_hash=hash_password(password),
                role="admin",
                status="active",
                created_at=clock.now(),
            )
            .returning(User.id)
        ).scalar_one()
        conn.execute(update(InstanceSettingsRow).values(setup_completed_at=clock.now()))
    return user_id


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
    """Pretend /setup already ran: the settings row exists with setup_completed_at, and
    registration is open (the admin's `registration_open` flag, off by default)."""
    from sqlalchemy import update

    from app.core.db import immediate
    from app.models import InstanceSettingsRow
    from app.services import instance

    clock = SimClock(local(2026, 10, 1, 9))
    with immediate(engine) as conn:
        instance.ensure(conn, clock, settings)
        conn.execute(
            update(InstanceSettingsRow).values(
                setup_completed_at=clock.now(), flags={"registration_open": True}
            )
        )
