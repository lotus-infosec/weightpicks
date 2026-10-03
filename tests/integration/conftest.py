import pytest
from sqlalchemy import Engine

from app.core.clock import SimClock
from app.core.config import Settings
from app.core.db import immediate
from app.services import instance
from app.services.ledger import open_season
from tests.integration.world import World, local, sync_sim


@pytest.fixture
def world(migrated_engine: Engine, settings: Settings) -> World:
    """Five weeks of simulated history, a season, the settings row; 12:00 on Mon Oct 5."""
    clock = SimClock(local(2026, 10, 5, 12))
    sync_sim(migrated_engine, clock)
    with immediate(migrated_engine) as conn:
        open_season(conn, clock)
        config = instance.ensure(conn, clock, settings)
    return World(migrated_engine, clock, config, settings)
