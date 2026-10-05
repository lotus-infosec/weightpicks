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
from datetime import date, timedelta
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
from tests.integration.world import local, mark_setup_done, sync_sim


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
    mark_setup_done(engine, settings)
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
    mark_setup_done(engine, settings)
    _props_and_parlays(engine, clock)
    _ai_props(engine, clock)
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


@dataclass
class FreshServer:
    url: str
    token: str


@pytest.fixture
def fresh_server(tmp_path: Path) -> Iterator[FreshServer]:
    """A brand-new instance that hasn't been set up, with a secret key for secrets."""
    from pydantic import SecretStr

    from app.services import setup

    settings = Settings(
        app_env="dev",
        data_dir=tmp_path,
        log_format="console",
        log_level="WARNING",
        app_secret_key=SecretStr("e2e-" + "k" * 40),
    )
    engine = make_engine(settings.db_url)
    upgrade_to_head(engine, tmp_path / ".migrate.lock")
    token = setup.issue_token(engine, SystemClock())  # what `wp setup-token` prints
    engine.dispose()
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.05)
    yield FreshServer(f"http://127.0.0.1:{port}", token)
    server.should_exit = True
    thread.join(timeout=10)


@dataclass
class OpsServer:
    url: str
    admin_email: str
    password: str
    reckless_id: int
    steady_id: int


@pytest.fixture
def ops_server(tmp_path: Path) -> Iterator[OpsServer]:
    """A dev instance on the persisted SimClock (so /dev/clock moves time), set up, with an
    admin, two players and bets: one player has staked everything on a losing side."""
    from sqlalchemy import select, update

    from app.calibration import canonical_tenths
    from app.models import InstanceSettingsRow, Market, OddsVersion, Selection
    from app.providers.simulated import SimulatedProvider
    from app.services import ledger, sim
    from app.services.bets import place_bet

    settings = Settings(
        app_env="dev",
        data_dir=tmp_path,
        data_provider="simulated",
        log_format="console",
        log_level="WARNING",
    )
    engine = make_engine(settings.db_url)
    upgrade_to_head(engine, tmp_path / ".migrate.lock")
    start = local(2026, 10, 5, 0, 30)
    with immediate(engine) as conn:
        state = sim.ensure_state(
            conn, SimClock(start), seed=settings.sim_seed, tz_name=settings.wp_timezone
        )
        ledger.open_season(conn, SimClock(start))
    sim.advance(engine, settings, local(2026, 10, 5, 12) - start)  # first daily drop at 11:00
    clock = SimClock(local(2026, 10, 5, 12))
    admin_email, password = "admin@example.invalid", "correct horse battery"
    auth.create_admin(
        engine, SystemClock(), email=admin_email, display_name="Admin", password=password
    )
    mark_setup_done(engine, settings)
    with immediate(engine) as conn:  # no allowance, so the all-in loser really goes bust
        economy = instance.read(conn).economy.to_json() | {"daily_allowance_cents": 0}  # type: ignore[union-attr]
        conn.execute(update(InstanceSettingsRow).values(economy=economy))
    code = auth.rotate_registration_code(engine, SystemClock())
    ids = [
        auth.register(
            engine,
            SystemClock(),
            email=f"{n}@example.invalid",
            display_name=n.title(),
            password=password,
            code=code,
            ip=f"10.0.0.{i}",
        )
        for i, n in enumerate(("reckless", "steady"), start=1)
    ]
    provider = SimulatedProvider(
        preset=state.preset, seed=state.seed, anchor_date=state.anchor_date, tz=settings.tz
    )
    series = canonical_tenths(provider, 10, settings.tz, settings.wp_unit)
    change = series[date(2026, 10, 6)] - series[date(2026, 10, 5)]
    with engine.connect() as conn:
        market_id, line, version = conn.execute(
            select(Market.id, OddsVersion.line_x10, OddsVersion.id)
            .join(OddsVersion, (OddsVersion.market_id == Market.id) & OddsVersion.is_current)
            .where(Market.metric == "weight", Market.timeframe == "daily")
        ).one()
        assert line is not None
        losing = "under" if change > line else "over"
        selections = dict(
            conn.execute(
                select(Selection.side, Selection.id).where(Selection.market_id == market_id)
            ).all()
        )
    assert change != line, "pick another seed: the line tied"
    place_bet(
        engine,
        clock,
        user_id=ids[0],
        selection_id=selections[losing],
        odds_version_id=version,
        stake_cents=100_000,
        client_key="all-in",
    )
    place_bet(
        engine,
        clock,
        user_id=ids[1],
        selection_id=selections["over"],
        odds_version_id=version,
        stake_cents=2_500,
        client_key="steady-1",
    )
    engine.dispose()

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.05)
    yield OpsServer(f"http://127.0.0.1:{port}", admin_email, password, ids[0], ids[1])
    server.should_exit = True
    thread.join(timeout=10)


