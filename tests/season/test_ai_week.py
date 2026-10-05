"""STAGE13 measure: a week of simulated drops produces 1-3 valid AI props a day, with
neurons a day logged well under the cap. Time moves through `sim.advance`, so the real
worker jobs run (sync -> drops -> AI props -> lock -> settle); only the Workers AI HTTP
transport is fake (tests/fake_ai.py, which mixes in bad proposals every cycle)."""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, func, select, update

from app.ai import client as ai_client
from app.core.clock import SimClock
from app.core.config import Settings
from app.core.db import immediate, make_engine
from app.core.migrations import upgrade_to_head
from app.models import AiProposal, AiRun, InstanceSettingsRow, Market
from app.services import ai_props, instance, ledger, secrets, sim
from app.services.audit import Actor
from tests.fake_ai import menu_model
from tests.integration.world import create_admin

pytestmark = pytest.mark.season

NY = ZoneInfo("America/New_York")
START = date(2026, 10, 1)
WARM_UP_DAYS = 10
WEEK = 7
KEY = "k" * 40


@dataclass
class Week:
    engine: Engine
    settings: Settings
    rows: list[dict[str, Any]] = field(default_factory=list)
    reasons: Counter[str] = field(default_factory=Counter)
    approved: int | None = None


def at(day: date, wall: time) -> datetime:
    return datetime.combine(day, wall, tzinfo=NY).astimezone(UTC)


@pytest.fixture(scope="module")
def week(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Week]:
    tmp = tmp_path_factory.mktemp("ai-week")
    settings = Settings(
        app_env="dev",
        data_dir=tmp,
        data_provider="simulated",
        log_format="console",
        log_level="WARNING",
        app_secret_key=SecretStr(KEY),
    )
    engine = make_engine(settings.db_url)
    upgrade_to_head(engine, Path(tmp) / ".migrate.lock")
    start = at(START, time(0, 30))
    with immediate(engine) as conn:
        sim.ensure_state(conn, SimClock(start), seed=7, tz_name="America/New_York")
        ledger.open_season(conn, SimClock(start))
        config = instance.ensure(conn, SimClock(start), settings)
        conn.execute(update(InstanceSettingsRow).values(setup_completed_at=start))
        secrets.put(conn, SimClock(start), KEY, "workers_ai.account_id", "acct")
        secrets.put(conn, SimClock(start), KEY, "workers_ai.token", "tok")
    model = menu_model(seed=13)
    patch = pytest.MonkeyPatch()
    real_from_store = ai_client.from_store
    patch.setattr(
        ai_props,
        "from_store",
        lambda conn, s, transport=None: real_from_store(conn, s, model.transport),
    )
    # Warm up with AI off so the weight model has history, then switch it on (auto mode).
    now = start
    first = START + timedelta(days=WARM_UP_DAYS)
    sim.advance(engine, settings, at(first, time(0, 30)) - now)
    now = at(first, time(0, 30))
    with immediate(engine) as conn:
        flags = config.flags | {"props_futures": True, "ai_props": True}
        conn.execute(update(InstanceSettingsRow).values(flags=flags, ai_mode="auto"))
    run = Week(engine, settings)
    for k in range(WEEK):
        day = first + timedelta(days=k)
        before = _max_run(engine)
        target = at(day, time(23, 0))  # past the daily drop, the AI run, the weekly drop
        sim.advance(engine, settings, target - now)
        now = target
        with engine.connect() as conn:
            runs = conn.execute(select(AiRun).where(AiRun.id > before).order_by(AiRun.id)).all()
            made = Counter(
                conn.execute(
                    select(AiRun.kind)
                    .join(Market, Market.ai_run_id == AiRun.id)
                    .where(AiRun.id > before)
                ).scalars()
            )
        for r in runs:
            run.reasons.update(f"{e.get('template')}:{e['reason']}" for e in r.errors)
        run.rows.append(
            {
                "day": day,
                "daily": made["props_daily"],
                "weekly": made["props_weekly"],
                "runs": len(runs),
                "dropped": sum(len(r.errors) for r in runs),
                "neurons": sum(r.neurons_est for r in runs),
                "statuses": sorted({r.status for r in runs}),
            }
        )
    # One more day in review mode: the cycle queues, the admin approves one.
    with immediate(engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(ai_mode="review"))
    day = first + timedelta(days=WEEK)
    target = at(day, time(12, 0))
    sim.advance(engine, settings, target - now)
    admin_id = create_admin(
        engine, SimClock(target), email="a@example.invalid", display_name="A", password="x" * 12
    )
    with engine.connect() as conn:
        queued = ai_props.pending(conn, target)
    if queued:
        run.approved = ai_props.approve(engine, SimClock(target), Actor(admin_id), queued[0].id)
    patch.undo()
    yield run
    engine.dispose()


def _max_run(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.coalesce(func.max(AiRun.id), 0))).scalar_one())


def test_a_week_of_drops_yields_one_to_three_valid_props_a_day(week: Week) -> None:
    print("\nday         daily weekly runs dropped neurons status")
    for r in week.rows:
        print(
            f"{r['day']}  {r['daily']:>5} {r['weekly']:>6} {r['runs']:>4} {r['dropped']:>7} "
            f"{r['neurons']:>7} {','.join(r['statuses'])}"
        )
    print(f"drop reasons: {dict(week.reasons)}")
    assert all(1 <= r["daily"] <= 3 for r in week.rows), week.rows
    assert all(r["statuses"] == ["ok"] for r in week.rows)
    assert sum(r["weekly"] for r in week.rows) >= 1  # the Sunday weekly run posted too
    assert week.reasons  # the validators really were exercised


def test_neurons_per_day_stay_well_under_the_cap(week: Week) -> None:
    cap = week.settings.ai_daily_neuron_cap
    assert all(0 < r["neurons"] <= cap // 10 for r in week.rows), week.rows


def test_review_mode_queues_and_approval_publishes(week: Week) -> None:
    assert week.approved is not None
    with week.engine.connect() as conn:
        origin, status = conn.execute(
            select(Market.origin, Market.status).where(Market.id == week.approved)
        ).one()
        statuses = Counter(conn.execute(select(AiProposal.status)).scalars())
    assert (origin, status) == ("ai", "open")
    assert statuses["approved"] == 1


def test_ai_props_settle_through_the_normal_pipeline(week: Week) -> None:
    """Settlement never reads AI output: AI props are ordinary prop markets."""
    with week.engine.connect() as conn:
        by_status = Counter(
            conn.execute(select(Market.status).where(Market.origin == "ai")).scalars()
        )
    assert by_status["settled"] + by_status["locked"] + by_status["open"] == sum(by_status.values())
    assert by_status["settled"] >= 1
