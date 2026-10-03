"""A live server for browser tests: uvicorn on a free port, temp DB, simulated data."""

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from app.core.clock import SimClock, SystemClock
from app.core.config import Settings
from app.core.db import immediate, make_engine
from app.core.migrations import upgrade_to_head
from app.domain.markets import Timeframe
from app.services import auth, instance, markets
from app.services.ledger import open_season
from app.web.main import create_app
from tests.integration.world import local, sync_sim


@dataclass
class LiveServer:
    url: str
    code: str


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    return browser_context_args | {"viewport": {"width": 375, "height": 812}, "is_mobile": True}


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[LiveServer]:
    settings = Settings(app_env="dev", data_dir=tmp_path, log_format="console", log_level="WARNING")
    engine = make_engine(settings.db_url)
    upgrade_to_head(engine, tmp_path / ".migrate.lock")
    clock = SimClock(local(2026, 10, 5, 12))
    sync_sim(engine, clock)
    with immediate(engine) as conn:
        open_season(conn, clock)
        config = instance.ensure(conn, clock, settings)
    markets.drop(engine, clock, config, Timeframe.DAILY, date(2026, 10, 5))
    code = auth.rotate_registration_code(engine, SystemClock())
    engine.dispose()

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, domain_clock=clock),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.05)
    yield LiveServer(f"http://127.0.0.1:{port}", code)
    server.should_exit = True
    thread.join(timeout=10)