def _props_and_parlays(engine: Any, clock: SimClock) -> None:
    """Turn props/futures and parlays on and post one engine-priced milestone (STAGE12)."""
    from datetime import timedelta

    from sqlalchemy import select, update

    from app.models import InstanceSettingsRow
    from app.services import props
    from app.services.admin import Actor
    from app.services.observations import canonical_weigh_ins

    with immediate(engine) as conn:
        flags = conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {}
        conn.execute(
            update(InstanceSettingsRow).values(
                flags=dict(flags) | {"props_futures": True, "parlays": True}
            )
        )
    admin_id = auth.create_admin(
        engine,
        SystemClock(),
        email="admin@example.invalid",
        display_name="Admin",
        password="correct horse battery",
    )
    today = clock.now().date()
    with engine.connect() as conn:
        latest = canonical_weigh_ins(conn, today - timedelta(days=5), today)[-1].value / 10
    deadline = (today + timedelta(days=7)).isoformat()
    for tenths in range(5, 120, 5):
        try:
            props.create(
                engine,
                clock,
                Actor(user_id=admin_id),
                "milestone_by",
                {"threshold": f"{latest - tenths / 10:.1f}", "deadline": deadline},
            )
            return
        except props.PropError:
            continue
    raise AssertionError("no priceable milestone for the e2e server")


def _ai_props(engine: Any, clock: SimClock) -> None:
    """STAGE13: AI props on, one published AI prop with a blurb, one waiting for review,
    an AI run and an admin note (inserted directly: no Workers AI in e2e)."""
    from sqlalchemy import insert, select, update

    from app.models import AdminNote, AiProposal, AiRun, InstanceSettingsRow
    from app.services import props

    now = clock.now()
    with immediate(engine) as conn:
        flags = conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {}
        conn.execute(update(InstanceSettingsRow).values(flags=dict(flags) | {"ai_props": True}))
        config = instance.read(conn)
        assert config is not None
        run_id = conn.execute(
            insert(AiRun)
            .values(
                kind="props_daily",
                model="@cf/meta/llama-3.1-8b-instruct",
                prompt_version="props_v1",
                started_at=SystemClock().now(),  # the neuron quota runs on real time
                input_tokens=812,
                output_tokens=233,
                neurons_est=39,
                status="ok",
                raw_output='{"proposals": [...]}',
                errors=[{"index": 2, "template": "milestone_by", "reason": "link_or_markup"}],
            )
            .returning(AiRun.id)
        ).scalar_one()
        today = now.astimezone(config.tz).date()
        published = props.preview(conn, config, today, "beat_last_week", {"metric": "steps"})
        props.insert_prop(
            conn,
            clock,
            config,
            published,
            origin="ai",
            ai_run_id=run_id,
            blurb="Last week set a high bar for steps. Can this week clear it?",
        )
        waiting = props.preview(conn, config, today, "beat_last_week", {"metric": "kcal"})
        conn.execute(
            insert(AiProposal).values(
                ai_run_id=run_id,
                template="beat_last_week",
                form={"metric": "kcal"},
                title=waiting.spec.title,
                blurb="A big calorie week would make this one interesting.",
                dedupe_key=waiting.spec.dedupe_key,
                status="pending",
                created_at=now,
                expires_at=waiting.spec.lock_at,
            )
        )
        conn.execute(
            insert(AdminNote).values(
                text="Traveling Thu-Sun",
                active_from=today,
                active_to=today + timedelta(days=4),
                created_at=now,
            )
        )
