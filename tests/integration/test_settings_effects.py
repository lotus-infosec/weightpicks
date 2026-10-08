"""The settings row is the source of truth: economy and schedule changes take effect."""

from datetime import date
from fractions import Fraction

from sqlalchemy import select, update

from app.core.db import immediate
from app.domain.economy import Economy
from app.domain.markets import Timeframe
from app.models import InstanceSettingsRow, OddsVersion, OutboxMessage
from app.services import instance, markets
from app.services.bets import BetRejected
from tests.integration.test_bets_settlement import bet, player, weight_market
from tests.integration.world import World


def set_economy(w: World, **changes: object) -> None:
    economy = Economy(**changes)  # type: ignore[arg-type]
    with immediate(w.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(economy=economy.to_json()))


def test_drops_price_with_the_configured_vig(world: World) -> None:
    set_economy(world, hold=Fraction(1, 12))
    with world.engine.connect() as conn:
        config = instance.read(conn)
    assert config is not None
    markets.drop(world.engine, world.clock, config, Timeframe.DAILY, date(2026, 10, 5))
    with world.engine.connect() as conn:
        holds = list(conn.execute(select(OddsVersion.model_inputs)).scalars())
    assert holds and {round(i["hold"], 6) for i in holds} == {round(1 / 12, 6)}


def test_max_bet_and_high_roller_come_from_settings(world: World) -> None:
    set_economy(world, max_bet_cents=5_000, high_roller_cents=2_000)
    user = player(world, 1)
    market = weight_market(world, -5)
    try:
        bet(world, user, market, "over", stake=5_001)
    except BetRejected as exc:
        assert exc.reason == "above_maximum"
    else:
        raise AssertionError("expected above_maximum")
    bet(world, user, market, "over", stake=2_000)
    with world.engine.connect() as conn:
        categories = sorted(conn.execute(select(OutboxMessage.category)).scalars())
    assert categories == ["bets_placed", "high_roller"]


def test_worker_rebuilds_jobs_when_settings_change(world: World) -> None:
    from app.worker.jobs import domain_jobs
    from app.worker.main import reload_if_changed

    config = instance.load(world.engine, world.clock, world.settings)
    jobs = domain_jobs(world.settings, config)
    same = reload_if_changed(world.engine, world.clock, world.settings, config, jobs)
    assert same[1] is jobs
    with immediate(world.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(timezone="America/Chicago"))
    latest, rebuilt = reload_if_changed(world.engine, world.clock, world.settings, config, jobs)
    assert latest.timezone == "America/Chicago" and rebuilt is not jobs
    sync = next(j for j in rebuilt if j.name == "garmin_sync")
    assert str(sync.config.tz) == "America/Chicago"  # type: ignore[attr-defined]
