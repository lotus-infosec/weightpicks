from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Engine

from app.core.clock import SimClock
from app.core.config import Settings
from app.core.db import make_engine

if TYPE_CHECKING:
    from app.services.instance import InstanceConfig


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(app_env="dev", data_dir=tmp_path, log_format="console")


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    eng = make_engine(settings.db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def clock() -> SimClock:
    return SimClock(datetime(2026, 10, 5, 6, 30, tzinfo=UTC))


@pytest.fixture
def migrated_engine(engine: Engine, tmp_path: Path) -> Engine:
    from app.core.migrations import upgrade_to_head

    upgrade_to_head(engine, tmp_path / ".migrate.lock")
    return engine


@pytest.fixture
def instance_config() -> "InstanceConfig":
    """Default schedule, America/New_York, lb, every count metric enabled."""
    from app.domain.markets import COUNT_MARKET_METRICS, Schedule
    from app.services.instance import InstanceConfig

    return InstanceConfig("America/New_York", "lb", Schedule(), COUNT_MARKET_METRICS, "active")
