"""A live server for browser tests: uvicorn on a free port, temp DB, simulated data.

Set PLAYWRIGHT_WS_ENDPOINT to drive a browser running elsewhere, e.g. the official
Playwright image (`scripts/browser.sh`), when the local machine lacks Chromium's
system libraries.
"""

import os
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
def connect_options() -> dict[str, Any] | None:
    endpoint = os.environ.get("PLAYWRIGHT_WS_ENDPOINT")
    return {"endpoint": endpoint} if endpoint else None


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


@dataclass
class RichServer:
    url: str
    email: str
    password: str


@pytest.fixture
def rich_server(tmp_path: Path) -> Iterator[RichServer]:
    """A lived-in instance at Sun Oct 4, 19:00: settled and open bets from two players,
    and open daily and weekly markets."""
    from app.services import ledger, settlement
    from app.services.bets import place_bet

    settings = Settings(app_env="dev", data_dir=tmp_path, log_format="console", log_level="WARNING")
    engine = make_engine(settings.db_url)
    upgrade_to_head(engine, tmp_path / ".migrate.lock")
    clock = SimClock(local(2026, 10, 3, 12))
    sync_sim(engine, clock)
    with immediate(engine) as conn:
        open_season(conn, clock)
        config = instance.ensure(conn, clock, settings)
    code = auth.rotate_registration_code(engine, SystemClock())
    users = []
    for n, name in enumerate(("Sam", "Alex"), start=1):
        users.append(
            auth.register(
                engine,
                SystemClock(),
                email=f"p{n}@example.invalid",
                display_name=name,
                password="correct horse battery",
                code=code,
                ip=f"10.0.0.{n}",
            )
        )
    markets.drop(engine, clock, config, Timeframe.DAILY, date(2026, 10, 3))

    from sqlalchemy import select

    from app.models import Market, OddsVersion, Selection

    def bet_all(day: date, stake: int) -> None:
        with engine.connect() as conn:
            rows = conn.execute(
                select(Selection.id, OddsVersion.id, Selection.side, OddsVersion.odds)
                .join(Market, Market.id == Selection.market_id)
                .join(OddsVersion, (OddsVersion.market_id == Market.id) & OddsVersion.is_current)
                .where(Market.window_start == day, Market.status == "open")
            ).all()
        for i, (sel, ver, side, odds) in enumerate(rows[:6]):
            if odds.get(side) is None:
                continue
            place_bet(
                engine,
                clock,
                user_id=users[i % 2],
                selection_id=sel,
                odds_version_id=ver,
                stake_cents=stake + 500 * i,
                client_key=f"{day}-{i}",
            )

    bet_all(date(2026, 10, 3), 2_000)
    clock.set(local(2026, 10, 4, 13, 30))
    sync_sim(engine, clock)
    markets.lock_due(engine, clock.now())
    settlement.settle_due(engine, clock)
    clock.set(local(2026, 10, 4, 18))
    markets.drop(engine, clock, config, Timeframe.WEEKLY, date(2026, 10, 4))
    clock.set(local(2026, 10, 4, 19))
    markets.drop(engine, clock, config, Timeframe.DAILY, date(2026, 10, 4))
    bet_all(date(2026, 10, 4), 1_500)
    with engine.connect() as conn:
        assert ledger.verify(conn).ok
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
    yield RichServer(f"http://127.0.0.1:{port}", "p1@example.invalid", "correct horse battery")
    server.should_exit = True
    thread.join(timeout=10)
